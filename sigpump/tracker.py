"""
sigpump/tracker.py

Registro de resultados: guarda en una base SQLite cada compra de una wallet
seguida (avisada o descartada, con su motivo) con los datos del momento, y
sigue su precio en DexScreener durante los 30 minutos siguientes. Sirve
para medir qué wallets dan margen, alimenta la lista negra automática y
permite ajustar los filtros con datos en vez de a ojo.
"""

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sigpump.util import buy_ratio_m5, normalize_address, to_float, txns_m5, volume_acceleration

log = logging.getLogger()

# Minutos después de la alerta en los que se anota el retorno.
CHECKPOINTS_MINUTES = (5, 15, 30)
# El precio se muestrea en cada vuelta del seguimiento de wallets, así que
# cada checkpoint se anota con la primera muestra que cae dentro de este
# margen. Pasado el margen queda en NULL: tras un reinicio, anotar como "+5m"
# un precio de +25m falsearía los datos.
CHECKPOINT_TOLERANCE_MINUTES = 5
TRACKING_MINUTES = CHECKPOINTS_MINUTES[-1]

_CHECKPOINT_COLUMNS = tuple(f"ret_{m}m_pct" for m in CHECKPOINTS_MINUTES)
_BEST, _WORST = f"mejor_ret_{TRACKING_MINUTES}m_pct", f"peor_ret_{TRACKING_MINUTES}m_pct"

