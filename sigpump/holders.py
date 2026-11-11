"""
sigpump/holders.py

Cuánto del supply de un token tienen sus mayores holders y su creador (el
dev) en el momento de la compra. Por ahora solo se registra, no filtra: un
dev o un puñado de wallets con mucho supply pueden vender de golpe, pero en
memecoins recién lanzadas es lo normal, y antes de descartar nada hay que
medir si esos tokens dan peor (2026-11-11: no había ningún dato guardado).

Los holders se sacan de getTokenLargestAccounts (las 20 mayores cuentas del
token), agrupadas por dueño y sin las que no son de una persona: el pool del
AMM, la bonding curve, lockers (cuentas de un programa), las autoridades de
Raydium y lo quemado. Si no, el pool solo ya ponía el top 10 en un 80-90%.

El creador es quien pagó la primera transacción del mint, buscada hacia
atrás en su historial de firmas, como mucho `creator_max_pages` páginas de
1000: un token viejo y con mucho volumen tiene cientos de miles y buscar
más gastaría demasiados créditos (entonces dev_pct queda en NULL).

Llamadas al RPC por token (1 crédito cada una en Helius): 3 para los
holders, 1 por página de firmas y 1 por la transacción de creación (solo
la primera vez: el creador no cambia y se recuerda) y 1 para el saldo del
dev si no está entre los 20 mayores.
"""

import asyncio
import dataclasses
import logging
import time

import aiohttp  # type: ignore[import-not-found]

from sigpump.solana import RpcClient, redact

log = logging.getLogger()

SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Dueños de cuentas del token que no son de nadie aunque sean del System
# Program o no existan como cuenta: las autoridades de los pools de Raydium
# (AMM v4 y CPMM) y el incinerador, adonde se mandan los tokens quemados.
EXCLUDED_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
    "1nc1nerator11111111111111111111111111111111",
}
TOP_HOLDERS = 10
# Firmas por página de getSignaturesForAddress (el máximo que acepta).
SIGNATURES_PAGE = 1000
# Varias wallets seguidas compran el mismo token en pocos segundos: se
# reutiliza la medida en vez de repetir las llamadas.
HOLDINGS_TTL_SECONDS = 60
_RPC_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError)


@dataclasses.dataclass(frozen=True)
class Holdings:
    """Porcentajes del supply (0-100) al comprar. None = no se pudo medir."""
    top10_pct: float | None = None
    max_holder_pct: float | None = None
    dev_wallet: str | None = None
    dev_pct: float | None = None


