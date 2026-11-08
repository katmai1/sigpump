"""
sigpump/wallet_audit.py

Qué hace en la cadena cada wallet del fichero, para entender por qué unas
avisan y otras no. Se ejecuta a mano:

    python wallet_audit.py [--txs 50] [--days 30] [--wallet DIRECCIÓN ...]

La base solo guarda las compras que llegan al radar: las de menos de
[wallets].min_sol no dejan rastro, y las de bonding curve (sin
[wallets].bonding_curve) solo salen en el log. El 2026-11-08, 24 de las 90
wallets no tenían ni una fila en alertas.db y no había forma de saber si no
compraban o si el bot las descartaba antes de registrarlas.

Por cada wallet:
- getSignaturesForAddress (1 crédito): cuántas transacciones hizo en 7 días,
  cuántas fallaron y la última.
- Sus últimas --txs transacciones de los últimos --days días, enteras
  (TransactionReader de buyers.py: 10 créditos por cada 100 con Helius, 1
  por transacción con --method rpc), clasificadas en compras (de min_sol o
  más, pequeñas, con stablecoin o en bonding curve), ventas y otras.
- Cuántos de los tokens que compró con min_sol o más están en alertas.db:
  los que faltan no llegaron al radar.

Solo lee: ni wallets.txt ni alertas.db se tocan (por eso no usa load_wallets,
que reescribe el fichero al quitar repetidas, ni Blacklist).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import statistics
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.buyers import DEFAULT_METHOD, METHODS, RPC_ERRORS, Rpc, TransactionReader, tx_signature
from sigpump.config import Config
from sigpump.solana import redact
from sigpump.util import SOLANA_ADDRESS_RE
from sigpump.wallet_list import OLD_TRUSTED_MARK
from sigpump.wallets import (
    COMMITMENT,
    LAMPORTS_PER_SOL,
    QUOTE_MINTS,
    STABLE_MINTS,
    WSOL_MINT,
    _account_index,
    _token_amounts,
)

log = logging.getLogger()

DEFAULT_TXS = 50
DEFAULT_DAYS = 30.0
# Ventana del recuento de transacciones (el mismo plazo que max_inactive_days
# en config.toml).
ACTIVITY_DAYS = 7
SIGNATURES_PAGE = 1000
# Gasto mínimo en SOL para llamar compra a que suba un token: crear la cuenta
# del token cuesta ~0,002 SOL de renta, y con menos sería un airdrop o una
# transferencia en la que la wallet pagó la cuenta.
MIN_BUY_SOL = 0.005
# Programas de bonding curve de los launchpads (los mismos que
# radar.BONDING_CURVE_DEX_IDS en DexScreener). Se miran en la transacción y no
# en DexScreener: muchos tokens se graduaron después, y lo que importa es
# dónde estaban cuando la wallet compró.
CURVE_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "LaunchLab",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonshot",
}

# Clases de transacción.
BUY = "compra"
BUY_SMALL = "compra pequeña"
BUY_STABLE = "compra con stable"
BUY_CURVE = "bonding curve"
SELL = "venta"
OTHER = "otra"
# Las que wallets.parse_buys deja pasar: el bot las ve y las registra.
SEEN_BY_BOT = (BUY, BUY_STABLE)


@dataclass(frozen=True)
class TxClass:
    """Qué fue una transacción para la wallet: su clase, los tokens que
    compró y el SOL que gastó (repartido entre ellos)."""
    kind: str
    mints: tuple[str, ...] = ()
    sol_spent: float = 0.0


def _programs(tx: dict) -> set[str]:
    """Programas que aparecen en la transacción: cuentas (también las de
    tablas de direcciones, que jsonParsed incluye), instrucciones internas y
    logs. Un agregador como Jupiter llega a la curva por una interna."""
    found: set[str] = set()
    message = (tx.get("transaction") or {}).get("message") or {}
    for key in message.get("accountKeys") or []:
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if isinstance(pubkey, str):
            found.add(pubkey)
    meta = tx.get("meta") or {}
    for loaded in (meta.get("loadedAddresses") or {}).values():
        found.update(a for a in loaded or [] if isinstance(a, str))
    for group in meta.get("innerInstructions") or []:
        for ins in (group or {}).get("instructions") or []:
            if isinstance(ins, dict) and isinstance(ins.get("programId"), str):
                found.add(ins["programId"])
    for line in meta.get("logMessages") or []:
        if isinstance(line, str) and line.startswith("Program ") and " invoke [" in line:
            found.add(line.split()[1])
    return found


def classify(tx: dict, wallet: str, min_sol: float) -> TxClass:
    """Clase de `tx` para `wallet`, con el mismo cálculo de saldos que
    wallets.parse_buys (que con min_sol descarta en silencio lo pequeño)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return TxClass(OTHER)
    pre = _token_amounts(meta.get("preTokenBalances"), wallet)
    post = _token_amounts(meta.get("postTokenBalances"), wallet)
    deltas = {mint: post.get(mint, 0.0) - pre.get(mint, 0.0) for mint in pre.keys() | post.keys()}
    sol_spent = -deltas.get(WSOL_MINT, 0.0)
    index = _account_index(tx, wallet)
    pre_lamports, post_lamports = meta.get("preBalances") or [], meta.get("postBalances") or []
    if index is not None and index < min(len(pre_lamports), len(post_lamports)):
        spent = pre_lamports[index] - post_lamports[index]
        if index == 0:
            spent -= meta.get("fee") or 0
        sol_spent += spent / LAMPORTS_PER_SOL
    stable_spent = -sum(deltas.get(mint, 0.0) for mint in STABLE_MINTS)
    bought = tuple(sorted(m for m, d in deltas.items() if d > 0 and m not in QUOTE_MINTS))
    sold = [m for m, d in deltas.items() if d < 0 and m not in QUOTE_MINTS]

    if bought and (sol_spent >= MIN_BUY_SOL or stable_spent > 0):
        share = round(max(0.0, sol_spent) / len(bought), 4)
        if _programs(tx) & CURVE_PROGRAMS.keys():
            return TxClass(BUY_CURVE, bought, share)
        if sol_spent >= min_sol:
            return TxClass(BUY, bought, share)
        if stable_spent > 0:
            return TxClass(BUY_STABLE, bought, share)
        return TxClass(BUY_SMALL, bought, share)
    if sold and (sol_spent < 0 or stable_spent < 0):
        return TxClass(SELL)
    return TxClass(OTHER)


