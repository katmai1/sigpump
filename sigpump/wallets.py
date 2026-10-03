"""
sigpump/wallets.py

Seguimiento de wallets: lee una lista de direcciones de un fichero, consulta
en el RPC de Solana sus transacciones nuevas y detecta en qué tokens entran.

Una compra se reconoce por los saldos antes y después de la transacción
(preTokenBalances/postTokenBalances y los lamports de la wallet), no por el
programa que la ejecutó: así vale igual para Raydium, Pump.fun, Jupiter,
Meteora o cualquier agregador, sin interpretar cada uno.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

log = logging.getLogger()

LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = "So11111111111111111111111111111111111111112"
# Pagar con estas no es "entrar" en ellas: son la moneda con la que se compra.
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
QUOTE_MINTS = STABLE_MINTS | {WSOL_MINT}
# Firmas nuevas que se piden por wallet y vuelta. Si una wallet hace más entre
# dos vueltas solo se miran las últimas: es un bot, no alguien a quien copiar.
MAX_SIGNATURES_PER_POLL = 25
REQUEST_TIMEOUT_SECONDS = 15
# Ritmo máximo de peticiones al RPC. El plan gratuito de Helius corta a 10
# por segundo; se deja margen para la comprobación de autoridades.
MAX_REQUESTS_PER_SECOND = 8
# Versión de transacción más alta que se acepta. Si llega una más nueva, el
# RPC da error y esa transacción se salta tras MAX_TX_ATTEMPTS intentos.
MAX_TRANSACTION_VERSION = 1
# Intentos de leer una transacción antes de saltarla. Sin tope, una que falla
# siempre dejaba la wallet atascada en ella sin ver nada de lo posterior.
MAX_TX_ATTEMPTS = 3
# "confirmed" llega ~10 s antes que "finalized"; revertir una confirmada es
# rarísimo y aquí solo se avisa, no se opera.
COMMITMENT = "confirmed"
# Direcciones de Solana: base58 de 32 bytes.
_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# Marca de wallet de confianza en el fichero, detrás de la dirección.
TRUSTED_MARK = "*"
# Entrada temporal de la lista negra: el motivo empieza por "hasta <fecha ISO>".
UNTIL_PREFIX = "hasta "


@dataclass(frozen=True)
class FollowedWallet:
    """Wallet del fichero: su etiqueta y si es de confianza (avisa sola,
    aunque no llegue a [wallets].min_wallets)."""
    label: str
    trusted: bool = False


@dataclass(frozen=True)
class WalletBuy:
    """Entrada de una wallet seguida en un token."""
    wallet: str
    label: str
    mint: str
    sol_spent: float
    stable_spent: float
    # True si la wallet no tenía el token antes: entrada nueva, no ampliación.
    new_position: bool
    signature: str
    ts: float
    trusted: bool = False


@dataclass(frozen=True)
class WalletSignal:
    """Lo que se avisa: la compra y las otras wallets seguidas que entraron
    en el mismo token hace poco (confluencia). `update` marca un token ya
    avisado al que se sumaron wallets nuevas."""
    buy: WalletBuy
    others: tuple[str, ...] = field(default_factory=tuple)
    update: bool = False


def _until(reason: str) -> float | None:
    """Fin (epoch) de una entrada temporal de la lista negra, por su motivo;
    None si es permanente."""
    reason = reason.strip()
    if not reason.startswith(UNTIL_PREFIX):
        return None
    stamp = reason.removeprefix(UNTIL_PREFIX).partition(" ")[0]
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _read_blacklist(path: Path) -> dict[str, float | None]:
    """Dirección -> fin de su entrada en la lista negra (None si es
    permanente). Si una dirección aparece varias veces, gana la permanente
    o la que acaba más tarde. Sin fichero, la lista está vacía."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    entries: dict[str, float | None] = {}
    for line in text.splitlines():
        address, _, reason = line.partition("#")
        address = address.strip()
        if not _ADDRESS_RE.match(address):
            continue
        until = _until(reason)
        if address in entries:
            previous = entries[address]
            until = None if previous is None or until is None else max(previous, until)
        entries[address] = until
    return entries


def load_blacklist(path: Path) -> set[str]:
    """Direcciones de la lista negra permanente (una por línea, lo que va
    detrás de `#` es el motivo). Sin fichero, la lista está vacía."""
    return {address for address, until in _read_blacklist(path).items() if until is None}


def load_temporary_blacklist(path: Path) -> dict[str, float]:
    """Dirección -> fin (epoch) de las entradas temporales de la lista negra
    que siguen vigentes."""
    now = time.time()
    return {
        address: until for address, until in _read_blacklist(path).items()
        if until is not None and until > now
    }