def _parsed_info(account: object) -> dict | None:
    """El `info` de una cuenta pedida con jsonParsed, o None."""
    data = account.get("data") if isinstance(account, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    return info if isinstance(info, dict) else None


def _raw_amount(info: dict) -> int:
    """Cantidad en unidades mínimas de una token account parseada."""
    try:
        return int((info.get("tokenAmount") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0


class TokenHolders:
    """Concentración del supply por token. Recuerda el creador de cada mint
    para siempre y la medida, HOLDINGS_TTL_SECONDS."""

    def __init__(self, rpc: RpcClient, creator_max_pages: int = 3):
        self._rpc = rpc
        self._creator_max_pages = creator_max_pages
        # mint -> creador; None = no se encontró en las páginas permitidas
        # (un token solo envejece: tampoco se encontraría después).
        self._creators: dict[str, str | None] = {}
        self._cache: dict[str, tuple[float, Holdings]] = {}

    async def holdings(self, mints: list[str]) -> dict[str, Holdings]:
        """Holdings de cada mint. Los que fallan quedan con None en lo que no
        se pudo medir: el aviso no espera a esto ni se pierde por esto."""
        wanted = [m for m in dict.fromkeys(mints) if m]
        results = await asyncio.gather(*(self._holdings(m) for m in wanted))
        return dict(zip(wanted, results))

    async def _holdings(self, mint: str) -> Holdings:
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < HOLDINGS_TTL_SECONDS:
            return cached[1]
        try:
            supply, by_owner = await self._largest_owners(mint)
        except _RPC_ERRORS as exc:
            log.warning("No se pudieron leer los holders de %s: %s", mint, redact(exc))
            supply, by_owner = 0, None
        top10 = max_holder = None
        if supply > 0 and by_owner is not None:
            people = sorted((a for o, a in by_owner.items() if o is not None), reverse=True)
            top10 = sum(people[:TOP_HOLDERS]) / supply * 100
            max_holder = (people[0] / supply * 100) if people else 0.0
        dev = dev_pct = None
        try:
            dev = await self._creator(mint)
            if dev and supply > 0:
                amount = (by_owner or {}).get(dev)
                if amount is None:
                    amount = await self._owner_amount(dev, mint)
                dev_pct = amount / supply * 100
        except _RPC_ERRORS as exc:
            log.warning("No se pudo leer el creador de %s: %s", mint, redact(exc))
        result = Holdings(top10, max_holder, dev, dev_pct)
        self._cache[mint] = (time.time(), result)
        return result

    async def _largest_owners(self, mint: str) -> tuple[int, dict[str | None, int] | None]:
        """(supply en unidades mínimas, {dueño: cantidad}) de las 20 mayores
        cuentas. Los dueños que no son personas van todos bajo la clave None,
        para que dev_pct no los confunda con un dueño sin medir."""
        largest = await self._rpc.call("getTokenLargestAccounts", [mint])
        entries = largest.get("value") if isinstance(largest, dict) else None
        if not isinstance(entries, list):
            raise ValueError("respuesta inesperada de getTokenLargestAccounts")
        addresses = [e["address"] for e in entries if isinstance(e, dict) and e.get("address")]
        # El mint va en la misma llamada que las token accounts: da el supply
        # sin una getTokenSupply aparte.
        accounts = await self._rpc.call("getMultipleAccounts", [addresses + [mint], {"encoding": "jsonParsed"}])
        values = accounts.get("value") if isinstance(accounts, dict) else None
        if not isinstance(values, list) or len(values) != len(addresses) + 1:
            raise ValueError("respuesta inesperada de getMultipleAccounts")
        mint_info = _parsed_info(values[-1]) or {}
        try:
            supply = int(mint_info.get("supply") or 0)
        except (TypeError, ValueError):
            supply = 0
        amounts: dict[str, int] = {}
        for account in values[:-1]:
            info = _parsed_info(account)
            if info and info.get("owner"):
                amounts[info["owner"]] = amounts.get(info["owner"], 0) + _raw_amount(info)
        if not amounts:
            return supply, {}
        owners = list(amounts)
        # Sin datos (dataSlice de 0 bytes): solo interesa de qué programa es cada dueño.
        info = await self._rpc.call(
            "getMultipleAccounts", [owners, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]
        )
        programs = info.get("value") if isinstance(info, dict) else None
        if not isinstance(programs, list) or len(programs) != len(owners):
            raise ValueError("respuesta inesperada de getMultipleAccounts")
        by_owner: dict[str | None, int] = {}
        for owner, account in zip(owners, programs):
            # Una wallet es una cuenta del System Program. La de un programa
            # (pool, curva, locker) no es de nadie, y una que no existe es una
            # autoridad derivada sin SOL (las de Meteora): tampoco.
            person = (
                isinstance(account, dict) and account.get("owner") == SYSTEM_PROGRAM
                and owner not in EXCLUDED_OWNERS
            )
            key = owner if person else None
            by_owner[key] = by_owner.get(key, 0) + amounts[owner]
        return supply, by_owner

    async def _owner_amount(self, owner: str, mint: str) -> int:
        """Cuánto del token tiene `owner` sumando todas sus cuentas."""
        result = await self._rpc.call(
            "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]
        )
        accounts = result.get("value") if isinstance(result, dict) else None
        if not isinstance(accounts, list):
            raise ValueError("respuesta inesperada de getTokenAccountsByOwner")
        return sum(_raw_amount(_parsed_info(a.get("account")) or {}) for a in accounts if isinstance(a, dict))

    async def _creator(self, mint: str) -> str | None:
        """Quien pagó la primera transacción del mint, o None si queda más
        atrás de creator_max_pages páginas. Lanza los errores del RPC (no
        se recuerda nada: se reintenta en la próxima compra)."""
        if mint in self._creators:
            return self._creators[mint]
        oldest = None
        complete = False
        for _ in range(self._creator_max_pages):
            options: dict[str, object] = {"limit": SIGNATURES_PAGE}
            if oldest:
                options["before"] = oldest
            page = await self._rpc.call("getSignaturesForAddress", [mint, options])
            if not isinstance(page, list):
                raise ValueError("respuesta inesperada de getSignaturesForAddress")
            if page:
                oldest = page[-1].get("signature")
            if len(page) < SIGNATURES_PAGE:
                complete = True
                break
        creator = None
        if complete and oldest:
            tx = await self._rpc.call("getTransaction", [oldest, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
            keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") if isinstance(tx, dict) else None
            if not isinstance(keys, list) or not keys:
                raise ValueError("respuesta inesperada de getTransaction")
            creator = keys[0] if isinstance(keys[0], str) else keys[0].get("pubkey")
        elif not complete:
            log.debug("Creador de %s más atrás de %d páginas de firmas", mint, self._creator_max_pages)
        self._creators[mint] = creator
        return creator
