"""Tests de la búsqueda de compradores recientes de un token."""

import asyncio
import time
import unittest
from unittest.mock import patch

from sigpump.buyers import SIGNATURES_PAGE, Buyer, BuyerFinder, HistoryFilter, TransactionReader
from sigpump.wallets import LAMPORTS_PER_SOL

MINT = "TokenMint1111111111111111111111111111111pump"
POOL = "Pool111111111111111111111111111111111111111"
BALLENA = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
PEZ = "7Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j9"


def _compra(wallet, sol, ts, firma="x"):
    """getTransaction de `wallet` gastando `sol` SOL en 1000 unidades de MINT."""
    balance = {"owner": wallet, "mint": MINT, "uiTokenAmount": {"amount": "1000000000", "decimals": 6}}
    return {
        "blockTime": ts,
        "transaction": {"signatures": [firma], "message": {"accountKeys": [{"pubkey": wallet}]}},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [int(10 * LAMPORTS_PER_SOL)],
            "postBalances": [int((10 - sol) * LAMPORTS_PER_SOL) - 5000],
            "preTokenBalances": [],
            "postTokenBalances": [balance],
        },
    }


def _find(rpc, addresses, since, max_txs=100, method="helius"):
    finder = BuyerFinder(TransactionReader(rpc, max_txs, method), min_sol=2.0)
    return asyncio.run(finder.find(MINT, addresses, since))


class _FakeRpc:
    def __init__(self, signatures, transactions):
        self.signatures = signatures
        self.transactions = transactions
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params[0]))
        if method == "getSignaturesForAddress":
            return self.signatures.get(params[0], [])
        if method == "getTransactionsForAddress":
            raise ValueError("Method not found")
        return self.transactions.get(params[0])


class _HeliusRpc:
    """getTransactionsForAddress paginado: `paginas` por dirección."""

    def __init__(self, paginas):
        self.paginas = paginas
        self.calls = []

    async def call(self, method, params):
        address, options = params
        self.calls.append((method, address, options))
        pagina = int(options.get("paginationToken") or 0)
        paginas = self.paginas.get(address, [[]])
        siguiente = str(pagina + 1) if pagina + 1 < len(paginas) else None
        return {"data": paginas[pagina], "paginationToken": siguiente}


class FindBuyersTest(unittest.TestCase):
    def test_suma_compras_de_la_ventana_y_filtra_por_sol(self):
        now = time.time()
        rpc = _FakeRpc(
            signatures={
                MINT: [
                    {"signature": "a", "blockTime": now - 60, "err": None},
                    {"signature": "fallida", "blockTime": now - 70, "err": {"x": 1}},
                    {"signature": "b", "blockTime": now - 120, "err": None},
                    {"signature": "vieja", "blockTime": now - 3600, "err": None},
                ],
                # La misma firma por el pool no se lee dos veces.
                POOL: [
                    {"signature": "a", "blockTime": now - 60, "err": None},
                    {"signature": "c", "blockTime": now - 90, "err": None},
                ],
            },
            transactions={
                "a": _compra(BALLENA, 3, now - 60, "a"),
                "b": _compra(BALLENA, 2, now - 120, "b"),
                "c": _compra(PEZ, 0.5, now - 90, "c"),
                "vieja": _compra(PEZ, 50, now - 3600, "vieja"),
            },
        )
        buyers = _find(rpc, [MINT, POOL], now - 600, method="rpc")

        self.assertEqual([b.wallet for b in buyers], [BALLENA])
        self.assertAlmostEqual(buyers[0].sol_spent, 5.0)
        self.assertEqual(buyers[0].buys, 2)
        leidas = sorted(p for m, p in rpc.calls if m == "getTransaction")
        self.assertEqual(leidas, ["a", "b", "c"])

    def test_tope_de_transacciones_lee_las_mas_recientes(self):
        now = time.time()
        rpc = _FakeRpc(
            signatures={MINT: [
                {"signature": "nueva", "blockTime": now - 10, "err": None},
                {"signature": "antigua", "blockTime": now - 300, "err": None},
            ]},
            transactions={
                "nueva": _compra(BALLENA, 3, now - 10, "nueva"),
                "antigua": _compra(PEZ, 3, now - 300, "antigua"),
            },
        )
        buyers = _find(rpc, [MINT], now - 600, max_txs=1, method="rpc")
        self.assertEqual([b.wallet for b in buyers], [BALLENA])