@dataclass
class WalletAudit:
    """Lo visto de una wallet."""
    wallet: str
    label: str
    # Motivo si está en la lista negra (o castigada todavía).
    banned: str | None = None
    txs_7d: int = 0
    # True si hay más de las SIGNATURES_PAGE contadas.
    txs_7d_more: bool = False
    failed_7d: int = 0
    last_ts: float | None = None
    read: int = 0
    # blockTime de la más vieja leída: cuánto tiempo cubren las --txs.
    oldest_read_ts: float | None = None
    # Última de esas compras: si es posterior a cuando
    # se añadió la wallet y no está en la base, el bot no la vio.
    last_buy_ts: float | None = None
    unreadable: int = 0
    kinds: Counter = field(default_factory=Counter)
    # SOL por compra de cada clase de compra.
    spent: dict[str, list[float]] = field(default_factory=dict)
    # Tokens de las compras que el bot registra (min_sol o más, o con
    # stablecoin, fuera de bonding curve), y cuántos tiene alertas.db de esta wallet.
    # 2026-11-08: las de stable se tomaban por un problema y avisaban igual.
    tokens: set[str] = field(default_factory=set)
    tokens_in_db: int = 0
    error: str | None = None

    @property
    def buys(self) -> int:
        return sum(self.kinds[k] for k in (BUY, BUY_SMALL, BUY_STABLE, BUY_CURVE))

    def median_spent(self, kind: str) -> float | None:
        values = self.spent.get(kind) or []
        return statistics.median(values) if values else None

    def diagnosis(self, min_sol: float, days: float) -> str:
        """Por qué avisa o no, en una frase. El orden importa: la primera
        causa que explica que no avise."""
        if self.error:
            return f"error: {self.error}"
        if self.banned:
            return f"lista negra: {self.banned}"
        if self.read == 0:
            return f"inactiva: ninguna transacción en {days:g} días"
        if self.buys == 0:
            return "no compra: solo ventas u otras"
        if not any(self.kinds[k] for k in SEEN_BY_BOT):
            main = max((BUY_CURVE, BUY_SMALL), key=lambda k: self.kinds[k])
            if main == BUY_SMALL:
                return f"solo compras < {min_sol:g} SOL (mediana {self.median_spent(BUY_SMALL):.3f})"
            return f"solo {main}"
        if self.tokens and self.tokens_in_db == 0:
            # Lo normal en una wallet recién añadida: compró antes de seguirla.
            return "compra pero no está en la base"
        return "ok"