def add_to_blacklist(path: Path, address: str, reason: str, until: float | None = None) -> None:
    """Añade `address` a la lista negra con su motivo; con `until` (epoch),
    solo hasta entonces. De paso quita las entradas temporales ya vencidas,
    para que el fichero no crezca con cada vuelta de una wallet hiperactiva."""
    if until is not None:
        stamp = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")
        reason = f"{UNTIL_PREFIX}{stamp} · {reason}"
    line = f"{address}  # {reason}\n"
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    now = time.time()
    kept = [l for l in lines if (u := _until(l.partition("#")[2])) is None or u > now]
    if len(kept) == len(lines):
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        return
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    # Se escribe aparte y se renombra para no dejar el fichero a medias.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(kept) + line, encoding="utf-8")
    tmp.replace(path)


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
        if not _ADDRESS_RE.match(address):
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


def _token_amounts(balances: object, owner: str) -> dict[str, float]:
    """Mint -> cantidad de los token accounts de `owner` en pre/postTokenBalances."""
    amounts: dict[str, float] = {}
    if not isinstance(balances, list):
        return amounts
    for entry in balances:
        if not isinstance(entry, dict) or entry.get("owner") != owner:
            continue
        mint = entry.get("mint")
        ui = entry.get("uiTokenAmount") or {}
        try:
            amount = int(ui.get("amount")) / 10 ** int(ui.get("decimals"))
        except (TypeError, ValueError):
            continue
        if isinstance(mint, str):
            amounts[mint] = amounts.get(mint, 0.0) + amount
    return amounts


