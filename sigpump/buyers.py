"""
sigpump/buyers.py

Wallets que compraron un token en los últimos minutos gastando al menos un
mínimo de SOL por transacción. Se ejecuta a mano con el mint del token:

    python -m sigpump.buyers <MINT> [--minutes 10] [--min-sol 2]
    python -m sigpump.buyers <MINT> --from 16:30 --to 16:35

Usa el [solana].rpc_url de config.toml. Las transacciones se piden al mint y
a sus pools de DexScreener (los swaps de Raydium AMM v4 no incluyen el mint
entre sus cuentas) y se leen enteras para ver los saldos: el gasto de créditos
crece con el volumen del token en la ventana, por eso hay un tope. Con Helius
se leen por páginas con getTransactionsForAddress (--method helius, ~10 veces
más barato); en otro RPC, una a una (--method rpc).

Después se mira el historial de firmas de cada comprador (1 crédito por cada
1000) para descartar las wallets nuevas y los bots, y dar la fecha de su
primera transacción.

Piezas:
- Rpc: llamadas al RPC con ritmo máximo y cuenta de créditos.
- TransactionReader: lee las transacciones de la ventana (helius o rpc).
- BuyerFinder: saca de ellas las compras del token y las suma por wallet.
- HistoryFilter: revisa el historial de cada comprador y descarta nuevas y bots.
- Report: imprime el resultado.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config
from sigpump.screener import DexScreenerClient
from sigpump.solana import RateLimiter, rpc_call
from sigpump.wallets import COMMITMENT, MAX_REQUESTS_PER_SECOND, MAX_TRANSACTION_VERSION, parse_buys

log = logging.getLogger()

DEFAULT_MINUTES = 20.0
DEFAULT_MIN_SOL = 2.0
# Transacciones leídas como máximo: un token con mucho volumen hace miles en
# 10 minutos, y cada una cuesta 0.1 créditos con "helius" o 1 con "rpc".
DEFAULT_MAX_TXS = 5000
# Máximo que acepta getSignaturesForAddress por llamada.
SIGNATURES_PAGE = 1000
REQUEST_TIMEOUT_SECONDS = 30
# Formas de leer las transacciones del token:
# - "helius": getTransactionsForAddress (método propio de Helius), que las
#   devuelve completas por páginas: 10 créditos por cada 100.
# - "rpc": getSignaturesForAddress + un getTransaction por firma, estándar en
#   cualquier RPC de Solana: 1 crédito por transacción.
METHODS = ("helius", "rpc")
DEFAULT_METHOD = "helius"
# Transacciones completas por página de getTransactionsForAddress. Admite
# 1000, pero el coste es por transacción y páginas tan grandes rozan el timeout.
HELIUS_PAGE = 100
HELIUS_CREDITS_PER_100_TXS = 10

# Filtros del historial de cada comprador.
# Menos transacciones que esto en toda su vida: wallet nueva o de usar y tirar.
DEFAULT_MIN_HISTORY = 20
# Más transacciones al día que esto (medido en sus últimas firmas): un bot.
DEFAULT_MAX_TXS_PER_DAY = 1000
# Más % de transacciones fallidas que esto: un bot (los snipers mandan
# muchas a la vez y casi todas fallan).
DEFAULT_MAX_FAILED_PCT = 50.0
# Páginas de 1000 firmas que se leen hacia atrás para llegar a la primera
# transacción (1 crédito cada una). Más allá, la fecha queda como "antes de".
DEFAULT_HISTORY_PAGES = 5
# Firmas mínimas para juzgar el ritmo o las fallidas: con pocas no es fiable.
MIN_TXS_FOR_BOT_CHECK = 100

# Errores de una llamada al RPC que no deben tumbar el script.
RPC_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError)


@dataclass
class Buyer:
    """Compras de una wallet en el token dentro de la ventana, y su historial."""
    wallet: str
    sol_spent: float = 0.0
    buys: int = 0
    last_ts: float = 0.0
    # Transacciones vistas en su historial (hasta history_pages páginas).
    history_txs: int = 0
    # Fecha de la transacción más antigua vista: la de creación si
    # history_complete, si no solo se sabe que es anterior.
    first_ts: float | None = None
    history_complete: bool = False

    @property
    def history_text(self) -> str:
        """Transacciones del historial, con "+" si hay más sin leer."""
        return f"{self.history_txs}{'' if self.history_complete else '+'} tx"

    @property
    def since_text(self) -> str:
        """Fecha de la primera transacción, o "antes de" si no se llegó a ella."""
        if self.first_ts is None:
            return "?"
        date = time.strftime("%Y-%m-%d", time.localtime(self.first_ts))
        return date if self.history_complete else f"antes de {date}"


class Rpc:
    """Cliente mínimo del RPC con el mismo ritmo máximo que el seguimiento de
    wallets, y que cuenta las llamadas y los créditos de Helius gastados."""

    def __init__(self, session: aiohttp.ClientSession, url: str):
        self._session = session
        self._url = url
        self._limiter = RateLimiter(MAX_REQUESTS_PER_SECOND)
        self.calls = 0
        self.credits = 0

    async def call(self, method: str, params: list) -> object:
        """Resultado de la llamada. Lanza ValueError si el RPC devuelve un
        error, y los de red de aiohttp tal cual."""
        self.calls += 1
        result = await rpc_call(self._session, self._url, method, params, self._limiter, REQUEST_TIMEOUT_SECONDS)
        self.credits += self._credits(method, result)
        return result

    @staticmethod
    def _credits(method: str, result: object) -> int:
        """Créditos de Helius de una llamada: 1, salvo getTransactionsForAddress,
        que cobra 10 por cada 100 transacciones devueltas (redondeando arriba)."""
        if method != "getTransactionsForAddress":
            return 1
        returned = len(result.get("data") or []) if isinstance(result, dict) else 0
        return HELIUS_CREDITS_PER_100_TXS * max(1, -(-returned // 100))


def tx_signature(tx: dict) -> str | None:
    """Firma de la transacción."""
    signatures = (tx.get("transaction") or {}).get("signatures") or []
    return signatures[0] if signatures and isinstance(signatures[0], str) else None


def tx_fee_payer(tx: dict) -> str | None:
    """Primera cuenta de la transacción: la que firma y paga la comisión."""
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return None
    key = keys[0]
    return key.get("pubkey") if isinstance(key, dict) else key


class TransactionReader:
    """Lee las transacciones correctas de unas direcciones en una ventana de
    tiempo, de la más nueva a la más vieja y como mucho `max_txs`."""

    def __init__(self, rpc: Rpc, max_txs: int = DEFAULT_MAX_TXS, method: str = DEFAULT_METHOD):
        self._rpc = rpc
        self._max_txs = max_txs
        self._method = method

    async def read(
        self, addresses: list[str], since: float, until: float | None = None
    ) -> list[dict | None]:
        """Las transacciones de la ventana [since, until] de todas las
        direcciones, sin repetir. None en las que no se pudieron leer.
        until None = hasta ahora."""
        if self._method == "helius":
            try:
                return await self._read_helius(addresses, since, until)
            except RPC_ERRORS as exc:
                # Un RPC que no es de Helius, un plan sin el método o un fallo
                # a mitad: se repite con los métodos estándar.
                log.warning("getTransactionsForAddress falló (%s), se usa --method rpc", exc)
        return await self._read_rpc(addresses, since, until)

    def _warn_truncated(self, total: int | None = None) -> None:
        found = f"{total}" if total is not None else f"Más de {self._max_txs}"
        log.warning(
            "%s transacciones en la ventana, solo se leen las %d más recientes (--max-txs)",
            found, self._max_txs,
        )

    # --- helius: getTransactionsForAddress ---

    async def _read_helius(
        self, addresses: list[str], since: float, until: float | None
    ) -> list[dict | None]:
        by_signature: dict[str, dict] = {}
        truncated = False
        for address in addresses:
            txs, more = await self._helius_pages(address, since, until)
            truncated |= more
            for tx in txs:
                if (signature := tx_signature(tx)) is not None:
                    by_signature[signature] = tx
        newest = sorted(by_signature.values(), key=lambda tx: tx.get("blockTime") or 0, reverse=True)
        if truncated or len(newest) > self._max_txs:
            self._warn_truncated()
        newest = newest[: self._max_txs]
        log.info("Leídas %d transacciones", len(newest))
        return list(newest)

    async def _helius_pages(
        self, address: str, since: float, until: float | None
    ) -> tuple[list[dict], bool]:
        """Transacciones de `address` en la ventana y True si quedaron más
        allá de max_txs. Lanza ValueError si el RPC no tiene el método."""
        txs: list[dict] = []
        token = None
        # Helius filtra por tiempo en el servidor: una ventana antigua no
        # cuesta más que una reciente.
        block_time = {"gte": int(since)}
        if until is not None:
            block_time["lte"] = int(until)
        while len(txs) < self._max_txs:
            options: dict = {
                "transactionDetails": "full",
                "sortOrder": "desc",
                "limit": min(HELIUS_PAGE, self._max_txs - len(txs)),
                "commitment": COMMITMENT,
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": MAX_TRANSACTION_VERSION,
                # Las fallidas las filtra Helius y no se pagan.
                "filters": {"blockTime": block_time, "status": "succeeded"},
            }
            if token:
                options["paginationToken"] = token
            result = await self._rpc.call("getTransactionsForAddress", [address, options])
            if not isinstance(result, dict):
                raise ValueError(f"respuesta inesperada de getTransactionsForAddress: {result!r:.100}")
            page = result.get("data") or []
            txs += [tx for tx in page if isinstance(tx, dict)]
            token = result.get("paginationToken")
            # Helius da token aunque no quede nada, y pedir una página vacía
            # cuesta el mínimo de 10 créditos: una incompleta ya es la última.
            if not token or len(page) < options["limit"]:
                return txs, False
        return txs, True

    # --- rpc: getSignaturesForAddress + getTransaction ---

    async def _read_rpc(
        self, addresses: list[str], since: float, until: float | None
    ) -> list[dict | None]:
        signatures: dict[str, float] = {}
        for address in addresses:
            try:
                signatures |= await self._recent_signatures(address, since, until)
            except RPC_ERRORS as exc:
                log.warning("No se pudieron pedir las firmas de %s: %s", address, exc)
        newest = sorted(signatures, key=signatures.__getitem__, reverse=True)
        if len(newest) > self._max_txs:
            self._warn_truncated(len(newest))
            newest = newest[: self._max_txs]
        log.info("Leyendo %d transacciones...", len(newest))
        return list(await asyncio.gather(*(self._transaction(s) for s in newest)))

    async def _recent_signatures(
        self, address: str, since: float, until: float | None
    ) -> dict[str, float]:
        """Firma -> blockTime de las transacciones correctas de `address`
        en la ventana. Las fallidas se descartan aquí, sin gastar un
        getTransaction. Las firmas solo se piden de la más nueva hacia atrás:
        en una ventana antigua se pagan también las páginas posteriores a
        ella (1 crédito por cada 1000)."""
        found: dict[str, float] = {}
        before = None
        while True:
            options: dict = {"limit": SIGNATURES_PAGE, "commitment": COMMITMENT}
            if before:
                options["before"] = before
            entries = await self._rpc.call("getSignaturesForAddress", [address, options])
            if not isinstance(entries, list) or not entries:
                return found
            for entry in entries:
                block_time = entry.get("blockTime")
                # Vienen de la más nueva a la más vieja: la primera fuera de
                # la ventana cierra la búsqueda.
                if block_time is not None and block_time < since:
                    return found
                if until is not None and block_time is not None and block_time > until:
                    continue
                if entry.get("err") is None:
                    found[entry["signature"]] = float(block_time or time.time())
            if len(entries) < SIGNATURES_PAGE:
                return found
            before = entries[-1]["signature"]

    async def _transaction(self, signature: str) -> dict | None:
        try:
            tx = await self._rpc.call(
                "getTransaction",
                [signature, {
                    "encoding": "jsonParsed",
                    "commitment": COMMITMENT,
                    "maxSupportedTransactionVersion": MAX_TRANSACTION_VERSION,
                }],
            )
        except RPC_ERRORS as exc:
            log.debug("No se pudo leer la transacción %s: %s", signature, exc)
            return None
        return tx if isinstance(tx, dict) else None


class BuyerFinder:
    """Wallets que compraron un token gastando al menos `min_sol` en una
    transacción, a partir de lo que lee un TransactionReader."""

    def __init__(self, reader: TransactionReader, min_sol: float = DEFAULT_MIN_SOL):
        self._reader = reader
        self._min_sol = min_sol

    async def find(
        self, mint: str, addresses: list[str], since: float, until: float | None = None
    ) -> list[Buyer]:
        """Compradores de `mint` entre `since` y `until` (None = ahora), de
        quien más SOL gastó a quien menos."""
        buyers: dict[str, Buyer] = {}
        failed = 0
        for tx in await self._reader.read(addresses, since, until):
            wallet = tx_fee_payer(tx) if tx else None
            signature = tx_signature(tx) if tx else None
            if wallet is None or signature is None:
                failed += 1
                continue
            for buy in parse_buys(tx, wallet, "", signature, self._min_sol):
                if buy.mint != mint or buy.sol_spent < self._min_sol:
                    continue
                buyer = buyers.setdefault(wallet, Buyer(wallet))
                buyer.sol_spent += buy.sol_spent
                buyer.buys += 1
                buyer.last_ts = max(buyer.last_ts, buy.ts)
        if failed:
            log.warning("%d transacciones no se pudieron leer", failed)
        return sorted(buyers.values(), key=lambda b: b.sol_spent, reverse=True)


class HistoryFilter:
    """Revisa el historial de firmas de cada comprador (1 crédito por cada
    1000) y descarta las wallets nuevas y los bots."""

    def __init__(
        self,
        rpc: Rpc,
        min_history: int = DEFAULT_MIN_HISTORY,
        max_txs_per_day: float = DEFAULT_MAX_TXS_PER_DAY,
        max_failed_pct: float = DEFAULT_MAX_FAILED_PCT,
        max_pages: int = DEFAULT_HISTORY_PAGES,
    ):
        self._rpc = rpc
        self._min_history = min_history
        self._max_txs_per_day = max_txs_per_day
        self._max_failed_pct = max_failed_pct
        self._max_pages = max_pages

    async def split(self, buyers: list[Buyer]) -> tuple[list[Buyer], list[tuple[Buyer, str]]]:
        """Separa los compradores que operan habitualmente de los descartados,
        estos con su motivo. Mantiene el orden."""
        kept: list[Buyer] = []
        discarded: list[tuple[Buyer, str]] = []
        reasons = await asyncio.gather(*(self._safe_check(b) for b in buyers))
        for buyer, reason in zip(buyers, reasons):
            if reason is None:
                kept.append(buyer)
            else:
                discarded.append((buyer, reason))
        return kept, discarded

    async def _safe_check(self, buyer: Buyer) -> str | None:
        try:
            return await self.check(buyer)
        except RPC_ERRORS as exc:
            log.warning("No se pudo leer el historial de %s: %s", buyer.wallet, exc)
            return "no se pudo leer su historial"

    async def check(self, buyer: Buyer) -> str | None:
        """Rellena el historial de `buyer` y devuelve por qué se descarta
        (None si se queda). Solana no guarda la fecha de creación de una
        cuenta: la primera transacción (normalmente la que le mandó los
        primeros SOL) es lo más cerca."""
        before = None
        for page in range(self._max_pages):
            options: dict = {"limit": SIGNATURES_PAGE, "commitment": COMMITMENT}
            if before:
                options["before"] = before
            entries = await self._rpc.call("getSignaturesForAddress", [buyer.wallet, options])
            if not isinstance(entries, list) or not entries:
                buyer.history_complete = True
                break
            if page == 0 and (reason := self._bot_reason(entries)):
                return reason
            buyer.history_txs += len(entries)
            times = [e["blockTime"] for e in entries if e.get("blockTime") is not None]
            if times:
                buyer.first_ts = times[-1]
            if len(entries) < SIGNATURES_PAGE:
                buyer.history_complete = True
                break
            before = entries[-1]["signature"]
        if buyer.history_complete and buyer.history_txs < self._min_history:
            return f"nueva: solo {buyer.history_txs} transacciones"
        return None

    def _bot_reason(self, entries: list[dict]) -> str | None:
        """Por qué parece un bot según sus firmas más recientes (lo que hace
        ahora), o None. Con pocas firmas no se juzga."""
        if len(entries) < MIN_TXS_FOR_BOT_CHECK:
            return None
        failed_pct = 100 * sum(e.get("err") is not None for e in entries) / len(entries)
        if failed_pct > self._max_failed_pct:
            return f"bot: {failed_pct:.0f}% de transacciones fallidas"
        times = [e["blockTime"] for e in entries if e.get("blockTime") is not None]
        if times:
            # Mínimo una hora, para que una ráfaga corta no dispare la media.
            span = max(times[0] - times[-1], 3600)
            per_day = len(entries) / span * 86400
            if per_day > self._max_txs_per_day:
                return f"bot: {per_day:.0f} transacciones/día"
        return None


class Report:
    """Salida por pantalla del resultado."""

    def __init__(self, window: str, min_sol: float):
        self._window = window
        self._min_sol = min_sol

    def print(self, kept: list[Buyer], discarded: list[tuple[Buyer, str]], rpc: Rpc) -> None:
        print(
            f"\n{len(kept)} wallets habituales compraron >= {self._min_sol:g} SOL"
            f" {self._window}:\n"
        )
        for buyer in kept:
            last = time.strftime("%H:%M:%S", time.localtime(buyer.last_ts))
            print(
                f"{buyer.wallet}  {buyer.sol_spent:10.3f} SOL  {buyer.buys:3d} compras  última {last}"
                f"  {buyer.history_text:>7}  desde {buyer.since_text}"
            )
        if discarded:
            print(f"\nDescartadas ({len(discarded)}):")
            for buyer, reason in discarded:
                print(f"{buyer.wallet}  {buyer.sol_spent:10.3f} SOL  {reason}")
        print(f"\nLlamadas al RPC: {rpc.calls}  (~{rpc.credits} créditos de Helius)")


async def _pool_addresses(session: aiohttp.ClientSession, chain_id: str, mint: str) -> list[str]:
    """Pools del token en DexScreener. Si falla, se sigue solo con el mint."""
    pairs = await DexScreenerClient(session).get_pairs_for_tokens(chain_id, [mint])
    return [p["pairAddress"] for p in pairs if isinstance(p.get("pairAddress"), str)]


TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def parse_time(text: str, now: datetime) -> datetime:
    """Hora local de --from/--to: "16:30", "16:30:15" o con fecha
    "2026-10-11 16:30". Sin fecha es hoy, o ayer si esa hora aún no ha
    llegado (a las 00:10, "23:50" es la de anoche)."""
    for fmt in TIME_FORMATS:
        try:
            parsed = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if "%Y" in fmt:
            return parsed
        moment = now.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)
        return moment - timedelta(days=1) if moment > now else moment
    raise ValueError(f"hora inválida {text!r}: usa HH:MM, HH:MM:SS o AAAA-MM-DD HH:MM")


def _time_text(moment: datetime, now: datetime) -> str:
    fmt = "%H:%M" if moment.second == 0 else "%H:%M:%S"
    return moment.strftime(fmt if moment.date() == now.date() else f"%Y-%m-%d {fmt}")


def time_window(
    minutes: float, start: str | None, end: str | None, now: datetime
) -> tuple[float, float | None, str]:
    """(desde, hasta, texto) de la ventana a buscar. Con --from manda el
    rango (--to vacío = hasta ahora); sin él, los últimos `minutes`.
    hasta None = ahora."""
    if start is None:
        if end is not None:
            raise ValueError("--to necesita --from")
        return now.timestamp() - minutes * 60, None, f"en los últimos {minutes:g} min"
    since = parse_time(start, now)
    until = parse_time(end, now) if end is not None else None
    if until is not None and until <= since:
        raise ValueError(f"--to ({_time_text(until, now)}) debe ser posterior a --from ({_time_text(since, now)})")
    end_text = _time_text(until, now) if until is not None else "ahora"
    return (
        since.timestamp(),
        until.timestamp() if until is not None else None,
        f"entre {_time_text(since, now)} y {end_text}",
    )


async def _run(config: Config, args: argparse.Namespace, since: float, until: float | None, window: str) -> None:
    async with aiohttp.ClientSession() as session:
        pools = await _pool_addresses(session, config.chain_id, args.mint)
        log.info("Pools del token: %d", len(pools))
        rpc = Rpc(session, config.solana_rpc_url)
        finder = BuyerFinder(TransactionReader(rpc, args.max_txs, args.method), args.min_sol)
        buyers = await finder.find(args.mint, [args.mint, *pools], since, until)
        log.info("Revisando el historial de %d wallets...", len(buyers))
        history = HistoryFilter(
            rpc, args.min_history, args.max_txs_per_day, args.max_failed_pct, args.history_pages
        )
        kept, discarded = await history.split(buyers)
    Report(window, args.min_sol).print(kept, discarded, rpc)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wallets que compraron un token en los últimos minutos")
    parser.add_argument("mint", help="dirección del mint del token")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                        help="ventana hasta ahora, si no se da --from")
    parser.add_argument("--from", dest="start", metavar="HORA",
                        help="inicio del rango en hora local: 16:30 o '2026-10-11 16:30'")
    parser.add_argument("--to", dest="end", metavar="HORA",
                        help="fin del rango (por defecto, ahora)")
    parser.add_argument("--min-sol", type=float, default=DEFAULT_MIN_SOL)
    parser.add_argument("--max-txs", type=int, default=DEFAULT_MAX_TXS)
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD,
                        help="helius: getTransactionsForAddress (10 créditos/100 tx); "
                             "rpc: getTransaction por firma (1 crédito/tx), cualquier RPC")
    parser.add_argument("--min-history", type=int, default=DEFAULT_MIN_HISTORY,
                        help="transacciones mínimas en su historial (menos = wallet nueva)")
    parser.add_argument("--max-txs-per-day", type=float, default=DEFAULT_MAX_TXS_PER_DAY,
                        help="ritmo a partir del cual se considera un bot")
    parser.add_argument("--max-failed-pct", type=float, default=DEFAULT_MAX_FAILED_PCT,
                        help="%% de transacciones fallidas a partir del cual se considera un bot")
    parser.add_argument("--history-pages", type=int, default=DEFAULT_HISTORY_PAGES,
                        help="páginas de 1000 firmas para buscar la primera transacción")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(format="[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    try:
        config = Config.from_toml(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"Configuración inválida: {e}") from e
    try:
        since, until, window = time_window(args.minutes, args.start, args.end, datetime.now())
    except ValueError as e:
        raise SystemExit(str(e)) from e
    asyncio.run(_run(config, args, since, until, window))


if __name__ == "__main__":
    main()
