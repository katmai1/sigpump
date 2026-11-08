"""
sigpump/activity.py

Qué hace cada wallet seguida, contado por día y tipo de transacción
(compra, compra pequeña, con stable, en bonding curve, venta, otra,
fallida, transferencia) en la tabla `actividad_wallet` de la base del
registro ([radar].alert_log_path).

La tabla `alertas` solo guarda las compras que llegan al radar: las de menos
de min_sol no dejan rastro y las de bonding curve solo salen en el log. El
2026-11-08, para saber por qué 24 de 90 wallets no tenían ni una fila hubo
que leer su historial en el RPC (wallet_audit.py, ~1000 créditos). Con esta
tabla, `wallet_audit.py --base` responde lo mismo sin gastar nada.

Se guardan contadores y no una fila por transacción: las wallets seguidas
hacían más de 28.000 a la semana (unos 150 MB al año); así son unos pocos MB.
Se cuenta en memoria y se vuelca cada FLUSH_SECONDS y al parar: un corte
pierde como mucho ese rato.
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger()

FLUSH_SECONDS = 300

# dia en hora local (AAAA-MM-DD), como `fecha` en alertas. sol = SOL gastado
# en las compras de ese tipo; ultima = blockTime de la más reciente.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS actividad_wallet ("
    "wallet TEXT, dia TEXT, tipo TEXT, n INTEGER, sol REAL, ultima REAL, "
    "PRIMARY KEY (wallet, dia, tipo))"
)
_UPSERT = (
    "INSERT INTO actividad_wallet (wallet, dia, tipo, n, sol, ultima) VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (wallet, dia, tipo) DO UPDATE SET n = n + excluded.n, sol = sol + excluded.sol, "
    "ultima = MAX(ultima, excluded.ultima)"
)


def day_of(ts: float) -> str:
    """Día local de un timestamp."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class WalletActivity:
    """Contadores de actividad por wallet, día y tipo, pendientes de volcar
    a la base."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic):
        self._path = path
        self._clock = clock
        self._next_flush = clock() + FLUSH_SECONDS
        # (wallet, dia, tipo) -> [n, sol, ultima]
        self._pending: dict[tuple[str, str, str], list[float]] = {}

    def count(self, wallet: str, kind: str, sol: float = 0.0, ts: float | None = None) -> None:
        """Una transacción más de `wallet` del tipo `kind`."""
        ts = ts or time.time()
        entry = self._pending.setdefault((wallet, day_of(ts), kind), [0, 0.0, 0.0])
        entry[0] += 1
        entry[1] += sol
        entry[2] = max(entry[2], ts)

    def due(self) -> bool:
        return self._clock() >= self._next_flush

    def flush(self) -> int:
        """Suma lo pendiente a la base y devuelve cuántas filas tocó. Si no
        se puede escribir lo avisa y lo guarda para el siguiente intento:
        un fallo pasajero de la base no pierde la cuenta."""
        self._next_flush = self._clock() + FLUSH_SECONDS
        if not self._pending:
            return 0
        rows = [(w, d, k, int(n), round(sol, 4), ultima) for (w, d, k), (n, sol, ultima) in self._pending.items()]
        try:
            conn = sqlite3.connect(self._path)
            try:
                with conn:
                    conn.execute(_SCHEMA)
                    conn.executemany(_UPSERT, rows)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("No se pudo guardar la actividad de las wallets en %s: %s", self._path, exc)
            return 0
        self._pending.clear()
        return len(rows)


def read_activity(conn: sqlite3.Connection, since_day: str) -> list[tuple[str, str, str, int, float, float]]:
    """(wallet, dia, tipo, n, sol, ultima) desde `since_day` (incluido). Vacío
    si la tabla todavía no existe (base de antes o bot sin reiniciar)."""
    try:
        return conn.execute(
            "SELECT wallet, dia, tipo, n, sol, ultima FROM actividad_wallet WHERE dia >= ?", (since_day,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