def _account_index(tx: dict, address: str) -> int | None:
    """Posición de `address` en las cuentas de la transacción (con jsonParsed
    vienen como {"pubkey": ...}; sin él, como texto)."""
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for i, key in enumerate(keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == address:
            return i
    return None


def parse_buys(
    tx: dict, wallet: str, label: str, signature: str, min_sol: float, trusted: bool = False
) -> list[WalletBuy]:
    """
    Compras de `wallet` en la transacción `tx` (getTransaction con
    jsonParsed): tokens cuyo saldo sube mientras la wallet paga SOL (al menos
    `min_sol`) o una stablecoin. Recibir un token sin pagar nada (airdrops de
    spam, transferencias) no cuenta.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    pre = _token_amounts(meta.get("preTokenBalances"), wallet)
    post = _token_amounts(meta.get("postTokenBalances"), wallet)
    deltas = {mint: post.get(mint, 0.0) - pre.get(mint, 0.0) for mint in pre.keys() | post.keys()}

    sol_spent = -deltas.get(WSOL_MINT, 0.0)
    index = _account_index(tx, wallet)
    pre_lamports, post_lamports = meta.get("preBalances") or [], meta.get("postBalances") or []
    if index is not None and index < min(len(pre_lamports), len(post_lamports)):
        spent = pre_lamports[index] - post_lamports[index]
        # La comisión de red la paga la cuenta 0 y no es parte de la compra.
        if index == 0:
            spent -= meta.get("fee") or 0
        sol_spent += spent / LAMPORTS_PER_SOL
    stable_spent = -sum(deltas.get(mint, 0.0) for mint in STABLE_MINTS)

    if sol_spent < min_sol and stable_spent <= 0:
        return []
    ts = tx.get("blockTime") or time.time()
    return [
        WalletBuy(
            wallet=wallet,
            label=label,
            mint=mint,
            sol_spent=round(max(0.0, sol_spent), 4),
            stable_spent=round(max(0.0, stable_spent), 2),
            new_position=pre.get(mint, 0.0) <= 0,
            signature=signature,
            ts=float(ts),
            trusted=trusted,
        )
        for mint, delta in deltas.items()
        if delta > 0 and mint not in QUOTE_MINTS
    ]


class WalletWatcher:
    """Consulta las transacciones nuevas de las wallets del fichero y
    devuelve sus compras. Relee el fichero (y la lista negra) cuando
    cambia, sin reiniciar."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        rpc_url: str,
        path: Path,
        min_sol: float,
        blacklist_path: Path | None = None,
    ):
        self._session = session
        self._rpc_url = rpc_url
        self._path = path
        self._min_sol = min_sol
        self._blacklist_path = blacklist_path
        self._wallets: dict[str, FollowedWallet] = {}
        # Wallet -> fin (epoch) de su entrada temporal en la lista negra: no
        # se consulta hasta entonces, pero sigue en el fichero de wallets.
        self._banned_until: dict[str, float] = {}
        self._mtime: tuple[float, float | None] | None = None
        # Wallet -> última firma procesada. Una wallet sin entrada todavía no
        # tiene punto de partida: su primera vuelta solo lo fija, para no
        # avisar de su historial al arrancar.
        self._last_signature: dict[str, str | None] = {}
        # Firma -> intentos fallidos de leerla.
        self._attempts: dict[str, int] = {}
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0

    @property
    def wallets(self) -> dict[str, FollowedWallet]:
        return self._wallets

    def _blacklist_mtime(self) -> float | None:
        if self._blacklist_path is None:
            return None
        try:
            return self._blacklist_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def banned(self, address: str) -> bool:
        """True si `address` tiene una entrada temporal vigente en la lista negra."""
        return self._banned_until.get(address, 0.0) > time.time()

    def blacklist(self, address: str, reason: str, seconds: float | None = None) -> None:
        """Mete `address` en la lista negra y deja de seguirla ya. Con
        `seconds`, solo durante ese tiempo: sigue en el fichero de wallets y
        se vuelve a consultar al acabar. Lanza OSError si no se puede
        escribir la lista."""
        if self._blacklist_path is None:
            return
        until = None if seconds is None else time.time() + seconds
        add_to_blacklist(self._blacklist_path, address, reason, until)
        if until is None:
            self._wallets.pop(address, None)
        else:
            self._banned_until[address] = until
        self._last_signature.pop(address, None)
        # Fuerza la relectura, que la quita también del fichero de wallets.
        self._mtime = None

    def reload(self) -> None:
        """Relee el fichero si cambió (él o la lista negra) desde la última
        vez. Si no se puede leer se sigue con la lista anterior."""
        try:
            mtime = (self._path.stat().st_mtime, self._blacklist_mtime())
            if mtime == self._mtime:
                return
            blacklist = load_blacklist(self._blacklist_path) if self._blacklist_path else set()
            banned = load_temporary_blacklist(self._blacklist_path) if self._blacklist_path else {}
            wallets = load_wallets(self._path, blacklist)
            # Quitar wallets reescribe el fichero y cambia su fecha.
            mtime = (self._path.stat().st_mtime, mtime[1])
        except OSError as exc:
            if self._mtime is not None or not self._wallets:
                log.warning("No se pudo leer el fichero de wallets %s: %s", self._path, exc)
            self._mtime = None
            return
        self._mtime = mtime
        self._banned_until = banned
        if wallets != self._wallets:
            log.info("Siguiendo %d wallets de %s", len(wallets), self._path)
        self._wallets = wallets
        for gone in set(self._last_signature) - set(wallets):
            del self._last_signature[gone]

    async def _rpc(self, method: str, params: list) -> object:
        """Resultado de una llamada al RPC. Lanza ValueError si el RPC
        devuelve un error, y los de red de aiohttp tal cual."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with self._rate_lock:
            wait = self._next_request - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request = time.monotonic() + 1 / MAX_REQUESTS_PER_SECOND
        async with self._session.post(self._rpc_url, json=body, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError(f"error del RPC en {method}: {(data or {}).get('error')}")
        return data.get("result")

    async def poll(self) -> list[WalletBuy]:
        """Compras nuevas de todas las wallets desde la vuelta anterior."""
        self.reload()
        active = {}
        for wallet, info in self._wallets.items():
            if self.banned(wallet):
                # Al acabar el castigo se empieza desde su última firma, sin
                # avisar de lo que compró mientras tanto.
                self._last_signature.pop(wallet, None)
            else:
                active[wallet] = info
        results = await asyncio.gather(
            *(self._poll_wallet(wallet, info) for wallet, info in active.items())
        )
        return [buy for buys in results for buy in buys]

    async def _poll_wallet(self, wallet: str, info: FollowedWallet) -> list[WalletBuy]:
        label = info.label
        first = wallet not in self._last_signature
        options: dict[str, object] = {
            "limit": 1 if first else MAX_SIGNATURES_PER_POLL,
            "commitment": COMMITMENT,
        }
        if not first and self._last_signature[wallet]:
            options["until"] = self._last_signature[wallet]
        try:
            entries = await self._rpc("getSignaturesForAddress", [wallet, options])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("No se pudieron consultar las transacciones de %s: %s", label, exc)
            return []
        if not isinstance(entries, list):
            return []
        if first:
            self._last_signature[wallet] = entries[0].get("signature") if entries else None
            return []
        if len(entries) == MAX_SIGNATURES_PER_POLL:
            log.debug("%s hizo más de %d transacciones desde la última vuelta", label, len(entries))

        buys: list[WalletBuy] = []
        # Vienen de la más nueva a la más vieja: se procesan en orden y se
        # avanza la marca solo hasta la última que se pudo leer, para
        # reintentar el resto en la próxima vuelta.
        for entry in reversed(entries):
            signature = entry.get("signature")
            if not isinstance(signature, str):
                continue
            if entry.get("err") is None:
                try:
                    tx = await self._rpc(
                        "getTransaction",
                        [signature, {
                            "encoding": "jsonParsed",
                            "commitment": COMMITMENT,
                            "maxSupportedTransactionVersion": MAX_TRANSACTION_VERSION,
                        }],
                    )
                    # None: todavía no está disponible en el nodo.
                    problem = "el RPC aún no la tiene" if tx is None else None
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    tx, problem = None, str(exc)
                if problem is not None:
                    attempts = self._attempts.get(signature, 0) + 1
                    if attempts < MAX_TX_ATTEMPTS:
                        self._attempts[signature] = attempts
                        log.debug("No se pudo leer la transacción %s de %s: %s", signature, label, problem)
                        break
                    log.warning(
                        "Se salta la transacción %s de %s tras %d intentos: %s",
                        signature, label, attempts, problem,
                    )
                self._attempts.pop(signature, None)
                if isinstance(tx, dict):
                    buys += parse_buys(tx, wallet, label, signature, self._min_sol, info.trusted)
            self._last_signature[wallet] = signature
        return buys
