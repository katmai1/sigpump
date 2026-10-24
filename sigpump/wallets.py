"""
sigpump/wallets.py

Seguimiento de wallets: consulta en el RPC de Solana las transacciones
nuevas de las wallets seguidas (las de wallet_list.WalletList) y detecta en
qué tokens entran.

Una compra se reconoce por los saldos antes y después de la transacción
(preTokenBalances/postTokenBalances y los lamports de la wallet), no por el
programa que la ejecutó: así vale igual para Raydium, Pump.fun, Jupiter,
Meteora o cualquier agregador, sin interpretar cada uno.

Con el WebSocket del RPC (logsSubscribe) solo se leen las transacciones que
avisa, directamente por su firma, en vez de consultar todas las wallets en
cada vuelta: el gasto de créditos pasa a depender de cuánto operan, no del
intervalo.
"""

import asyncio
import json
import logging
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.blacklist import ABANDONED_REASON, Blacklist
from sigpump.solana import RpcClient, redact
from sigpump.tracker import TrackRecord
from sigpump.wallet_list import FollowedWallet, WalletList

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
# Versión de transacción más alta que se acepta. Si llega una más nueva, el
# RPC da error y esa transacción se salta tras MAX_TX_ATTEMPTS intentos.
MAX_TRANSACTION_VERSION = 1
# Intentos de leer una transacción antes de saltarla. Sin tope, una que falla
# siempre dejaba la wallet atascada en ella sin ver nada de lo posterior.
MAX_TX_ATTEMPTS = 3
# "confirmed" llega ~10 s antes que "finalized"; revertir una confirmada es
# rarísimo y aquí solo se avisa, no se opera.
COMMITMENT = "confirmed"
# Firmas ya leídas (o descartadas) que se recuerdan, para que la pasada
# completa no vuelva a pedir las que ya llegaron por el WebSocket.
SEEN_SIGNATURES_MAX = 10_000
# Ping del WebSocket: Helius cierra las conexiones calladas al minuto.
WS_HEARTBEAT_SECONDS = 30
# Cada cuánto se revisan las suscripciones contra el fichero de wallets.
WS_SYNC_SECONDS = 5
# Espera máxima entre reconexiones (empieza en 1 s y se dobla).
WS_RECONNECT_MAX_SECONDS = 60
# Programas que aparecen en cualquier transferencia. Una transacción que solo
# invoca estos no puede ser una compra (no pasa por ningún DEX): son los
# airdrops de spam y los envíos de SOL o tokens, que con logsSubscribe llegan
# a montones y cada uno costaba dos o más consultas al RPC.
TRANSFER_PROGRAMS = {
    "11111111111111111111111111111111",  # System
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}
_INVOKE_RE = re.compile(r"^Program (\S+) invoke \[")
# Ventana del tope [wallets].max_txs_per_hour.
ACTIVITY_WINDOW_SECONDS = 3600


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
    avisado al que se sumaron wallets nuevas y `bonding_curve` uno que
    todavía no se graduó a un AMM. `transfer_fee_bps` es la comisión de
    transferencia del token (Token-2022), None si no se sabe.
    `track_record` es cómo le fue a la wallet en el registro (() sin tokens
    medidos, None sin registro)."""
    buy: WalletBuy
    others: tuple[str, ...] = field(default_factory=tuple)
    update: bool = False
    bonding_curve: bool = False
    transfer_fee_bps: int | None = None
    track_record: TrackRecord | None = None


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
    spam, transferencias) no cuenta. Si suben varios tokens en la misma
    transacción, lo pagado se reparte entre ellos a partes iguales: no se
    sabe cuánto fue a cada uno, y asignarlo entero a todos lo contaba dos veces.
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
    bought = [mint for mint, delta in deltas.items() if delta > 0 and mint not in QUOTE_MINTS]
    share = max(1, len(bought))
    ts = tx.get("blockTime") or time.time()
    return [
        WalletBuy(
            wallet=wallet,
            label=label,
            mint=mint,
            sol_spent=round(max(0.0, sol_spent) / share, 4),
            stable_spent=round(max(0.0, stable_spent) / share, 2),
            new_position=pre.get(mint, 0.0) <= 0,
            signature=signature,
            ts=float(ts),
            trusted=trusted,
        )
        for mint in bought
    ]


def only_transfers(logs: object) -> bool:
    """True si los logs de una transacción muestran que solo invoca programas
    de transferencia (no puede ser una compra). Sin logs, o truncados, no se
    puede saber y se devuelve False."""
    if not isinstance(logs, list) or not logs:
        return False
    programs = set()
    for line in logs:
        if not isinstance(line, str) or line.startswith("Log truncated"):
            return False
        if match := _INVOKE_RE.match(line):
            programs.add(match.group(1))
    return bool(programs) and programs <= TRANSFER_PROGRAMS


def ws_url(rpc_url: str) -> str:
    """URL del WebSocket de un RPC HTTP: la misma con wss:// (o ws://)."""
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if rpc_url.startswith(http):
            return ws + rpc_url.removeprefix(http)
    return rpc_url