class _HistoryRpc:
    """getSignaturesForAddress paginado sobre una lista fija de firmas."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    async def call(self, method, params):
        self.calls += 1
        before = params[1].get("before")
        start = 0 if before is None else int(before) + 1
        return self.entries[start : start + SIGNATURES_PAGE]


def _historial(n, cada_segundos, fallidas_cada=0, desde=None):
    """n firmas de la más nueva a la más vieja, separadas cada_segundos."""
    now = desde or time.time()
    return [
        {
            "signature": str(i),
            "blockTime": now - i * cada_segundos,
            "err": {"x": 1} if fallidas_cada and i % fallidas_cada == 0 else None,
        }
        for i in range(n)
    ]


def _check(entries, pages=5):
    buyer = Buyer(BALLENA)
    history = HistoryFilter(_HistoryRpc(entries), min_history=20, max_txs_per_day=1000, max_failed_pct=50, max_pages=pages)
    reason = asyncio.run(history.check(buyer))
    return buyer, reason


class FindBuyersHeliusTest(unittest.TestCase):
    def test_pagina_y_no_repite_firmas_entre_direcciones(self):
        now = time.time()
        rpc = _HeliusRpc({
            MINT: [[_compra(BALLENA, 3, now - 60, "a")], [_compra(BALLENA, 2, now - 120, "b")]],
            POOL: [[_compra(BALLENA, 3, now - 60, "a"), _compra(PEZ, 4, now - 90, "c")]],
        })
        # Páginas de 1: la de MINT viene llena y se pide la siguiente.
        with patch("sigpump.buyers.HELIUS_PAGE", 1):
            buyers = _find(rpc, [MINT, POOL], now - 600)

        self.assertEqual([(b.wallet, b.sol_spent, b.buys) for b in buyers], [(BALLENA, 5.0, 2), (PEZ, 4.0, 1)])
        self.assertTrue(all(m == "getTransactionsForAddress" for m, _, _ in rpc.calls))
        filtros = rpc.calls[0][2]["filters"]
        self.assertEqual(filtros, {"blockTime": {"gte": int(now - 600)}, "status": "succeeded"})

    def test_pagina_incompleta_es_la_ultima(self):
        # Helius devuelve token aunque no quede nada: no se paga otra página.
        now = time.time()
        rpc = _HeliusRpc({MINT: [[_compra(BALLENA, 3, now - 60, "a")], []]})
        _find(rpc, [MINT], now - 600)
        self.assertEqual(len(rpc.calls), 1)

    def test_tope_corta_la_paginacion(self):
        now = time.time()
        rpc = _HeliusRpc({MINT: [
            [_compra(BALLENA, 3, now - 10, "nueva")],
            [_compra(PEZ, 3, now - 300, "antigua")],
        ]})
        buyers = _find(rpc, [MINT], now - 600, max_txs=1)
        self.assertEqual([b.wallet for b in buyers], [BALLENA])
        self.assertEqual(len(rpc.calls), 1)

    def test_sin_el_metodo_usa_el_rpc_estandar(self):
        now = time.time()
        rpc = _FakeRpc(
            signatures={MINT: [{"signature": "a", "blockTime": now - 60, "err": None}]},
            transactions={"a": _compra(BALLENA, 3, now - 60, "a")},
        )
        buyers = _find(rpc, [MINT], now - 600)
        self.assertEqual([b.wallet for b in buyers], [BALLENA])


class CheckHistoryTest(unittest.TestCase):
    def test_wallet_nueva_se_descarta(self):
        buyer, reason = _check(_historial(5, 3600))
        self.assertEqual(reason, "nueva: solo 5 transacciones")

    def test_wallet_habitual_con_fecha_de_creacion(self):
        entries = _historial(300, 6 * 3600)
        buyer, reason = _check(entries)
        self.assertIsNone(reason)
        self.assertEqual(buyer.history_txs, 300)
        self.assertTrue(buyer.history_complete)
        self.assertEqual(buyer.first_ts, entries[-1]["blockTime"])

    def test_bot_por_ritmo(self):
        # 1000 transacciones en ~17 minutos.
        _, reason = _check(_historial(SIGNATURES_PAGE, 1))
        self.assertTrue(reason.startswith("bot:"), reason)

    def test_bot_por_fallidas(self):
        _, reason = _check(_historial(200, 3600, fallidas_cada=1))
        self.assertEqual(reason, "bot: 100% de transacciones fallidas")

    def test_historial_largo_queda_como_antes_de(self):
        # 2500 firmas, una cada 3 horas, pero solo se lee una página.
        buyer, reason = _check(_historial(2500, 3 * 3600), pages=1)
        self.assertIsNone(reason)
        self.assertFalse(buyer.history_complete)
        self.assertEqual(buyer.history_txs, SIGNATURES_PAGE)

    def test_split_separa_y_mantiene_orden(self):
        rpc = _HistoryRpc(_historial(300, 6 * 3600))
        buyers = [Buyer(BALLENA, sol_spent=5), Buyer(PEZ, sol_spent=3)]
        kept, discarded = asyncio.run(HistoryFilter(rpc).split(buyers))
        self.assertEqual([b.wallet for b in kept], [BALLENA, PEZ])
        self.assertEqual(discarded, [])


if __name__ == "__main__":
    unittest.main()
