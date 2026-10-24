"""Tests de la detección de compras por los saldos de la transacción."""

import unittest

from sigpump.wallets import WSOL_MINT, parse_buys
from tests.helpers import WALLET, OTRA, USDC, TOKEN, _saldo, _tx


class TestParseBuys(unittest.TestCase):
    def test_compra_con_sol_es_entrada_nueva(self):
        tx = _tx(post_tokens=[_saldo(TOKEN, 1_000)], sol_gastado=1.5)
        [buy] = parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1)
        self.assertEqual(buy.mint, TOKEN)
        # La comisión de red no cuenta como gasto de la compra.
        self.assertAlmostEqual(buy.sol_spent, 1.5)
        self.assertTrue(buy.new_position)
        self.assertEqual(buy.signature, "SIG")

    def test_ampliar_posicion(self):
        tx = _tx(
            pre_tokens=[_saldo(TOKEN, 500)], post_tokens=[_saldo(TOKEN, 900)], sol_gastado=0.5
        )
        [buy] = parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1)
        self.assertFalse(buy.new_position)

    def test_compra_pagando_con_wsol(self):
        tx = _tx(
            pre_tokens=[_saldo(WSOL_MINT, 2, decimals=9)],
            post_tokens=[_saldo(WSOL_MINT, 0, decimals=9), _saldo(TOKEN, 1_000)],
        )
        [buy] = parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1)
        self.assertAlmostEqual(buy.sol_spent, 2.0)

    def test_compra_con_usdc_cuenta_aunque_no_gaste_sol(self):
        tx = _tx(pre_tokens=[_saldo(USDC, 300)], post_tokens=[_saldo(USDC, 0), _saldo(TOKEN, 10)])
        [buy] = parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1)
        self.assertEqual(buy.mint, TOKEN)
        self.assertAlmostEqual(buy.stable_spent, 300)

    def test_airdrop_sin_pagar_no_es_compra(self):
        tx = _tx(post_tokens=[_saldo(TOKEN, 1_000_000)], sol_gastado=0.002)
        self.assertEqual(parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1), [])

    def test_venta_no_es_compra(self):
        tx = _tx(pre_tokens=[_saldo(TOKEN, 1_000)], post_tokens=[_saldo(TOKEN, 0)], sol_gastado=-1)
        self.assertEqual(parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1), [])

    def test_saldos_de_otra_wallet_no_cuentan(self):
        tx = _tx(post_tokens=[_saldo(TOKEN, 1_000, owner=OTRA)], sol_gastado=1)
        self.assertEqual(parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1), [])

    def test_transaccion_fallida(self):
        tx = _tx(post_tokens=[_saldo(TOKEN, 1_000)], sol_gastado=1, err={"InstructionError": []})
        self.assertEqual(parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1), [])

    def test_varios_tokens_se_reparten_lo_pagado(self):
        # Antes cada token llevaba los 2 SOL enteros: 4 SOL gastados en total.
        otro = "OtroMint111111111111111111111111111111111pump"
        tx = _tx(post_tokens=[_saldo(TOKEN, 1_000), _saldo(otro, 50)], sol_gastado=2)
        buys = parse_buys(tx, WALLET, "ballena", "SIG", min_sol=0.1)
        self.assertEqual({b.mint for b in buys}, {TOKEN, otro})
        self.assertEqual([b.sol_spent for b in buys], [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