def read_wallets(path: Path) -> dict[str, str]:
    """Dirección -> etiqueta del fichero de wallets, sin modificarlo (mismo
    formato que wallet_list.load_wallets)."""
    wallets: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        address, _, label = line.partition("#")
        address = address.strip().removesuffix(OLD_TRUSTED_MARK).strip()
        if address and SOLANA_ADDRESS_RE.match(address) and address not in wallets:
            wallets[address] = label.strip() or f"{address[:4]}…{address[-4:]}"
    return wallets


class Ledger:
    """alertas.db en solo lectura: lista negra y tokens registrados por wallet.
    Sin base (o sin tablas) devuelve vacío."""

    def __init__(self, path: Path):
        self._db: sqlite3.Connection | None = None
        if path.exists():
            self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._db is None:
            return []
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            log.warning("No se pudo leer la base: %s", exc)
            return []

    def banned(self, now: float) -> dict[str, str]:
        """Wallet -> motivo de las que están en la lista negra ahora."""
        rows = self._query("SELECT wallet, motivo, hasta FROM lista_negra")
        return {w: m or "" for w, m, until in rows if until is None or until > now}

    def tokens(self, wallet: str) -> set[str]:
        """Tokens de las compras de `wallet` registradas, avisadas o no."""
        return {t for (t,) in self._query("SELECT DISTINCT token FROM alertas WHERE wallet = ?", (wallet,)) if t}


class WalletAuditor:
    """Lee la actividad de cada wallet y la clasifica."""

    def __init__(self, rpc: Rpc, reader: TransactionReader, min_sol: float, days: float, now: float | None = None):
        self._rpc = rpc
        self._reader = reader
        self._min_sol = min_sol
        self._days = days
        self._now = now

    async def audit(self, audit: WalletAudit) -> WalletAudit:
        now = self._now or time.time()
        try:
            await self._activity(audit, now)
            # Sin ninguna firma en la ventana no se gasta la lectura completa.
            if audit.last_ts is None or audit.last_ts < now - self._days * 86400:
                return audit
            for tx in await self._reader.read([audit.wallet], now - self._days * 86400):
                if not tx or tx_signature(tx) is None:
                    audit.unreadable += 1
                    continue
                audit.read += 1
                if (ts := tx.get("blockTime")) and (audit.oldest_read_ts is None or ts < audit.oldest_read_ts):
                    audit.oldest_read_ts = ts
                kind = classify(tx, audit.wallet, self._min_sol)
                audit.kinds[kind.kind] += 1
                if kind.mints:
                    audit.spent.setdefault(kind.kind, []).append(kind.sol_spent * len(kind.mints))
                if kind.kind in SEEN_BY_BOT:
                    audit.tokens.update(kind.mints)
                    audit.last_buy_ts = max(audit.last_buy_ts or 0.0, tx.get("blockTime") or 0.0) or None
        except RPC_ERRORS as exc:
            audit.error = redact(exc)[:80]
        return audit

    async def _activity(self, audit: WalletAudit, now: float) -> None:
        """Transacciones de los últimos ACTIVITY_DAYS días (fallidas
        incluidas) y la última, con una sola página de firmas."""
        entries = await self._rpc.call(
            "getSignaturesForAddress", [audit.wallet, {"limit": SIGNATURES_PAGE, "commitment": COMMITMENT}]
        )
        if not isinstance(entries, list) or not entries:
            return
        audit.last_ts = entries[0].get("blockTime")
        since = now - ACTIVITY_DAYS * 86400
        recent = [e for e in entries if (e.get("blockTime") or 0) >= since]
        audit.txs_7d = len(recent)
        audit.txs_7d_more = len(recent) == len(entries) == SIGNATURES_PAGE
        audit.failed_7d = sum(e.get("err") is not None for e in recent)


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}"


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "nunca"
    hours = (now - ts) / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} d"


