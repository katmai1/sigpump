"""Tests de los avisos de compras de wallets: filtros, cooldowns, confluencia, lista negra y registro."""

import asyncio
import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from sigpump.radar import RANKING_MIN_TOKENS, MemecoinRadar, next_daily
from sigpump.solana import RpcClient
from sigpump.telegram import TelegramAlerter
from sigpump.wallet_list import FollowedWallet
from sigpump.wallets import WalletSignal, WalletWatcher
from tests.test_radar import _FakeClient, _config, _hace_horas, _pair
from tests.helpers import (
    WALLET,
    OTRA,
    _abrir_negra,
    _permanentes,
    _motivos,
    _buy,
    _FakeWatcher,
    _FakeAutoridades,
    _FakeAlerter,
)


class TestAvisosDeWallets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "alertas.db"

    def _radar(self, *rondas, unsafe=None, fees=None, **kwargs):
        radar = MemecoinRadar(_config(alert_log_path=str(self.path), **kwargs))
        self.addCleanup(radar._tracker.close)
        radar._wallet_watcher = _FakeWatcher(*rondas)
        radar._authorities = _FakeAutoridades(unsafe, fees)
        return radar

    def _seguir(self, radar, wallets_txt, negra):
        """Un watcher de verdad (sin red) sobre `wallets_txt` y la lista negra
        `negra`, ya cargado: para los tests de la lista negra automática."""
        radar._wallet_watcher = WalletWatcher(RpcClient(None, "http://rpc"), wallets_txt, 0.1, negra)
        radar._wallet_watcher.reload()
        return radar._wallet_watcher

    def _anotar(self, radar, row_id, cambios):
        """Cambia columnas de una fila del registro (retornos, lista negra...)."""
        radar._tracker.update_row(row_id, cambios)

    def _rows(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM alertas")]
        finally:
            conn.close()

    async def test_al_salir_cierra_las_bases(self):
        # Antes las dos conexiones a la base quedaban abiertas hasta que moría el proceso.
        radar = MemecoinRadar(_config(alert_log_path=str(self.path), wallets_blacklist=True))
        abiertas = {}

        async def seguir(blacklist):
            blacklist.entries()  # abre su conexión
            abiertas["negra"] = blacklist
            raise asyncio.CancelledError

        radar._follow = seguir
        with self.assertRaises(asyncio.CancelledError):
            await radar.run()
        self.assertIsNone(radar._tracker._conn)
        self.assertIsNone(abiertas["negra"]._conn)

    async def test_avisa_y_registra_la_compra(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[{**_pair("TOK"), "dexId": "pumpswap"}]), alerter)
        [(pair, signal)] = alerter.wallet
        self.assertEqual(signal.buy.label, "ballena")
        fila = self._rows()[0]
        self.assertEqual((fila["tipo"], fila["enviada"]), ("wallet", 1))
        self.assertEqual(fila["wallet"], WALLET)
        self.assertEqual(fila["sol_gastado"], 1.5)
        self.assertEqual(fila["entrada_nueva"], 1)

    async def test_el_aviso_lleva_el_historial_de_la_wallet(self):
        radar = self._radar([_buy(mint="T0")], [_buy(mint="T1")], [_buy(wallet=OTRA, mint="T2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair(f"T{i}") for i in range(3)])
        await radar.run_once(client, alerter)
        self._anotar(radar, self._rows()[0]["id"], {"ret_60m_pct": 12, "ret_1440m_pct": -30})
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(
            [s.track_record for _, s in alerter.wallet], [(), ((60, 12, 1), (1440, -30, 1)), ()]
        )

    async def test_ranking_de_las_wallets_seguidas(self):
        tercera = "9Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j3"
        radar = self._radar(wallets_ranking_hour=9)
        radar._wallet_watcher.wallets = {
            WALLET: FollowedWallet("ballena", trusted=True),
            OTRA: FollowedWallet("ballena 2"),
            tercera: FollowedWallet("nueva"),
        }
        for wallet, ret in [(WALLET, -10), (OTRA, 20)]:
            for i in range(RANKING_MIN_TOKENS):
                row_id = radar._tracker.record(_pair(f"T{i}"), sent=True)
                self._anotar(radar, row_id, {"wallet": wallet, "token": f"T{i}", "ret_60m_pct": ret})
        # Una sola compra medida no basta para entrar.
        row_id = radar._tracker.record(_pair("X"), sent=True)
        self._anotar(radar, row_id, {"wallet": tercera, "token": "X", "ret_60m_pct": 99})
        alerter = _FakeAlerter()
        radar._next_ranking = 0.0
        await radar.send_ranking(alerter)
        [texto] = alerter.textos
        self.assertIn("1. ballena 2: +20% a 1 h", texto)
        self.assertIn("2. ⭐ ballena: -10% a 1 h", texto)
        self.assertNotIn("nueva", texto)
        self.assertIn(": 1 wallets", texto)
        self.assertGreater(radar._next_ranking, time.time())

    async def test_ranking_apagado_por_defecto(self):
        self.assertIsNone(self._radar()._next_ranking)
        self.assertIsNotNone(self._radar(wallets_ranking_hour=0)._next_ranking)

    def test_proxima_hora_del_ranking(self):
        from datetime import datetime
        antes = datetime(2026, 10, 24, 8, 30).timestamp()
        self.assertEqual(next_daily(antes, 9), datetime(2026, 10, 24, 9).timestamp())
        # Justo a la hora (recién mandado) pasa al día siguiente.
        a_las_9 = datetime(2026, 10, 24, 9).timestamp()
        self.assertEqual(next_daily(a_las_9, 9), datetime(2026, 10, 25, 9).timestamp())

    async def test_la_comision_de_transferencia_llega_al_aviso_y_al_registro(self):
        radar = self._radar([_buy()], [_buy(mint="SIN")], fees={"TOK": 300})
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK"), _pair("SIN")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual([s.transfer_fee_bps for _, s in alerter.wallet], [300, 0])
        self.assertEqual([f["comision_transferencia_bps"] for f in self._rows()], [300, 0])

    async def test_sin_comprobacion_de_autoridades_la_comision_queda_vacia(self):
        radar = self._radar([_buy()])
        radar._authorities = None
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), alerter)
        self.assertIsNone(alerter.wallet[0][1].transfer_fee_bps)
        self.assertIsNone(self._rows()[0]["comision_transferencia_bps"])

    async def test_el_resumen_cuenta_cada_caso(self):
        radar = self._radar(
            [_buy(), _buy(mint="SIN_POOL"), _buy(wallet=OTRA, label="b2", mint="MALO")],
            [_buy()],  # misma wallet y token: en cooldown
            unsafe={"MALO": "el creador conserva autoridades: se pueden congelar"},
        )
        client = _FakeClient(pairs=[_pair("TOK"), _pair("MALO")])
        await radar.run_once(client, _FakeAlerter())
        await radar.run_once(client, _FakeAlerter())
        summary = radar._summary
        self.assertEqual(
            (summary.buys, summary.sent, summary.no_pool, summary.in_cooldown, dict(summary.discarded)),
            (4, 1, 1, 1, {"autoridades": 1}),
        )
        logging.disable(logging.NOTSET)  # el setUp lo silencia
        with self.assertLogs(level=logging.INFO) as logs:
            radar._log_summary()
        self.assertIn("4 compras de wallets seguidas, 1 avisos", logs.output[0])
        self.assertIn("WebSocket conectado | 0 llamadas al RPC", logs.output[0])

    async def test_los_descartes_del_par_se_agrupan_por_su_clave(self):
        radar = self._radar([_buy()], wallets_min_market_cap_usd=5_000_000)
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), _FakeAlerter())
        self.assertEqual(dict(radar._summary.discarded), {"min_market_cap_usd": 1})

    async def test_ignora_tokens_en_bonding_curve(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[{**_pair("TOK"), "dexId": "pumpfun"}]), alerter)
        self.assertEqual(alerter.wallet, [])
        self.assertEqual(self._rows(), [])

    async def test_con_bonding_curve_avisa_sin_los_filtros_de_amm(self):
        """Un token sin graduar es joven, de market cap chico y no está en
        pumpswap/raydium: esos filtros no se le aplican."""
        radar = self._radar(
            [_buy()], wallets_bonding_curve=True, dex_ids=["pumpswap", "raydium"],
            wallets_min_pair_age_minutes=120, wallets_min_market_cap_usd=100_000,
            wallets_max_turnover_h1=0.05,
        )
        alerter = _FakeAlerter()
        pair = {**_pair("TOK", created_at=_hace_horas(0.2), change=10), "dexId": "pumpfun", "marketCap": 30_000}
        await radar.run_once(_FakeClient(pairs=[pair]), alerter)
        [(_, signal)] = alerter.wallet
        self.assertTrue(signal.bonding_curve)
        [fila] = self._rows()
        self.assertEqual((fila["enviada"], fila["bonding_curve"], fila["pasa_filtros_par"]), (1, 1, 1))

    async def test_con_bonding_curve_aplica_actividad_y_cambio_1h(self):
        casos = [
            ({"txns": {"m5": {"buys": 1, "sells": 0}}}, "1 txns en 5 min < 5"),
            ({"priceChange": {"h1": 80}}, "cambio 1h +80% > 50%"),
        ]
        for extra, motivo in casos:
            with self.subTest(motivo=motivo):
                radar = self._radar(
                    [_buy()], wallets_bonding_curve=True, wallets_min_txns_m5=5,
                    wallets_max_price_change_h1_pct=50,
                )
                alerter = _FakeAlerter()
                pair = {**_pair("TOK", change=10), "dexId": "pumpfun", **extra}
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(alerter.wallet, [])
                self.assertEqual(self._rows()[-1]["motivo_descarte"], motivo)

    async def test_con_bonding_curve_prefiere_el_pool_del_amm(self):
        radar = self._radar([_buy()], wallets_bonding_curve=True)
        alerter = _FakeAlerter()
        curva = {**_pair("TOK", liquidity=900_000), "dexId": "pumpfun"}
        amm = {**_pair("TOK", liquidity=5_000), "dexId": "pumpswap"}
        await radar.run_once(_FakeClient(pairs=[curva, amm]), alerter)
        [(pair, signal)] = alerter.wallet
        self.assertEqual((pair["dexId"], signal.bonding_curve), ("pumpswap", False))

    async def test_rug_en_bonding_curve_no_echa_a_la_wallet(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")
        radar = self._radar(
            [_buy()], wallets_bonding_curve=True, wallets_blacklist=True, wallets_rug_drop_pct=90,
        )
        await radar.run_once(_FakeClient(pairs=[{**_pair("TOK"), "dexId": "pumpfun"}]), _FakeAlerter())
        self._anotar(radar, self._rows()[0]["id"], {"peor_ret_30m_pct": -97.0})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_ruggers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [WALLET])

    async def test_descarta_compras_fuera_de_los_dex_y_quote_pedidos(self):
        sol = {"symbol": "SOL", "address": "So11111111111111111111111111111111111111112"}
        usdc = {"symbol": "USDC", "address": "EPjF"}
        casos = (("meteora", sol, "par en meteora"), ("pumpswap", usdc, "par contra USDC"))
        for dex, quote, motivo in casos:
            with self.subTest(dex=dex, quote=quote["symbol"]):
                self.path.unlink(missing_ok=True)
                radar = self._radar([_buy()], dex_ids=["pumpswap", "raydium"], quote_tokens=["SOL"])
                alerter = _FakeAlerter()
                pair = {**_pair("TOK"), "dexId": dex, "quoteToken": quote}
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(alerter.wallet, [])
                [fila] = self._rows()
                self.assertEqual(fila["enviada"], 0)
                self.assertIn(motivo, fila["motivo_descarte"])
                radar._tracker.close()

    async def test_sin_pool_en_dexscreener_se_ignora(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[]), alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_descarta_tokens_minteables_o_congelables(self):
        radar = self._radar([_buy()], unsafe={"TOK": "se pueden congelar"})
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), alerter)
        self.assertEqual(alerter.wallet, [])
        fila = self._rows()[0]
        self.assertEqual(fila["enviada"], 0)
        self.assertIn("congelar", fila["motivo_descarte"])

    async def test_no_repite_la_misma_wallet_en_el_mismo_token(self):
        radar = self._radar([_buy(), _buy()], [_buy()])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(len(alerter.wallet), 1)

    async def test_confluencia_de_otra_wallet(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA, label="ballena 2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual([s.others for _, s in alerter.wallet], [(), ("ballena",)])
        self.assertEqual(self._rows()[1]["wallets_confluencia"], 1)

    async def test_confluencia_marca_las_wallets_de_confianza(self):
        radar = self._radar([_buy(trusted=True)], [_buy(wallet=OTRA, label="ballena 2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(alerter.wallet[-1][1].others, ("⭐ ballena",))

    async def test_la_confluencia_caduca(self):
        viejo = time.time() - 2 * 3600
        radar = self._radar([_buy(ts=viejo)], [_buy(wallet=OTRA, label="ballena 2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(alerter.wallet[-1][1].others, ())

    async def test_min_wallets_espera_a_la_segunda_wallet(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA, label="ballena 2")], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        self.assertEqual(alerter.wallet, [])
        await radar.run_once(client, alerter)
        [(_, signal)] = alerter.wallet
        self.assertEqual((signal.buy.label, signal.others), ("ballena 2", ("ballena",)))
        filas = self._rows()
        self.assertEqual([(f["enviada"], f["motivo_descarte"]) for f in filas],
                         [(0, "1 de 2 wallets"), (1, None)])

    async def test_min_wallets_no_cuenta_dos_veces_la_misma_wallet(self):
        radar = self._radar([_buy()], [_buy()], wallets_min_wallets=2, wallets_cooldown_minutes=0)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_dos_wallets_con_la_misma_etiqueta_cuentan_como_dos(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA)], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        self.assertEqual(len(alerter.wallet), 1)

    async def test_la_confluencia_sobrevive_a_un_reinicio(self):
        radar = self._radar([_buy()], wallets_min_wallets=2)
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(wallet=OTRA, label="ballena 2")], wallets_min_wallets=2)
        reiniciado.restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado.run_once(client, alerter)
        [(_, signal)] = alerter.wallet
        self.assertEqual(signal.others, ("ballena",))

    async def test_la_estrella_de_confianza_sobrevive_a_un_reinicio(self):
        # Las compras recuperadas del registro no guardan si la wallet es de
        # confianza: se mira en el fichero de ahora.
        radar = self._radar([_buy(trusted=True)])
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(wallet=OTRA, label="ballena 2")])
        reiniciado._wallet_watcher.wallets = {WALLET: FollowedWallet("ballena", trusted=True)}
        reiniciado.restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado.run_once(client, alerter)
        self.assertEqual(alerter.wallet[-1][1].others, ("⭐ ballena",))

    async def test_wallet_de_confianza_avisa_sola(self):
        radar = self._radar([_buy(trusted=True)], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), alerter)
        self.assertEqual(len(alerter.wallet), 1)

    async def test_un_aviso_por_token_salvo_que_entren_mas_wallets(self):
        tercera = "8Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j8"
        cuarta = "9Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j7"
        radar = self._radar(
            [_buy()], [_buy(wallet=OTRA)], [_buy(wallet=tercera)], [_buy(wallet=cuarta)],
            wallets_token_cooldown_minutes=120, wallets_realert_new_wallets=2,
        )
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        for _ in range(4):
            await radar.run_once(client, alerter)
        # 1 wallet: aviso; 2: ya avisado; 3: dos más que en el aviso, actualización.
        self.assertEqual([(1 + len(s.others), s.update) for _, s in alerter.wallet], [(1, False), (3, True)])
        self.assertEqual(
            [f["motivo_descarte"] for f in self._rows()],
            [None, "token ya avisado con 1 wallets", None, "token ya avisado con 3 wallets"],
        )

    async def test_realert_cero_no_reavisa_aunque_entren_mas_wallets(self):
        tercera = "8Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j8"
        radar = self._radar(
            [_buy()], [_buy(wallet=OTRA)], [_buy(wallet=tercera)],
            wallets_token_cooldown_minutes=120, wallets_realert_new_wallets=0,
        )
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        for _ in range(3):
            await radar.run_once(client, alerter)
        self.assertEqual(len(alerter.wallet), 1)
        self.assertEqual(
            [f["motivo_descarte"] for f in self._rows()],
            [None, "token ya avisado con 1 wallets", "token ya avisado con 1 wallets"],
        )

    async def test_el_aviso_por_token_sobrevive_a_un_reinicio(self):
        radar = self._radar([_buy()], wallets_token_cooldown_minutes=120)
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(wallet=OTRA)], wallets_token_cooldown_minutes=120)
        reiniciado.restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado.run_once(client, alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_el_cooldown_de_la_wallet_sobrevive_a_un_reinicio(self):
        radar = self._radar([_buy()], wallets_cooldown_minutes=1440)
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar.run_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(), _buy(wallet=OTRA)], wallets_cooldown_minutes=1440)
        reiniciado.restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado.run_once(client, alerter)
        # La recompra de la misma wallet no avisa; la de otra wallet sí.
        self.assertEqual([s.buy.wallet for _, s in alerter.wallet], [OTRA])

    async def test_wallet_hiperactiva_no_avisa_ni_cuenta_para_la_confluencia(self):
        compras = [_buy(mint=f"T{i}") for i in range(3)] + [_buy(mint="TOK")]
        radar = self._radar(compras, [_buy(wallet=OTRA)], wallets_max_tokens_per_hour=3)
        alerter = _FakeAlerter()
        pairs = [_pair(m) for m in ("T0", "T1", "T2", "TOK")]
        client = _FakeClient(pairs=pairs)
        await radar.run_once(client, alerter)
        await radar.run_once(client, alerter)
        motivos = [f["motivo_descarte"] for f in self._rows()]
        self.assertEqual(motivos[:4], ["wallet hiperactiva (4 tokens en 1h)"] * 4)
        [(_, signal)] = alerter.wallet
        self.assertEqual((signal.buy.wallet, signal.others), (OTRA, ()))

    async def test_wallet_hiperactiva_va_a_la_lista_negra(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n{OTRA}  # buena\n", encoding="utf-8")
        compras = [_buy(mint=f"T{i}") for i in range(4)]
        radar = self._radar(wallets_blacklist=True, wallets_max_tokens_per_hour=3)
        watcher = WalletWatcher(RpcClient(None, "http://rpc"), wallets_txt, 0.1, negra)
        watcher.reload()
        watcher.poll = AsyncMock(return_value=compras)
        radar._wallet_watcher = watcher
        await radar.run_once(_FakeClient(pairs=[_pair(f"T{i}") for i in range(4)]), _FakeAlerter())
        self.assertIn(WALLET, _permanentes(negra))
        self.assertEqual(_motivos(negra).count(WALLET), 1)
        self.assertIn("ballena: hiperactiva, 4 tokens en 1h", _motivos(negra))
        watcher.reload()
        self.assertEqual(list(watcher.wallets), [OTRA])
        self.assertEqual(wallets_txt.read_text(encoding="utf-8"), f"{OTRA}  # buena\n")

    async def test_wallet_que_pierde_va_a_la_lista_negra(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n{OTRA}  # buena\n", encoding="utf-8")
        compras = [_buy(mint=f"T{i}") for i in range(3)] + [_buy(wallet=OTRA, mint=f"T{i}") for i in range(3)]
        radar = self._radar(
            compras, wallets_blacklist=True,
            wallets_loser_min_signals=3, wallets_loser_max_median_ret_pct=-30,
        )
        await radar.run_once(_FakeClient(pairs=[_pair(f"T{i}") for i in range(3)]), _FakeAlerter())
        rows = self._rows()
        for row, ret in zip(rows, [-50, -40, 10, -50, 20, 30]):
            self._anotar(radar, row["id"], {"ret_30m_pct": ret})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_losers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [OTRA])
        self.assertIn("ballena: mediana -40% a 30 min en 3 tokens", _motivos(negra))

    async def test_wallet_que_pierde_se_mide_con_el_checkpoint_pedido(self):
        """A 30 min la mediana es buena, pero a 24 h se hundieron: con
        loser_checkpoint_minutes = 1440 cuenta la de 24 h."""
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")
        compras = [_buy(mint=f"T{i}") for i in range(3)]
        radar = self._radar(
            compras, wallets_blacklist=True, wallets_loser_min_signals=3,
            wallets_loser_max_median_ret_pct=-40, wallets_loser_checkpoint_minutes=1440,
        )
        await radar.run_once(_FakeClient(pairs=[_pair(f"T{i}") for i in range(3)]), _FakeAlerter())
        for row, ret in zip(self._rows(), [-80, -60, 5]):
            self._anotar(radar, row["id"], {"ret_30m_pct": 3, "ret_1440m_pct": ret})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_losers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [])
        self.assertIn("ballena: mediana -60% a 24 h en 3 tokens", _motivos(negra))

    async def test_wallet_con_pocas_senales_no_se_juzga(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")
        radar = self._radar(
            [_buy()], wallets_blacklist=True,
            wallets_loser_min_signals=3, wallets_loser_max_median_ret_pct=-30,
        )
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), _FakeAlerter())
        self._anotar(radar, self._rows()[0]["id"], {"ret_30m_pct": -90})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_losers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [WALLET])

    async def test_filtros_del_par_para_wallets(self):
        casos = [
            (_pair("TOK", created_at=_hace_horas(0.5)), "par creado hace 30 min"),
            (_pair("TOK", created_at=_hace_horas(5), change=80), "cambio 1h +80% > 50%"),
            ({**_pair("TOK", created_at=_hace_horas(5)), "marketCap": 50_000}, "market cap $50,000 < $100,000"),
            (_pair("TOK", created_at=_hace_horas(5), change=10), ""),
        ]
        for pair, motivo in casos:
            with self.subTest(motivo=motivo):
                radar = self._radar(
                    [_buy()], wallets_min_pair_age_minutes=120,
                    wallets_max_price_change_h1_pct=50, wallets_min_market_cap_usd=100_000,
                )
                alerter = _FakeAlerter()
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(self._rows()[-1]["motivo_descarte"] or "", motivo)
                self.assertEqual(len(alerter.wallet), 0 if motivo else 1)

    async def test_wallet_de_confianza_se_salta_cambio_market_cap_y_turnover(self):
        casos = [
            (_pair("TOK", created_at=_hace_horas(5), change=80), ""),
            ({**_pair("TOK", created_at=_hace_horas(5)), "marketCap": 50_000}, ""),
            (_pair("TOK", created_at=_hace_horas(5), volume=50_000), ""),
            ({**_pair("TOK", created_at=_hace_horas(5)), "txns": {"m5": {"buys": 1, "sells": 0}}}, ""),
            (_pair("TOK", created_at=_hace_horas(0.5)), "par creado hace 30 min"),
            ({**_pair("TOK", created_at=_hace_horas(5)), "priceChange": {"m5": 25, "h1": 10}},
             "cambio 5m +25% > 20%"),
            ({**_pair("TOK", created_at=_hace_horas(5)), "quoteToken": {"symbol": "PUMP"}},
             "par contra PUMP, no contra SOL"),
        ]
        for pair, motivo in casos:
            with self.subTest(motivo=motivo):
                radar = self._radar(
                    [_buy(trusted=True)], wallets_min_pair_age_minutes=120,
                    wallets_max_price_change_h1_pct=50, wallets_min_market_cap_usd=100_000,
                    wallets_max_turnover_h1=0.03, wallets_min_txns_m5=5, quote_tokens=["SOL"],
                    wallets_max_price_change_m5_pct=20,
                )
                alerter = _FakeAlerter()
                pair = {"quoteToken": {"symbol": "SOL"}, **pair}
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(self._rows()[-1]["motivo_descarte"] or "", motivo)
                self.assertEqual(len(alerter.wallet), 0 if motivo else 1)

    async def test_filtros_de_actividad_del_par_para_wallets(self):
        casos = [
            ({**_pair("TOK"), "txns": {"m5": {"buys": 1, "sells": 0}}}, "1 txns en 5 min < 5"),
            (_pair("TOK", volume=300), "volumen 1h $300 < $5,000"),
            ({k: v for k, v in _pair("TOK").items() if k not in ("txns", "volume")}, ""),
            (_pair("TOK"), ""),
        ]
        for pair, motivo in casos:
            with self.subTest(motivo=motivo):
                radar = self._radar([_buy()], wallets_min_txns_m5=5, wallets_min_volume_h1_usd=5_000)
                alerter = _FakeAlerter()
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(self._rows()[-1]["motivo_descarte"] or "", motivo)
                self.assertEqual(len(alerter.wallet), 0 if motivo else 1)

    async def test_filtro_de_volumen_sobre_market_cap(self):
        casos = [
            (_pair("TOK", volume=50_000), "volumen 1h 5% del market cap > 3%"),
            (_pair("TOK", volume=20_000), ""),
            ({**_pair("TOK", volume=50_000), "marketCap": 0, "fdv": 0}, ""),
        ]
        for pair, motivo in casos:
            with self.subTest(motivo=motivo):
                radar = self._radar([_buy()], wallets_max_turnover_h1=0.03)
                alerter = _FakeAlerter()
                await radar.run_once(_FakeClient(pairs=[pair]), alerter)
                self.assertEqual(self._rows()[-1]["motivo_descarte"] or "", motivo)
                self.assertEqual(len(alerter.wallet), 0 if motivo else 1)

    async def test_wallet_que_compra_un_rug_va_a_la_lista_negra(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n{OTRA}  # buena\n", encoding="utf-8")
        radar = self._radar([_buy()], wallets_blacklist=True, wallets_rug_drop_pct=90)
        await radar.run_once(_FakeClient(pairs=[_pair("TOK", symbol="NGU")]), _FakeAlerter())
        self._anotar(radar, self._rows()[0]["id"], {"peor_ret_30m_pct": -96.0})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_ruggers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [OTRA])
        self.assertIn("rug NGU -96%", _motivos(negra))
        radar._wallet_watcher.reload()
        self.assertEqual(wallets_txt.read_text(encoding="utf-8"), f"{OTRA}  # buena\n")

    async def test_rug_en_un_par_descartado_no_cuenta_pero_las_perdidas_si(self):
        """Antes una sola compra en un par de 3 minutos que hacía rug echaba a
        la wallet, aunque ese par nunca se iba a avisar. Para la mediana de
        resultados sí cuentan: elegir mal es elegir mal aunque no se avise."""
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", _abrir_negra(self, tmp / "negra.db")
        wallets_txt.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")
        compras = [_buy(mint=f"T{i}") for i in range(3)]
        radar = self._radar(
            compras, wallets_blacklist=True, wallets_rug_drop_pct=90,
            wallets_loser_min_signals=3, wallets_loser_max_median_ret_pct=-30,
            wallets_min_pair_age_minutes=120,
        )
        pairs = [_pair(f"T{i}", created_at=_hace_horas(0.05)) for i in range(3)]
        await radar.run_once(_FakeClient(pairs=pairs), _FakeAlerter())
        for row in self._rows():
            self.assertEqual(row["pasa_filtros_par"], 0)
            self._anotar(radar, row["id"], {"ret_30m_pct": -90, "peor_ret_30m_pct": -96.0})
        self._seguir(radar, wallets_txt, negra)
        radar._blacklist_ruggers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [WALLET])
        self.assertEqual(_motivos(negra), "")
        radar._blacklist_losers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [])
        self.assertIn("ballena: mediana -90% a 30 min en 3 tokens", _motivos(negra))

    async def test_descarte_por_confluencia_pasa_los_filtros_del_par(self):
        radar = self._radar([_buy()], wallets_min_wallets=2)
        await radar.run_once(_FakeClient(pairs=[_pair("TOK")]), _FakeAlerter())
        [fila] = self._rows()
        self.assertEqual((fila["motivo_descarte"], fila["pasa_filtros_par"]), ("1 de 2 wallets", 1))

    async def test_filas_anteriores_se_marcan_con_los_filtros_de_ahora(self):
        radar = self._radar()
        casos = [
            # (enviada, motivo, moneda, edad en min, pasa)
            (1, None, "SOL", 300, True),
            (1, None, "SOL", 4, False),  # enviada antes de que hubiera filtro de edad
            (0, "1 de 2 wallets", "SOL", 300, True),
            (0, "1 de 2 wallets", "SOL", 4, False),
            (0, "token ya avisado con 2 wallets", "SOL", 300, True),
            (0, "par creado hace 3 min", "SOL", 3, False),
            (0, "par en meteora, no en pumpswap o raydium", "SOL", 300, False),
            (0, "par contra USDC, no contra SOL", "USDC", 300, False),
        ]
        ids = []
        for enviada, motivo, moneda, edad, _ in casos:
            row_id = radar._tracker.record(_pair("TOK"), sent=bool(enviada), reason=motivo or "")
            self._anotar(radar, row_id, {"moneda_par": moneda, "edad_par_min": edad, "wallet": WALLET})
            ids.append(row_id)
        radar._tracker.close()
        radar = self._radar(
            wallets_min_pair_age_minutes=120, quote_tokens=["SOL"], dex_ids=["pumpswap", "raydium"],
        )
        radar._mark_old_rows()
        marcas = {r["id"]: r["pasa_filtros_par"] for r in self._rows()}
        for row_id, (enviada, motivo, moneda, edad, pasa) in zip(ids, casos):
            with self.subTest(motivo=motivo, edad=edad):
                self.assertEqual(marcas[row_id], int(pasa))

    async def test_cada_vuelta_muestrea_el_precio_de_las_senales(self):
        radar = self._radar()
        radar._tracker.update = AsyncMock()
        await radar.run_once(_FakeClient(pairs=[]), _FakeAlerter())
        radar._tracker.update.assert_awaited_once()

class TestMensajeDeWallet(unittest.TestCase):
    def test_encabezado_con_compra_y_confluencia(self):
        alerter = TelegramAlerter("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", "-100123")
        signal = WalletSignal(_buy(label="<ballena>", new=False), others=("otra",))
        text = alerter.format_message(_pair("TOK", symbol="PEPE"), signal)
        self.assertIn("&lt;ballena&gt; compró PEPE", text)
        self.assertIn("Amplía posición", text)
        self.assertIn("1.50 SOL", text)
        self.assertIn("https://solscan.io/tx/SIG", text)
        self.assertIn("También entraron: <b>otra</b>", text)
        self.assertNotIn("ACTUALIZACIÓN", text)
        self.assertNotIn("Score", text)

    def test_encabezado_de_actualizacion_y_confianza(self):
        alerter = TelegramAlerter("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", "-100123")
        signal = WalletSignal(_buy(trusted=True), others=("a", "b"), update=True)
        text = alerter.format_message(_pair("TOK", symbol="PEPE"), signal)
        self.assertTrue(text.startswith("🔁 <b>ACTUALIZACIÓN</b>"))
        self.assertIn("⭐ ballena compró", text)


if __name__ == "__main__":
    unittest.main()
