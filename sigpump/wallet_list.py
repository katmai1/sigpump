"""
sigpump/wallet_list.py

Las wallets seguidas: el fichero de wallets (una por línea, con su etiqueta
detrás de `#` y un `*` si es de confianza), menos las de la lista negra, y
las castigadas por un tiempo, que siguen en el fichero pero no se consultan.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sigpump.blacklist import Blacklist
from sigpump.util import SOLANA_ADDRESS_RE

log = logging.getLogger()

# Marca de wallet de confianza en el fichero, detrás de la dirección.
TRUSTED_MARK = "*"


@dataclass(frozen=True)
class FollowedWallet:
    """Wallet del fichero: su etiqueta y si es de confianza (avisa sola,
    aunque no llegue a [wallets].min_wallets)."""
    label: str
    trusted: bool = False


def load_wallets(path: Path, blacklist: set[str] | frozenset[str] = frozenset()) -> dict[str, FollowedWallet]:
    """
    Dirección -> wallet, desde un fichero con una wallet por línea. Lo que
    va detrás de `#` es la etiqueta; un `*` detrás de la dirección la marca
    de confianza. Las líneas que empiezan por `#` y las vacías se ignoran.
    Una línea que no es una dirección se avisa y se salta, para que un typo
    no tire el resto de la lista. Una dirección repetida se queda con su
    primera aparición y las demás se borran del fichero, igual que las que
    están en `blacklist`.
    """
    wallets: dict[str, FollowedWallet] = {}
    kept: list[str] = []
    removed = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(keepends=True), start=1):
        address, _, label = line.partition("#")
        address = address.strip()
        trusted = address.endswith(TRUSTED_MARK)
        address = address.removesuffix(TRUSTED_MARK).strip()
        if address in wallets:
            log.warning("%s:%d wallet duplicada, se elimina: %s", path, number, address)
            removed = True
            continue
        if address in blacklist:
            log.warning("%s:%d wallet en la lista negra, se elimina: %s", path, number, address)
            removed = True
            continue
        kept.append(line)
        if not address:
            continue
        if not SOLANA_ADDRESS_RE.match(address):
            log.warning("%s:%d no es una dirección de Solana, se ignora: %r", path, number, address)
            continue
        wallets[address] = FollowedWallet(label.strip() or f"{address[:4]}…{address[-4:]}", trusted)
    if removed:
        # Se escribe aparte y se renombra para no dejar el fichero a medias.
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text("".join(kept), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("No se pudieron quitar wallets de %s: %s", path, exc)
    return wallets


class WalletList:
    """Las wallets seguidas: las del fichero menos las de la lista negra, y
    hasta cuándo está castigada cada una de las temporales (siguen en el
    fichero, pero no se consultan). Relee el fichero cuando cambia, y la
    lista negra en cada vuelta, sin reiniciar."""

    def __init__(self, path: Path, blacklist: Blacklist | None = None):
        self._path = path
        self._blacklist = blacklist
        self._wallets: dict[str, FollowedWallet] = {}
        # Wallet -> fin (epoch) de su entrada temporal en la lista negra.
        self._banned_until: dict[str, float] = {}
        # Fecha del fichero y lista negra con los que se cargaron las wallets.
        self._loaded: tuple[float, dict[str, float | None]] | None = None

    @property
    def wallets(self) -> dict[str, FollowedWallet]:
        return self._wallets

    @property
    def has_blacklist(self) -> bool:
        return self._blacklist is not None

    def banned(self, address: str) -> bool:
        """True si `address` tiene una entrada temporal vigente en la lista negra."""
        return self._banned_until.get(address, 0.0) > time.time()

    def blacklist(self, address: str, reason: str, until: float | None = None) -> None:
        """Mete `address` en la lista negra; con `until` (epoch), solo hasta
        entonces. Lanza OSError si no se puede escribir la lista."""
        if self._blacklist is None:
            return
        self._blacklist.add(address, reason, until)
        if until is None:
            self._wallets.pop(address, None)
        else:
            self._banned_until[address] = until
        # Fuerza la relectura, que la quita también del fichero de wallets.
        self._loaded = None

    def reload(self) -> None:
        """Relee el fichero si cambió él o la lista negra desde la última
        vez. Si no se puede leer se sigue con la lista anterior."""
        try:
            entries = self._blacklist.entries() if self._blacklist else {}
            mtime = self._path.stat().st_mtime
            if (mtime, entries) == self._loaded:
                return
            blacklist = {address for address, until in entries.items() if until is None}
            wallets = load_wallets(self._path, blacklist)
            # Quitar wallets reescribe el fichero y cambia su fecha.
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            if self._loaded is not None or not self._wallets:
                log.warning("No se pudo leer el fichero de wallets %s: %s", self._path, exc)
            self._loaded = None
            return
        self._loaded = (mtime, entries)
        self._banned_until = {address: until for address, until in entries.items() if until is not None}
        if wallets != self._wallets:
            log.info("Siguiendo %d wallets de %s", len(wallets), self._path)
        self._wallets = wallets
