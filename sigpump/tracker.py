"""
sigpump/tracker.py

Registro de resultados: guarda en una base SQLite cada alerta enviada (y
cada candidato descartado por llegar tarde) con los datos del momento, y
sigue su precio en DexScreener durante los 30 minutos siguientes. Sirve
para medir si las señales dan margen y para ajustar umbrales con datos en
vez de a ojo.
"""

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sigpump.signals import RECENT_HIGH_MINUTES, CandleStats, buy_ratio_m5, volume_acceleration
from sigpump.util import normalize_address, to_float

log = logging.getLogger()

# Minutos después de la alerta en los que se anota el retorno.
CHECKPOINTS_MINUTES = (5, 15, 30)
# El precio solo se muestrea una vez por pasada del radar (~2-3 min), así que
# cada checkpoint se anota con la primera muestra que cae dentro de este
# margen. Pasado el margen queda en NULL: tras un reinicio, anotar como "+5m"
# un precio de +25m falsearía los datos.
CHECKPOINT_TOLERANCE_MINUTES = 5
TRACKING_MINUTES = CHECKPOINTS_MINUTES[-1]

_CHECKPOINT_COLUMNS = tuple(f"ret_{m}m_pct" for m in CHECKPOINTS_MINUTES)
_BEST, _WORST = f"mejor_ret_{TRACKING_MINUTES}m_pct", f"peor_ret_{TRACKING_MINUTES}m_pct"
_RECENT_HIGH = f"bajo_maximo_{RECENT_HIGH_MINUTES}m_pct"

# Columnas de la tabla `alertas` (además de `id`) y su tipo. Al abrir una
# base creada por una versión anterior se agregan las que falten.
COLUMNS: dict[str, str] = {
    "fecha": "TEXT",
    "enviada": "INTEGER",  # 1 = alerta enviada, 0 = descartada por llegar tarde
    "motivo_descarte": "TEXT",
    "simbolo": "TEXT",
    "token": "TEXT",
    "pool": "TEXT",
    "score": "REAL",
    "precio_usd": "REAL",
    "cambio_m5_pct": "REAL",
    "cambio_h1_pct": "REAL",
    "cambio_h6_pct": "REAL",
    "volumen_m5_usd": "REAL",
    "volumen_h1_usd": "REAL",
    "liquidez_usd": "REAL",
    "market_cap_usd": "REAL",
    "aceleracion_volumen": "REAL",
    "compras_m5_pct": "REAL",
    "sobre_minimo_1h_pct": "REAL",
    _RECENT_HIGH: "REAL",
    **{column: "REAL" for column in _CHECKPOINT_COLUMNS},
    _BEST: "REAL",
    _WORST: "REAL",
    "url": "TEXT",
    "timestamp": "REAL",
}

