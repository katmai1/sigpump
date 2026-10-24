"""Tests del resumen periódico del log, con un reloj falso."""

import unittest

from sigpump.summary import Summary


class _Reloj:
    def __init__(self):
        self.ahora = 0.0

    def __call__(self):
        return self.ahora


class TestSummary(unittest.TestCase):
    def test_toca_cada_periodo(self):
        reloj = _Reloj()
        summary = Summary(every_seconds=3600, clock=reloj)
        self.assertFalse(summary.due())
        reloj.ahora = 3600
        self.assertTrue(summary.due())
        summary.take(wallets=1, banned=0, websocket="conectado", rpc_calls=0)
        self.assertFalse(summary.due())

    def test_linea_y_vuelta_a_cero(self):
        reloj = _Reloj()
        summary = Summary(every_seconds=3600, clock=reloj)
        summary.buys, summary.sent, summary.send_errors = 12, 2, 1
        summary.no_pool, summary.in_cooldown = 3, 4
        summary.discarded.update({"min_txns_m5": 4, "dex_ids": 1})
        linea = summary.take(wallets=48, banned=2, websocket="conectado", rpc_calls=1500)
        self.assertEqual(
            linea,
            "Resumen de 1 h: 12 compras de wallets seguidas, 2 avisos (1 sin poder enviar), "
            "5 descartadas (min_txns_m5 4, dex_ids 1), 3 sin pool, 4 repetidas en cooldown"
            " | 48 wallets (2 castigadas), WebSocket conectado | 1500 llamadas al RPC",
        )
        # El siguiente empieza de cero y cuenta solo las llamadas nuevas.
        linea = summary.take(wallets=48, banned=0, websocket="caído", rpc_calls=1600)
        self.assertEqual(
            linea,
            "Resumen de 1 h: 0 compras de wallets seguidas, 0 avisos, 0 descartadas, 0 sin pool, "
            "0 repetidas en cooldown | 48 wallets, WebSocket caído | 100 llamadas al RPC",
        )


if __name__ == "__main__":
    unittest.main()
