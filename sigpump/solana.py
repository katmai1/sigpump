"""
sigpump/solana.py

Comprueba en la blockchain (RPC de Solana) si el mint de un token conserva
autoridades peligrosas: mintAuthority (pueden acuñar más y diluir a los que
compraron) o freezeAuthority (pueden congelar tus tokens y dejarte sin poder
vender). Las autoridades solo se pueden renunciar, nunca volver a poner, así
que un token limpio se recuerda para siempre y uno con autoridades se vuelve
a mirar cada tanto por si las renunció. De paso lee la comisión de
transferencia de los Token-2022, que se cobra al comprar y al vender.

También tiene lo común a todas las llamadas al RPC (RpcClient): el ritmo
máximo, los reintentos ante 429/5xx y quitar la API key de lo que se
escribe en el log.
"""

import asyncio
import dataclasses
import logging
import re
import time

import aiohttp  # type: ignore[import-not-found]

from sigpump.util import RETRYABLE_STATUSES

log = logging.getLogger()

MAINNET_RPC_URL = "https://api.mainnet-beta.solana.com"
# getMultipleAccounts acepta como máximo 100 cuentas por llamada.
MAX_ACCOUNTS_PER_CALL = 100
REQUEST_TIMEOUT_SECONDS = 15
# Ritmo máximo de peticiones al RPC, por proceso: el seguimiento de wallets y
# la comprobación de autoridades comparten limitador. El plan gratuito de
# Helius corta a 10 por segundo: con el radar y buyers.py a la vez son 5 + 5,
# y los 429 sueltos se reintentan.
MAX_REQUESTS_PER_SECOND = 5
# Un token con autoridades puede renunciarlas después: se vuelve a consultar
# pasado este tiempo. Uno limpio ya no puede volver atrás y no caduca.
UNSAFE_TTL_SECONDS = 30 * 60
# Reintentos ante 429/5xx del RPC, esperando 1 s, 2 s, 4 s...
RPC_MAX_RETRIES = 3
RPC_RETRY_BACKOFF_SECONDS = 1.0
_API_KEY_RE = re.compile(r"(api[-_]key=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: object) -> str:
    """`text` sin la API key que lleve la URL del RPC (Helius la pone en la
    query y aiohttp copia la URL entera en sus errores)."""
    return _API_KEY_RE.sub(r"\1***", str(text))


class RateLimiter:
    """Deja pasar como mucho `per_second` peticiones por segundo."""

    def __init__(self, per_second: float):
        self._interval = 1 / per_second
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self._interval


async def rpc_call(
    session: aiohttp.ClientSession,
    url: str,
    method: str,
    params: list,
    limiter: RateLimiter,
    timeout_seconds: float,
) -> object:
    """Resultado de una llamada JSON-RPC. Reintenta los 429 y 5xx, cada vez
    pasando por `limiter`. Lanza ValueError si el RPC devuelve un error o un
    HTTP de error (sin la URL, que lleva la API key), y los de red de aiohttp
    tal cual."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    for attempt in range(RPC_MAX_RETRIES + 1):
        await limiter.wait()
        async with session.post(url, json=body, timeout=timeout) as resp:
            if resp.status not in RETRYABLE_STATUSES or attempt == RPC_MAX_RETRIES:
                if resp.status >= 400:
                    raise ValueError(f"HTTP {resp.status} en {method}")
                data = await resp.json(content_type=None)
                break
        wait = RPC_RETRY_BACKOFF_SECONDS * (2**attempt)
        log.debug("RPC HTTP %s en %s, reintentando en %.1fs", resp.status, method, wait)
        await asyncio.sleep(wait)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError(f"error del RPC en {method}: {(data or {}).get('error')}")
    return data.get("result")


class RpcClient:
    """Llamadas JSON-RPC a un nodo de Solana. Lo comparten el seguimiento de
    wallets y la comprobación de autoridades: el tope de peticiones es por
    API key, y cada uno con su limitador lo pasaban entre los dos."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str = MAINNET_RPC_URL,
        limiter: RateLimiter | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ):
        # La sesión queda a la vista para el WebSocket del mismo nodo.
        self.session = session
        self.url = url
        self._limiter = limiter or RateLimiter(MAX_REQUESTS_PER_SECOND)
        self._timeout_seconds = timeout_seconds
        # Llamadas hechas (sin contar reintentos), para el resumen del log y buyers.py.
        self.calls = 0

    async def call(self, method: str, params: list) -> object:
        """Resultado de la llamada. Lanza ValueError si el RPC devuelve un
        error, y los de red de aiohttp tal cual."""
        self.calls += 1
        return await rpc_call(self.session, self.url, method, params, self._limiter, self._timeout_seconds)