# Filas a las que todavía les falta el último checkpoint y siguen dentro de
# su margen. Sin precio en la alerta no hay contra qué medir.
_PENDING = f"precio_usd > 0 AND {_CHECKPOINT_COLUMNS[-1]} IS NULL AND timestamp > :cutoff"


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class AlertTracker:
    def __init__(self, path: Path):
        self._path = path
        self._conn: sqlite3.Connection | None = None
        # Se apaga si la base no se pudo abrir, para no reintentarlo (y
        # loguearlo) en cada pasada.
        self._enabled = True

    def open(self) -> None:
        """Abre (o crea) la base al arrancar y avisa cuántas alertas de antes
        de un reinicio siguen en seguimiento. Si no se puede abrir, desactiva
        el registro: el radar sigue alertando igual."""
        conn = self._db()
        if conn is None:
            return
        try:
            total = conn.execute("SELECT COUNT(*) FROM alertas").fetchone()[0]
            pending = conn.execute(
                f"SELECT COUNT(*) FROM alertas WHERE {_PENDING}", {"cutoff": self._cutoff()}
            ).fetchone()[0]
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return
        log.info("Registro de alertas: %d filas en %s, %d en seguimiento", total, self._path, pending)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection | None:
        """Conexión abierta la primera vez que se necesita, con la tabla
        creada o completada. None si el registro está desactivado."""
        if self._conn is not None or not self._enabled:
            return self._conn
        conn = None
        try:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            columns = ", ".join(f"{name} {kind}" for name, kind in COLUMNS.items())
            conn.execute(f"CREATE TABLE IF NOT EXISTS alertas (id INTEGER PRIMARY KEY, {columns})")
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(alertas)")}
            for name, kind in COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE alertas ADD COLUMN {name} {kind}")
            conn.execute("CREATE INDEX IF NOT EXISTS alertas_timestamp ON alertas (timestamp)")
            conn.commit()
        except sqlite3.Error as exc:
            # Un archivo que no es una base SQLite falla acá sin modificarse.
            log.error("No se pudo abrir %s, no se registrarán alertas: %s", self._path, exc)
            if conn is not None:
                conn.close()
            self._enabled = False
            return None
        self._conn = conn
        return conn

    @staticmethod
    def _cutoff(now: float | None = None) -> float:
        """Timestamp a partir del cual una fila sigue en seguimiento."""
        now = time.time() if now is None else now
        return now - (TRACKING_MINUTES + CHECKPOINT_TOLERANCE_MINUTES) * 60

    def is_tracking(self, address: object, sent: bool) -> bool:
        """True si `address` ya tiene una fila del mismo tipo (enviada o
        descartada) en seguimiento. Evita una fila por pasada para un token
        que sigue descartándose mientras está por encima del umbral."""
        conn = self._db()
        if conn is None:
            return False
        try:
            row = conn.execute(
                f"SELECT 1 FROM alertas WHERE token = :token AND enviada = :sent AND {_PENDING} LIMIT 1",
                {"token": str(address), "sent": int(sent), "cutoff": self._cutoff()},
            ).fetchone()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return False
        return row is not None

    def record(
        self,
        pair: dict,
        score: float,
        candles: CandleStats | None,
        sent: bool,
        reason: str = "",
    ) -> None:
        """Agrega una fila con los datos de `pair` en el momento de la alerta
        (o del descarte, con sent=False y su motivo)."""
        conn = self._db()
        if conn is None:
            return
        now = time.time()
        base = pair.get("baseToken") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        buy_ratio = buy_ratio_m5(pair)
        values = {
            "fecha": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "enviada": int(sent),
            "motivo_descarte": reason or None,
            "simbolo": str(base.get("symbol") or ""),
            "token": str(normalize_address(base.get("address") or "")),
            "pool": str(normalize_address(pair.get("pairAddress") or "")),
            "score": score,
            "precio_usd": to_float(pair.get("priceUsd")) or None,
            "cambio_m5_pct": to_float(change.get("m5")),
            "cambio_h1_pct": to_float(change.get("h1")),
            "cambio_h6_pct": to_float(change.get("h6")),
            "volumen_m5_usd": _round(to_float(volume.get("m5"))),
            "volumen_h1_usd": _round(to_float(volume.get("h1"))),
            "liquidez_usd": _round(to_float((pair.get("liquidity") or {}).get("usd"))),
            "market_cap_usd": _round(to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))),
            "aceleracion_volumen": _round(volume_acceleration(pair)),
            "compras_m5_pct": _round(buy_ratio * 100) if buy_ratio is not None else None,
            "sobre_minimo_1h_pct": _round(candles.rise_from_low_pct) if candles else None,
            _RECENT_HIGH: _round(candles.drop_from_recent_high_pct) if candles else None,
            "url": str(pair.get("url") or ""),
            "timestamp": now,
        }
        try:
            conn.execute(
                f"INSERT INTO alertas ({', '.join(values)}) "
                f"VALUES ({', '.join(':' + name for name in values)})",
                values,
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("No se pudo registrar la alerta de %s en %s: %s", values["simbolo"], self._path, exc)

    async def update(self, client, chain_id: str) -> None:
        """Muestrea el precio actual de las filas pendientes (un request a
        DexScreener cada 30 tokens) y anota los checkpoints alcanzados."""
        conn = self._db()
        if conn is None:
            return
        now = time.time()
        try:
            pending = conn.execute(
                f"SELECT * FROM alertas WHERE {_PENDING}", {"cutoff": self._cutoff(now)}
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return
        if not pending:
            return
        addresses = list(dict.fromkeys(row["token"] for row in pending))
        pairs = await client.get_pairs_for_tokens(chain_id, addresses)
        # Se sigue el mismo pool de la alerta: el precio de otro pool del
        # mismo token puede diferir y ensuciar el retorno.
        prices = {
            str(normalize_address(pair.get("pairAddress") or "")): to_float(pair.get("priceUsd"))
            for pair in pairs
        }
        try:
            for row in pending:
                price = prices.get(row["pool"])
                if not price:
                    continue
                ret = round((price / row["precio_usd"] - 1) * 100, 2)
                changes = {
                    _BEST: ret if row[_BEST] is None else max(row[_BEST], ret),
                    _WORST: ret if row[_WORST] is None else min(row[_WORST], ret),
                }
                elapsed_minutes = (now - row["timestamp"]) / 60
                for minutes, column in zip(CHECKPOINTS_MINUTES, _CHECKPOINT_COLUMNS):
                    in_window = minutes <= elapsed_minutes < minutes + CHECKPOINT_TOLERANCE_MINUTES
                    if in_window and row[column] is None:
                        changes[column] = ret
                conn.execute(
                    f"UPDATE alertas SET {', '.join(f'{c} = :{c}' for c in changes)} WHERE id = :id",
                    {**changes, "id": row["id"]},
                )
                if _CHECKPOINT_COLUMNS[-1] in changes:
                    final = {**dict(row), **changes}
                    log.info(
                        "Seguimiento de %s (%s): %s | mejor %+.1f%%, peor %+.1f%%",
                        row["simbolo"],
                        "alerta" if row["enviada"] else "descartado",
                        ", ".join(
                            f"+{m}m {final[c]:+.1f}%" if final[c] is not None else f"+{m}m s/d"
                            for m, c in zip(CHECKPOINTS_MINUTES, _CHECKPOINT_COLUMNS)
                        ),
                        final[_BEST],
                        final[_WORST],
                    )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("No se pudo actualizar %s: %s", self._path, exc)
