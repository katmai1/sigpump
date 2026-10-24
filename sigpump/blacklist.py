"""
sigpump/blacklist.py

Lista negra de wallets, en la tabla `lista_negra` de la base del registro
([radar].alert_log_path): una fila por wallet con el motivo y, si es
temporal, hasta cuándo dura.
"""

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger()

# hasta NULL = permanente: la wallet se quita del fichero de wallets y no se
# vuelve a seguir. Con hasta, sigue en el fichero y no se consulta hasta
# entonces (las abandonadas, para comprobar si vuelven a operar).
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS lista_negra ("
    "wallet TEXT PRIMARY KEY, motivo TEXT, fecha TEXT, timestamp REAL, hasta REAL)"
)


class Blacklist:
    def __init__(self, path: Path):
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        """Conexión abierta la primera vez, con la tabla creada. Lanza
        OSError si no se puede abrir."""
        if self._conn is None:
            try:
                conn = sqlite3.connect(self._path)
                conn.execute(_SCHEMA)
                conn.commit()
            except sqlite3.Error as exc:
                raise OSError(f"no se pudo abrir la lista negra en {self._path}: {exc}") from exc
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def entries(self) -> dict[str, float | None]:
        """Wallet -> fin (epoch) de su entrada vigente, None si es permanente.
        Lanza OSError si no se puede leer."""
        try:
            rows = self._db().execute(
                "SELECT wallet, hasta FROM lista_negra WHERE hasta IS NULL OR hasta > ?", (time.time(),)
            ).fetchall()
        except sqlite3.Error as exc:
            raise OSError(f"no se pudo leer la lista negra en {self._path}: {exc}") from exc
        return dict(rows)

    def reason(self, address: str) -> str | None:
        """Motivo de la entrada de `address`, vigente o no; None si no tiene."""
        row = self._db().execute("SELECT motivo FROM lista_negra WHERE wallet = ?", (address,)).fetchone()
        return row[0] if row else None

    def add(self, address: str, reason: str, until: float | None = None) -> None:
        """Mete `address` con su motivo; con `until` (epoch), solo hasta
        entonces. Una temporal no reemplaza a una permanente. De paso borra
        las temporales vencidas. Lanza OSError si no se puede escribir."""
        now = time.time()
        try:
            conn = self._db()
            with conn:
                conn.execute("DELETE FROM lista_negra WHERE hasta IS NOT NULL AND hasta <= ?", (now,))
                if until is not None and conn.execute(
                    "SELECT 1 FROM lista_negra WHERE wallet = ? AND hasta IS NULL", (address,)
                ).fetchone():
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO lista_negra (wallet, motivo, fecha, timestamp, hasta) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (address, reason, datetime.fromtimestamp(now).isoformat(timespec="seconds"), now, until),
                )
        except sqlite3.Error as exc:
            raise OSError(f"no se pudo escribir la lista negra en {self._path}: {exc}") from exc