@dataclasses.dataclass(frozen=True)
class MintCheck:
    """Lo que se sabe del mint de un token: por qué es peligroso (None si
    está limpio), su comisión de transferencia en puntos básicos (None si
    no tiene la extensión) y cuándo se consultó."""
    reason: str | None
    transfer_fee_bps: int | None
    checked_at: float


def _transfer_fee_bps(info: dict) -> int | None:
    """Comisión de transferencia de un mint Token-2022 (puntos básicos), o
    None si no tiene la extensión. Se toma la mayor entre la vigente y la
    programada: saber cuál rige pide consultar la época actual, y el aviso
    es para no subestimarla."""
    for extension in info.get("extensions") or []:
        if not isinstance(extension, dict) or extension.get("extension") != "transferFeeConfig":
            continue
        state = extension.get("state") or {}
        fees = [
            (state.get(key) or {}).get("transferFeeBasisPoints") for key in ("olderTransferFee", "newerTransferFee")
        ]
        return max((int(f) for f in fees if isinstance(f, int)), default=0)
    return None


class TokenAuthorities:
    """Autoridades y comisión del mint de cada token, cacheadas entre consultas."""

    def __init__(self, rpc: RpcClient):
        self._rpc = rpc
        self._cache: dict[str, MintCheck] = {}

    def _cached(self, address: str) -> MintCheck | None:
        """Lo que se sabe del token, o None si hay que consultarlo."""
        entry = self._cache.get(address)
        if entry is None:
            return None
        # Las autoridades se pueden renunciar y la comisión, cambiar: esos se
        # vuelven a mirar. Un token limpio y sin comisión no puede cambiar.
        mutable = entry.reason is not None or entry.transfer_fee_bps is not None
        if mutable and time.time() - entry.checked_at > UNSAFE_TTL_SECONDS:
            return None
        return entry

    def transfer_fee_bps(self, address: str) -> int | None:
        """Comisión de transferencia del token según la última consulta: 0 si
        no tiene, None si no se pudo consultar. Se llama después de
        unsafe_reasons(), que es la que consulta al RPC."""
        entry = self._cache.get(address)
        if entry is None:
            return None
        return entry.transfer_fee_bps or 0

    async def unsafe_reasons(self, addresses: list[str]) -> dict[str, str]:
        """
        {dirección: motivo} de los tokens que se pueden acuñar o congelar. Los
        que no se pudieron consultar no figuran: es preferible dejar pasar una
        alerta a perderlas todas porque el RPC esté caído.
        """
        wanted = list(dict.fromkeys(addresses))
        pending = [a for a in wanted if self._cached(a) is None]
        for i in range(0, len(pending), MAX_ACCOUNTS_PER_CALL):
            await self._fetch(pending[i : i + MAX_ACCOUNTS_PER_CALL])
        reasons = {}
        for address in wanted:
            entry = self._cached(address)
            if entry is not None and entry.reason is not None:
                reasons[address] = entry.reason
        return reasons

    async def _fetch(self, addresses: list[str]) -> None:
        """Consulta un lote de mints y guarda lo que devuelva. Ante cualquier
        fallo no cachea nada: se reintenta en la próxima señal."""
        try:
            # Por rpc_call y no un POST suelto: sin sus reintentos, un 429
            # dejaba el lote sin comprobar y el aviso salía como limpio.
            result = await self._rpc.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("No se pudieron consultar las autoridades en %s: %s", redact(self._rpc.url), redact(exc))
            return
        accounts = result.get("value") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or len(accounts) != len(addresses):
            log.warning("Respuesta inesperada del RPC de Solana para %d mints", len(addresses))
            return

        now = time.time()
        for address, account in zip(addresses, accounts):
            # Con jsonParsed, una cuenta que el RPC no sabe parsear vuelve como
            # texto en base64 en vez de como diccionario.
            data = (account or {}).get("data")
            parsed = data.get("parsed") if isinstance(data, dict) else None
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if not isinstance(info, dict):
                # Cuenta inexistente, o que no es un mint (otra chain, token raro):
                # no se sabe nada, así que no se cachea ni se bloquea.
                continue
            problems = []
            if info.get("mintAuthority"):
                problems.append("se pueden acuñar más")
            if info.get("freezeAuthority"):
                problems.append("se pueden congelar")
            reason = "el creador conserva autoridades: " + " y ".join(problems) if problems else None
            self._cache[address] = MintCheck(reason, _transfer_fee_bps(info), now)