class Report:
    """Tabla por wallet, agrupada por diagnóstico, y totales."""

    def __init__(self, min_sol: float, days: float, txs: int):
        self._min_sol = min_sol
        self._days = days
        self._txs = txs

    def print(self, audits: list[WalletAudit], rpc: Rpc, now: float, out=sys.stdout) -> None:
        def p(text: str = "") -> None:
            print(text, file=out)

        p(f"\nÚltimas {self._txs} transacciones de los últimos {self._days:g} días de cada wallet"
          f" (compra = >= {self._min_sol:g} SOL fuera de bonding curve):\n")
        p(f"{'wallet':11} {'etiqueta':16} {'tx 7d':>6} {'últ.':>5} {'leídas':>6} {'cubre':>5} {'compra':>6} {'últ.c':>5} "
          f"{'peq.':>5} {'stab.':>5} {'curva':>5} {'venta':>5} {'base':>7}  diagnóstico")
        order = sorted(audits, key=lambda a: (a.diagnosis(self._min_sol, self._days) == "ok", -a.kinds[BUY]))
        for a in order:
            txs_7d = f"{a.txs_7d}{'+' if a.txs_7d_more else ''}"
            base = f"{a.tokens_in_db}/{len(a.tokens)}" if a.tokens else "-"
            covers = _ago(a.oldest_read_ts, now) if a.read else "-"
            p(f"{_short(a.wallet):11} {a.label[:16]:16} {txs_7d:>6} {_ago(a.last_ts, now):>5} {a.read:>6} {covers:>5} "
              f"{a.kinds[BUY]:>6} {_ago(a.last_buy_ts, now) if a.last_buy_ts else '-':>5} {a.kinds[BUY_SMALL]:>5} {a.kinds[BUY_STABLE]:>5} {a.kinds[BUY_CURVE]:>5} "
              f"{a.kinds[SELL]:>5} {base:>7}  {a.diagnosis(self._min_sol, self._days)}")
        p("\núlt.c = hace cuánto fue la última compra que registra el bot (min_sol o más, o con stable).")
        p("cubre = antigüedad de la más vieja leída (hasta dónde llegan las --txs).")
        p("base = de los tokens de esas compras, cuántos están en alertas.db para esa wallet.")
        diagnoses = Counter(a.diagnosis(self._min_sol, self._days).split(":")[0].split(" (")[0] for a in audits)
        p("\nResumen:")
        for text, n in diagnoses.most_common():
            p(f"  {n:3d}  {text}")
        p(f"\nLlamadas al RPC: {rpc.calls}  (~{rpc.credits} créditos de Helius)")


async def _run(config: Config, args: argparse.Namespace) -> None:
    now = time.time()
    wallets = read_wallets(Path(config.wallets_file))
    if args.wallet:
        wanted = set(args.wallet)
        wallets = {w: l for w, l in wallets.items() if w in wanted or any(w.startswith(x) for x in wanted)}
        if not wallets:
            raise SystemExit("Ninguna de esas wallets está en el fichero")
    ledger = Ledger(Path(config.alert_log_path))
    try:
        banned = ledger.banned(now)
        audits = [WalletAudit(w, label, banned=banned.get(w)) for w, label in wallets.items()]
        async with aiohttp.ClientSession() as session:
            rpc = Rpc(session, config.solana_rpc_url)
            reader = TransactionReader(rpc, args.txs, args.method)
            auditor = WalletAuditor(rpc, reader, config.wallets_min_sol, args.days, now)
            print(f"Revisando {len(audits)} wallets...", file=sys.stderr)
            await asyncio.gather(*(auditor.audit(a) for a in audits))
        for a in audits:
            if a.tokens:
                a.tokens_in_db = len(a.tokens & ledger.tokens(a.wallet))
    finally:
        ledger.close()
    Report(config.wallets_min_sol, args.days, args.txs).print(audits, rpc, now)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qué hace en la cadena cada wallet del fichero")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--txs", type=int, default=DEFAULT_TXS,
                        help="transacciones leídas enteras por wallet (las más recientes)")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS,
                        help="solo transacciones de los últimos días")
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD,
                        help="helius: getTransactionsForAddress (10 créditos/100 tx); "
                             "rpc: getTransaction por firma (1 crédito/tx)")
    parser.add_argument("--wallet", nargs="+", metavar="DIRECCIÓN",
                        help="solo estas wallets (dirección o su principio)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(format="[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    # TransactionReader dice cuántas lee de cada wallet y avisa de que hay más
    # que --txs (pensado para buyers.py: aquí es lo normal); con 90 wallets
    # tapa la tabla. Los errores del RPC salen en el diagnóstico de cada una.
    log.setLevel(logging.INFO if args.verbose else logging.ERROR)
    try:
        config = Config.from_toml(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"Configuración inválida: {e}") from e
    asyncio.run(_run(config, args))


if __name__ == "__main__":
    main()