class _Subscriptions:
    """Las suscripciones logsSubscribe de una conexión del WebSocket. Hay una
    por conexión: al reconectar, los ids de la anterior ya no valen."""

    def __init__(self) -> None:
        # Wallet -> id de su suscripción (None mientras no llega la respuesta).
        self._by_wallet: dict[str, int | None] = {}
        self._wallet_by_sub: dict[int, str] = {}
        # Id de petición -> wallet de las suscripciones pedidas.
        self._requests: dict[int, str] = {}
        self._next_id = 0

    async def sync(self, ws: aiohttp.ClientWebSocketResponse, wanted: set[str]) -> None:
        """Suscribe las wallets de `wanted` que faltan y quita las demás."""
        for wallet in wanted - self._by_wallet.keys():
            self._next_id += 1
            self._requests[self._next_id] = wallet
            self._by_wallet[wallet] = None
            await ws.send_json({
                "jsonrpc": "2.0", "id": self._next_id, "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": COMMITMENT}],
            })
        for wallet in self._by_wallet.keys() - wanted:
            sub = self._by_wallet.pop(wallet)
            if sub is None:
                continue
            self._wallet_by_sub.pop(sub, None)
            self._next_id += 1
            await ws.send_json({
                "jsonrpc": "2.0", "id": self._next_id, "method": "logsUnsubscribe", "params": [sub],
            })

    def take_request(self, request_id: object) -> str | None:
        """Wallet de la suscripción pedida con `request_id`, si es una."""
        return self._requests.pop(request_id, None) if isinstance(request_id, int) else None

    def confirm(self, wallet: str, sub: int) -> None:
        # Si mientras tanto se quitó del fichero, la respuesta ya no sirve.
        if wallet in self._by_wallet:
            self._by_wallet[wallet] = sub
            self._wallet_by_sub[sub] = wallet

    def wallet_of(self, sub: object) -> str | None:
        return self._wallet_by_sub.get(sub) if isinstance(sub, int) else None