# Columnas de la tabla `alertas` (además de `id`) y su tipo. Al abrir una
# base creada por una versión anterior se agregan las que falten; las que
# ya no se usan (score, velas, prealertas) se quedan como están.
COLUMNS: dict[str, str] = {
    "fecha": "TEXT",
    "enviada": "INTEGER",  # 1 = aviso enviado, 0 = descartado (ver motivo_descarte)
    # 'wallet'; las bases anteriores también tienen 'alerta', 'prealerta' o NULL.
    "tipo": "TEXT",
    "motivo_descarte": "TEXT",
    "simbolo": "TEXT",
    "token": "TEXT",
    "pool": "TEXT",
    "precio_usd": "REAL",
    "cambio_m5_pct": "REAL",
    "cambio_h1_pct": "REAL",
    "cambio_h6_pct": "REAL",
    "volumen_m5_usd": "REAL",
    "txns_m5": "REAL",
    # Contra qué cotiza el par y cuánto llevaba vivo al avisar.
    "moneda_par": "TEXT",
    "edad_par_min": "REAL",
    "volumen_h1_usd": "REAL",
    "liquidez_usd": "REAL",
    "market_cap_usd": "REAL",
    "aceleracion_volumen": "REAL",
    "compras_m5_pct": "REAL",
    # Quién compró, cuánto pagó, si ya tenía el token y
    # cuántas otras wallets seguidas habían entrado en él poco antes.
    "wallet": "TEXT",
    "wallet_etiqueta": "TEXT",
    "sol_gastado": "REAL",
    "stable_gastado": "REAL",
    "entrada_nueva": "INTEGER",
    "wallets_confluencia": "INTEGER",
    "tx": "TEXT",
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

    def record(
        self,
        pair: dict,
        sent: bool,
        reason: str = "",
        kind: str = "wallet",
    ) -> int | None:
        """Agrega una fila con los datos de `pair` en el momento del aviso
        (o del descarte, con sent=False y su motivo). Devuelve el id de la
        fila, o None si no se pudo registrar."""
        conn = self._db()
        if conn is None:
            return None
        now = time.time()
        base = pair.get("baseToken") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        buy_ratio = buy_ratio_m5(pair)
        values = {
            "fecha": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "enviada": int(sent),
            "tipo": kind,
            "motivo_descarte": reason or None,
            "simbolo": str(base.get("symbol") or ""),
            "token": str(normalize_address(base.get("address") or "")),
            "pool": str(normalize_address(pair.get("pairAddress") or "")),
            "precio_usd": to_float(pair.get("priceUsd")) or None,
            "cambio_m5_pct": to_float(change.get("m5")),
            "cambio_h1_pct": to_float(change.get("h1")),
            "cambio_h6_pct": to_float(change.get("h6")),
            "volumen_m5_usd": _round(to_float(volume.get("m5"))),
            "txns_m5": txns_m5(pair),
            "moneda_par": str((pair.get("quoteToken") or {}).get("symbol") or ""),
            "edad_par_min": _round((now - created / 1000) / 60) if (created := to_float(pair.get("pairCreatedAt"))) > 0 else None,
            "volumen_h1_usd": _round(to_float(volume.get("h1"))),
            "liquidez_usd": _round(to_float((pair.get("liquidity") or {}).get("usd"))),
            "market_cap_usd": _round(to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))),
            "aceleracion_volumen": _round(volume_acceleration(pair)),
            "compras_m5_pct": _round(buy_ratio * 100) if buy_ratio is not None else None,
            "url": str(pair.get("url") or ""),
            "timestamp": now,
        }
        try:
            cursor = conn.execute(
                f"INSERT INTO alertas ({', '.join(values)}) "
                f"VALUES ({', '.join(':' + name for name in values)})",
                values,
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("No se pudo registrar la señal de %s en %s: %s", values["simbolo"], self._path, exc)
            return None
        return cursor.lastrowid

    def update_row(self, row_id: int, values: dict[str, object]) -> None:
        """Actualiza columnas de una fila ya registrada."""
        unknown = set(values) - set(COLUMNS)
        if unknown:
            raise ValueError(f"Columnas desconocidas en el registro: {sorted(unknown)}")
        conn = self._db()
        if conn is None or not values:
            return
        try:
            conn.execute(
                f"UPDATE alertas SET {', '.join(f'{c} = :{c}' for c in values)} WHERE id = :id",
                {**values, "id": row_id},
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("No se pudo actualizar %s: %s", self._path, exc)

    def recent_wallet_buys(self, since: float) -> list[dict]:
        """Compras de wallets seguidas (avisadas o no) posteriores a `since`,
        de la más vieja a la más nueva: token, wallet, wallet_etiqueta,
        timestamp, enviada y wallets_confluencia."""
        conn = self._db()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT token, wallet, wallet_etiqueta, timestamp, enviada, wallets_confluencia "
                "FROM alertas "
                "WHERE tipo = 'wallet' AND wallet IS NOT NULL AND timestamp > :since "
                "ORDER BY timestamp",
                {"since": since},
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return []
        return [dict(row) for row in rows]

    def rug_wallets(self, drop_pct: float) -> dict[str, str]:
        """Wallet -> descripción del rug, de las wallets con alguna compra
        (avisada o no) que cayó al menos `drop_pct` en su seguimiento."""
        conn = self._db()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                f"SELECT wallet, simbolo, token, MIN({_WORST}) AS peor FROM alertas "
                f"WHERE tipo = 'wallet' AND wallet IS NOT NULL AND {_WORST} <= :limit "
                "GROUP BY wallet",
                {"limit": -drop_pct},
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return {}
        return {
            row["wallet"]: f"rug {row['simbolo'] or row['token']} {row['peor']:+.0f}%"
            for row in rows
        }

    def wallet_returns(self) -> dict[str, list[float]]:
        """Wallet -> rendimiento a 30 min de la primera compra registrada de
        cada token (avisada o no), solo las ya medidas. Una por token: las
        compras repetidas del mismo token no deben pesar más."""
        conn = self._db()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT wallet, ret_30m_pct FROM alertas WHERE id IN ("
                "SELECT MIN(id) FROM alertas WHERE tipo = 'wallet' AND wallet IS NOT NULL "
                "GROUP BY wallet, token) AND ret_30m_pct IS NOT NULL"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer %s: %s", self._path, exc)
            return {}
        returns: dict[str, list[float]] = {}
        for row in rows:
            returns.setdefault(row["wallet"], []).append(row["ret_30m_pct"])
        return returns

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
                        (row["tipo"] or "alerta") if row["enviada"] else "descartado",
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