class WalletWatcher:
    """Consulta las transacciones nuevas de las wallets de una WalletList y
    devuelve sus compras: por sondeo y, con listen(), por los avisos del
    WebSocket. Echa a la lista negra los bots y las abandonadas."""

    def __init__(
        self,
        rpc: RpcClient,
        path: Path,
        min_sol: float,
        blacklist: Blacklist | None = None,
        full_poll_seconds: float = 600.0,
        max_txs_per_hour: int = 0,
        max_inactive_seconds: float = 0.0,
    ):
        # Compartido con la comprobación de autoridades: el tope del RPC es por API key.
        self._rpc_client = rpc
        self._list = WalletList(path, blacklist)
        self._min_sol = min_sol
        # Wallet -> última firma procesada. Una wallet sin entrada todavía no
        # tiene punto de partida: su primera vuelta solo lo fija, para no
        # avisar de su historial al arrancar.
        self._last_signature: dict[str, str | None] = {}
        # Firma -> intentos fallidos de leerla.
        self._attempts: dict[str, int] = {}
        # Wallet -> firmas avisadas por el WebSocket pendientes de leer.
        self._pending: dict[str, list[str]] = {}
        # Firmas ya leídas o descartadas, por orden de llegada (las más
        # viejas se olvidan al pasar de SEEN_SIGNATURES_MAX).
        self._seen: dict[str, None] = {}
        # Con el WebSocket conectado solo se leen las avisadas, y todas las
        # wallets se consultan cada full_poll_seconds por si se perdió algún aviso.
        self._connected = False
        self._full_poll_seconds = full_poll_seconds
        self._next_full_poll = 0.0
        # Wallet -> firma -> hora de sus transacciones de la última hora, para
        # detectar los bots antes de gastar una consulta en cada una. 0 = sin tope.
        self._max_txs_per_hour = max_txs_per_hour
        self._activity: dict[str, dict[str, float]] = {}
        # Wallet -> hora de su última transacción conocida, para mandar a la
        # lista negra las abandonadas. 0 = no hacerlo.
        self._max_inactive_seconds = max_inactive_seconds
        self._last_active: dict[str, float] = {}
        # Suscripciones de la conexión actual del WebSocket.
        self._subs = _Subscriptions()

    @property
    def wallets(self) -> dict[str, FollowedWallet]:
        return self._list.wallets

    @property
    def connected(self) -> bool:
        """Si el WebSocket está conectado (con él solo se leen las avisadas)."""
        return self._connected

    def banned(self, address: str) -> bool:
        """True si `address` tiene una entrada temporal vigente en la lista negra."""
        return self._list.banned(address)

    def blacklist(self, address: str, reason: str, seconds: float | None = None) -> None:
        """Mete `address` en la lista negra y deja de seguirla ya. Con
        `seconds`, solo durante ese tiempo: sigue en el fichero de wallets y
        se vuelve a consultar al acabar. Lanza OSError si no se puede
        escribir la lista."""
        if not self._list.has_blacklist:
            return
        self._list.blacklist(address, reason, None if seconds is None else time.time() + seconds)
        self._last_signature.pop(address, None)
        self._activity.pop(address, None)
        self._last_active.pop(address, None)
        self._pending.pop(address, None)

    def ban(self, address: str, label: str, reason: str, seconds: float | None = None) -> bool:
        """blacklist() con su aviso en el log, con el motivo "etiqueta: motivo".
        True si la metió; si no se pudo escribir la lista, lo avisa y False."""
        try:
            self.blacklist(address, f"{label}: {reason}", seconds)
        except OSError as exc:
            log.warning("No se pudo añadir %s a la lista negra: %s", label, exc)
            return False
        log.warning("Wallet %s a la lista negra: %s", label, reason)
        return True

    def _blacklist_bot(self, wallet: str, signatures: list[tuple[str, float]]) -> bool:
        """Apunta las transacciones (firma, hora) de `wallet` y, si en la
        última hora pasa de max_txs_per_hour, la mete en la lista negra: es un
        bot, y leer cada una de sus transacciones agota los créditos del RPC.
        True si la quitó."""
        if not self._max_txs_per_hour or not self._list.has_blacklist or wallet not in self.wallets:
            return False
        recent = self._activity.setdefault(wallet, {})
        for signature, ts in signatures:
            recent.setdefault(signature, ts)
        cutoff = time.time() - ACTIVITY_WINDOW_SECONDS
        for signature in [s for s, ts in recent.items() if ts < cutoff]:
            del recent[signature]
        if len(recent) <= self._max_txs_per_hour:
            return False
        return self.ban(wallet, self.wallets[wallet].label, f"bot, {len(recent)} tx en 1h")

    def _blacklist_abandoned(self, wallet: str) -> bool:
        """Mete `wallet` en la lista negra si lleva más de max_inactive_seconds
        sin ninguna transacción: está abandonada y solo gasta consultas. Es
        por otros max_inactive_seconds: al acabar se vuelve a consultar y,
        si sigue sin operar, vuelve a entrar. True si la quitó."""
        last = self._last_active.get(wallet)
        if not self._max_inactive_seconds or not self._list.has_blacklist or last is None:
            return False
        idle = time.time() - last
        if idle <= self._max_inactive_seconds or wallet not in self.wallets:
            return False
        label = self.wallets[wallet].label
        reason = f"{ABANDONED_REASON}, {idle / 86400:.0f} días sin actividad"
        if not self.ban(wallet, label, reason, seconds=self._max_inactive_seconds):
            return False
        log.info("%s se vuelve a comprobar en %.0f días", label, self._max_inactive_seconds / 86400)
        return True

    def reload(self) -> None:
        """Relee la lista de wallets y olvida lo que se sabía de las que ya no están."""
        self._list.reload()
        for gone in set(self._last_signature) - set(self.wallets):
            del self._last_signature[gone]
        for gone in set(self._last_active) - set(self.wallets):
            del self._last_active[gone]

    async def _rpc(self, method: str, params: list) -> object:
        """Resultado de una llamada al RPC. Lanza ValueError si el RPC
        devuelve un error, y los de red de aiohttp tal cual."""
        return await self._rpc_client.call(method, params)

    async def poll(self) -> list[WalletBuy]:
        """Compras nuevas desde la vuelta anterior: las de las transacciones
        avisadas por el WebSocket y, sin él o cada full_poll_seconds, las de
        todas las wallets."""
        self.reload()
        active = {}
        for wallet, info in self.wallets.items():
            if self.banned(wallet):
                # Al acabar el castigo se empieza desde su última firma, sin
                # avisar de lo que compró mientras tanto.
                self._last_signature.pop(wallet, None)
            else:
                active[wallet] = info
        buys: list[WalletBuy] = []
        now = time.monotonic()
        if not self._connected or now >= self._next_full_poll:
            if self._connected:
                self._next_full_poll = now + self._full_poll_seconds
            for wallet_buys in await asyncio.gather(
                *(self._poll_wallet(wallet, info) for wallet, info in active.items())
            ):
                buys += wallet_buys
        # Se limpia después de la pasada completa: mientras corre, el
        # WebSocket puede apuntar firmas de una wallet que esa misma pasada
        # echó por bot, y leerlas tiraba un KeyError que perdía las compras
        # de toda la vuelta.
        for gone in [w for w in self._pending if w not in self.wallets or self.banned(w)]:
            del self._pending[gone]
        # Después de la pasada completa: las que ya leyó se saltan.
        for wallet_buys in await asyncio.gather(
            *(self._read_pending(wallet, self.wallets[wallet]) for wallet in list(self._pending))
        ):
            buys += wallet_buys
        return buys

    def _mark_seen(self, signature: str) -> None:
        self._seen[signature] = None
        while len(self._seen) > SEEN_SIGNATURES_MAX:
            del self._seen[next(iter(self._seen))]

    async def _transaction(self, signature: str, label: str) -> tuple[dict | None, bool]:
        """La transacción `signature` (None si no se pudo leer) y si conviene
        reintentarla en la próxima vuelta. Tras MAX_TX_ATTEMPTS fallos se
        salta: sin tope, una que falla siempre dejaba atascada a la wallet."""
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
                return None, True
            log.warning(
                "Se salta la transacción %s de %s tras %d intentos: %s",
                signature, label, attempts, problem,
            )
        self._attempts.pop(signature, None)
        self._mark_seen(signature)
        return (tx if isinstance(tx, dict) else None), False

    async def _read_pending(self, wallet: str, info: FollowedWallet) -> list[WalletBuy]:
        """Compras de las transacciones de `wallet` avisadas por el WebSocket.
        Las que el RPC aún no tiene se dejan para la próxima vuelta."""
        signatures = self._pending.pop(wallet, [])
        buys: list[WalletBuy] = []
        for i, signature in enumerate(signatures):
            if signature in self._seen:
                continue
            tx, retry = await self._transaction(signature, info.label)
            if retry:
                rest = signatures[i:]
                # Mientras se leía pueden haber llegado avisos nuevos.
                self._pending[wallet] = rest + [s for s in self._pending.get(wallet, []) if s not in rest]
                break
            if tx is not None:
                buys += parse_buys(tx, wallet, info.label, signature, self._min_sol, info.trusted)
        return buys

    async def _poll_wallet(self, wallet: str, info: FollowedWallet) -> list[WalletBuy]:
        """Compras de `wallet` desde su última firma."""
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
        now = time.time()
        activity = [
            (entry["signature"], entry.get("blockTime") or now)
            for entry in entries if isinstance(entry.get("signature"), str)
        ]
        # Una wallet sin historial cuenta desde que se empieza a seguir.
        self._last_active[wallet] = max(
            [ts for _, ts in activity] + [self._last_active.get(wallet, 0.0 if activity else now)]
        )
        if self._blacklist_abandoned(wallet):
            return []
        if first:
            self._last_signature[wallet] = entries[0].get("signature") if entries else None
            return []
        if self._blacklist_bot(wallet, activity):
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
            if entry.get("err") is None and signature not in self._seen:
                tx, retry = await self._transaction(signature, label)
                if retry:
                    break
                if tx is not None:
                    buys += parse_buys(tx, wallet, label, signature, self._min_sol, info.trusted)
            self._last_signature[wallet] = signature
        return buys

    async def listen(self, url: str) -> None:
        """Mantiene una suscripción logsSubscribe por wallet en el WebSocket
        `url` y apunta las firmas de sus transacciones para leerlas en la
        próxima vuelta de poll(). Se reconecta sola; mientras está caído, poll() consulta todas."""
        delay = 1.0
        while True:
            started = time.monotonic()
            try:
                async with self._rpc_client.session.ws_connect(url, heartbeat=WS_HEARTBEAT_SECONDS) as ws:
                    log.info("WebSocket de wallets conectado")
                    await self._serve(ws)
                log.warning("El WebSocket de wallets se cerró")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                # La URL lleva la API key: no se escribe en el log.
                log.warning("WebSocket de wallets caído: %s", redact(exc) or type(exc).__name__)
            except Exception:
                # Un mensaje con una forma inesperada no debe tumbar el bot:
                # el gather() de radar.run() pararía todo el proceso.
                log.error("Error inesperado en el WebSocket de wallets:\n%s", redact(traceback.format_exc()))
            finally:
                self._connected = False
            if time.monotonic() - started > WS_RECONNECT_MAX_SECONDS:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX_SECONDS)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Atiende una conexión hasta que se cierra."""
        self._subs = _Subscriptions()
        self._connected = True
        # Lo que pasó mientras estaba caído no llega por aquí: pasada completa.
        self._next_full_poll = 0.0
        while True:
            await self.sync_subscriptions(ws)
            try:
                msg = await ws.receive(timeout=WS_SYNC_SECONDS)
            except asyncio.TimeoutError:
                continue
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.handle_message(json.loads(msg.data))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    async def sync_subscriptions(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Suscribe las wallets nuevas del fichero y quita las que ya no están
        o tienen una entrada temporal en la lista negra (no se consultan)."""
        await self._subs.sync(ws, {wallet for wallet in self.wallets if not self.banned(wallet)})

    def handle_message(self, data: object) -> None:
        """Respuesta a una suscripción o aviso de una transacción."""
        if not isinstance(data, dict):
            return
        wallet = self._subs.take_request(data.get("id"))
        if wallet is not None:
            sub = data.get("result")
            if data.get("error") or not isinstance(sub, int):
                # Se queda sin suscripción: solo la cubre la pasada completa.
                info = self.wallets.get(wallet)
                log.warning(
                    "No se pudo suscribir a %s en el WebSocket: %s",
                    info.label if info else wallet, data.get("error"),
                )
            else:
                self._subs.confirm(wallet, sub)
            return
        if data.get("method") != "logsNotification":
            return
        params = data.get("params") or {}
        wallet = self._subs.wallet_of(params.get("subscription"))
        value = (params.get("result") or {}).get("value") or {}
        signature = value.get("signature")
        if wallet is None or not isinstance(signature, str) or signature in self._seen:
            return
        # Una wallet ya quitada (o castigada) sigue suscrita hasta la próxima
        # sincronización: sus avisos no se apuntan.
        if wallet not in self.wallets or self.banned(wallet):
            return
        self._last_active[wallet] = time.time()
        # Se cuentan todas, también las que no se van a leer: un bot se nota
        # por lo que opera, y cuanto antes se corte menos créditos gasta.
        if self._blacklist_bot(wallet, [(signature, time.time())]):
            return
        # Una transacción fallida o una simple transferencia no compra nada:
        # no merece la consulta, ni ahora ni en la pasada completa.
        if value.get("err") is not None or only_transfers(value.get("logs")):
            self._mark_seen(signature)
            return
        pending = self._pending.setdefault(wallet, [])
        if signature not in pending:
            pending.append(signature)
