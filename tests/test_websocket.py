"""Tests del WebSocket de wallets: suscripciones, avisos, bots, abandonadas y pasada completa."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from sigpump.solana import RpcClient
from sigpump.wallets import MAX_TX_ATTEMPTS, WalletWatcher, only_transfers, ws_url
from tests.helpers import (
    conectar,
    desconectar,
    pedir_pasada_completa,
    pendientes,
    castigar,
    forzar_relectura,
    poner_actividad,
    poner_ultima_actividad,
    WALLET,
    OTRA,
    TOKEN,
    _abrir_negra,
    _permanentes,
    _motivos,
    _saldo,
    _tx,
    _FakeRpc,
    _RpcPorWallet,
    _FakeWs,
    _aviso,
    _LOGS_TRANSFERENCIA,
    _LOGS_SWAP,
    ConFicheroDeWallets,
)


class TestWebSocket(ConFicheroDeWallets):

    async def _conectado(self, rpc, **kwargs):
        """Watcher con la wallet ya suscrita (suscripción 77) y su punto de partida fijado."""
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1, **kwargs)
        await watcher.poll()
        conectar(watcher)
        ws = _FakeWs()
        await watcher.sync_subscriptions(ws)
        [pedida] = ws.enviados
        self.assertEqual(pedida["method"], "logsSubscribe")
        self.assertEqual(pedida["params"][0], {"mentions": [WALLET]})
        watcher.handle_message({"jsonrpc": "2.0", "id": pedida["id"], "result": 77})
        rpc.llamadas.clear()
        return watcher, ws

    async def test_bot_a_la_lista_negra_sin_leer_sus_transacciones(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, ws = await self._conectado(rpc, blacklist=negra, max_txs_per_hour=3)
        for i in range(3):
            watcher.handle_message(_aviso(77, f"TX{i}", logs=_LOGS_SWAP))
        self.assertIn(WALLET, watcher.wallets)
        # Las transferencias y las fallidas también cuentan.
        watcher.handle_message(_aviso(77, "SPAM", logs=_LOGS_TRANSFERENCIA))
        self.assertEqual(watcher.wallets, {})
        self.assertIn(WALLET, _permanentes(negra))
        self.assertIn("bot, 4 tx en 1h", _motivos(negra))
        # Los avisos que siguen llegando hasta darse de baja no la apuntan otra vez.
        watcher.handle_message(_aviso(77, "TX9", logs=_LOGS_SWAP))
        self.assertEqual(_motivos(negra).count(WALLET), 1)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas, [])
        await watcher.sync_subscriptions(ws)
        self.assertEqual(ws.enviados[-1]["method"], "logsUnsubscribe")

    async def test_bot_echado_en_la_pasada_completa_no_pierde_las_demas_compras(self):
        """Mientras la pasada completa lee otras wallets, el WebSocket aún
        apunta firmas del bot que esa pasada acaba de echar: leerlas daba un
        KeyError que perdía las compras de toda la vuelta."""
        self.path.write_text(f"{WALLET}  # bot\n{OTRA}  # ballena\n", encoding="utf-8")
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _RpcPorWallet({WALLET: ["VIEJA"], OTRA: ["VIEJA2"]})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_txs_per_hour=3)
        await watcher.poll()
        rpc.por_wallet = {WALLET: ["D", "C", "B", "A", "VIEJA"], OTRA: ["NUEVA", "VIEJA2"]}
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1, owner=OTRA)], sol_gastado=1, wallet=OTRA)
        rpc.al_leer = lambda: pendientes(watcher).setdefault(WALLET, []).append("TARDE")
        [buy] = await watcher.poll()
        self.assertEqual((buy.wallet, buy.signature), (OTRA, "NUEVA"))
        self.assertIn(WALLET, _permanentes(negra))
        self.assertEqual(pendientes(watcher), {})

    async def test_una_wallet_castigada_no_apunta_avisos_y_se_desuscribe(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, ws = await self._conectado(rpc)
        castigar(watcher, WALLET, time.time() + 3600)
        watcher.handle_message(_aviso(77, "NUEVA"))
        self.assertEqual(pendientes(watcher), {})
        await watcher.sync_subscriptions(ws)
        self.assertEqual(ws.enviados[-1]["method"], "logsUnsubscribe")

    async def test_un_error_inesperado_no_para_el_websocket(self):
        """Antes salía de listen() y el gather() de radar.run() paraba el bot."""
        class _Sesion:
            intentos = 0

            def ws_connect(self, url, heartbeat=None):
                self.intentos += 1
                raise RuntimeError("mensaje con forma rara")

        sesion = _Sesion()
        watcher = WalletWatcher(RpcClient(sesion, "http://rpc"), self.path, 0.1)
        with patch("sigpump.wallets.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await watcher.listen("wss://rpc/?api-key=SECRETO")
        self.assertEqual(sesion.intentos, 2)

    async def test_bot_sin_tope_se_sigue(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc, blacklist=negra)
        for i in range(50):
            watcher.handle_message(_aviso(77, f"TX{i}", logs=_LOGS_SWAP))
        self.assertIn(WALLET, watcher.wallets)

    async def test_bot_cuenta_solo_la_ultima_hora(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc, blacklist=negra, max_txs_per_hour=3)
        poner_actividad(watcher, WALLET, {f"VIEJA{i}": time.time() - 3700 for i in range(10)})
        watcher.handle_message(_aviso(77, "NUEVA", logs=_LOGS_SWAP))
        self.assertIn(WALLET, watcher.wallets)

    async def test_bot_detectado_en_la_pasada_completa(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_txs_per_hour=3)
        await watcher.poll()
        rpc.firmas = ["D", "C", "B", "A", "VIEJA"]
        rpc.llamadas.clear()
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual([m for m, _ in rpc.llamadas], ["getSignaturesForAddress"])
        self.assertIn(WALLET, _permanentes(negra))

    async def test_abandonada_a_la_lista_negra(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"], horas={"VIEJA": time.time() - 31 * 86400})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_inactive_seconds=30 * 86400)
        await watcher.poll()
        # Por otros 30 días: sigue en el fichero, pero no se consulta.
        self.assertIn(WALLET, watcher.wallets)
        self.assertAlmostEqual(negra.entries()[WALLET], time.time() + 30 * 86400, delta=5)
        self.assertIn("abandonada, 31 días sin actividad", _motivos(negra))
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n")
        rpc.llamadas.clear()
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])

    async def test_abandonada_se_vuelve_a_comprobar_al_acabar(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"], horas={"VIEJA": time.time() - 31 * 86400})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_inactive_seconds=30 * 86400)
        await watcher.poll()
        # Al acabar sigue sin operar: vuelve a entrar.
        negra._db().execute("UPDATE lista_negra SET hasta = ?", (time.time() - 1,))
        rpc.llamadas.clear()
        await watcher.poll()
        self.assertEqual([m for m, _ in rpc.llamadas], ["getSignaturesForAddress"])
        self.assertGreater(negra.entries()[WALLET], time.time() + 29 * 86400)
        # Ha vuelto a operar: se sigue otra vez, desde su última firma.
        negra._db().execute("UPDATE lista_negra SET hasta = ?", (time.time() - 1,))
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.horas["NUEVA"] = time.time() - 3600
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(negra.entries(), {})
        rpc.firmas = ["OTRA", "NUEVA", "VIEJA"]
        rpc.horas["OTRA"] = time.time()
        rpc.txs["OTRA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        self.assertEqual(len(await watcher.poll()), 1)

    async def test_activa_hace_poco_se_sigue(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"], horas={"VIEJA": time.time() - 29 * 86400})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_inactive_seconds=30 * 86400)
        await watcher.poll()
        self.assertIn(WALLET, watcher.wallets)

    async def test_se_abandona_mientras_se_sigue(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra, max_inactive_seconds=30 * 86400)
        await watcher.poll()
        self.assertIn(WALLET, watcher.wallets)
        poner_ultima_actividad(watcher, WALLET, time.time() - 31 * 86400)
        await watcher.poll()
        self.assertIn(WALLET, negra.entries())

    async def test_un_aviso_del_websocket_cuenta_como_actividad(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc, blacklist=negra, max_inactive_seconds=30 * 86400)
        poner_ultima_actividad(watcher, WALLET, time.time() - 31 * 86400)
        watcher.handle_message(_aviso(77, "SPAM", logs=_LOGS_TRANSFERENCIA))
        pedir_pasada_completa(watcher)
        await watcher.poll()
        self.assertIn(WALLET, watcher.wallets)

    async def test_abandonada_sin_lista_negra_se_sigue(self):
        rpc = _FakeRpc(firmas=["VIEJA"], horas={"VIEJA": time.time() - 365 * 86400})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, max_inactive_seconds=30 * 86400)
        await watcher.poll()
        self.assertIn(WALLET, watcher.wallets)

    def test_url_del_websocket(self):
        self.assertEqual(ws_url("https://mainnet.helius-rpc.com/?api-key=K"), "wss://mainnet.helius-rpc.com/?api-key=K")
        self.assertEqual(ws_url("http://localhost:8899"), "ws://localhost:8899")

    async def test_sin_avisos_no_consulta_nada(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas, [])

    async def test_un_aviso_lee_solo_esa_transaccion(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        watcher.handle_message(_aviso(77, "NUEVA"))
        [buy] = await watcher.poll()
        self.assertEqual(buy.signature, "NUEVA")
        # Directamente por la firma del aviso, sin getSignaturesForAddress.
        self.assertEqual([(m, p[0]) for m, p in rpc.llamadas], [("getTransaction", "NUEVA")])
        rpc.llamadas.clear()
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas, [])

    async def test_la_pasada_completa_no_relee_lo_avisado(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        rpc.firmas = ["SPAM", "NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        watcher.handle_message(_aviso(77, "NUEVA"))
        watcher.handle_message(_aviso(77, "SPAM", logs=_LOGS_TRANSFERENCIA))
        self.assertEqual(len(await watcher.poll()), 1)
        rpc.llamadas.clear()
        pedir_pasada_completa(watcher)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual([m for m, _ in rpc.llamadas], ["getSignaturesForAddress"])
        # Y la marca avanza: la siguiente pasada parte de SPAM.
        rpc.llamadas.clear()
        pedir_pasada_completa(watcher)
        await watcher.poll()
        self.assertEqual(rpc.llamadas[0][1][1]["until"], "SPAM")

    async def test_un_aviso_repetido_se_lee_una_vez(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        watcher.handle_message(_aviso(77, "NUEVA"))
        watcher.handle_message(_aviso(77, "NUEVA"))
        self.assertEqual(len(await watcher.poll()), 1)
        watcher.handle_message(_aviso(77, "NUEVA"))
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(len(rpc.llamadas), 1)

    async def test_las_transacciones_fallidas_no_marcan(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        watcher.handle_message(_aviso(77, err={"InstructionError": [0, "Custom"]}))
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])

    async def test_las_transferencias_no_marcan(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        watcher.handle_message(_aviso(77, logs=_LOGS_TRANSFERENCIA))
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])

    async def test_un_swap_marca(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        watcher.handle_message(_aviso(77, logs=_LOGS_SWAP))
        await watcher.poll()
        self.assertEqual([m for m, _ in rpc.llamadas], ["getTransaction"])

    def test_logs_truncados_no_se_filtran(self):
        self.assertTrue(only_transfers(_LOGS_TRANSFERENCIA))
        self.assertFalse(only_transfers(_LOGS_SWAP))
        self.assertFalse(only_transfers(_LOGS_TRANSFERENCIA + ["Log truncated"]))
        self.assertFalse(only_transfers(None))
        self.assertFalse(only_transfers([]))

    async def test_si_el_rpc_va_por_detras_reintenta_unas_vueltas(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        watcher.handle_message(_aviso(77, "NUEVA"))
        # El aviso llegó pero getTransaction aún no tiene la transacción.
        for _ in range(MAX_TX_ATTEMPTS - 1):
            self.assertEqual(await watcher.poll(), [])
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        self.assertEqual(len(await watcher.poll()), 1)

    async def test_si_el_rpc_nunca_la_tiene_se_salta(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        watcher.handle_message(_aviso(77, "NUEVA"))
        for _ in range(MAX_TX_ATTEMPTS + 2):
            await watcher.poll()
        self.assertEqual(len(rpc.llamadas), MAX_TX_ATTEMPTS)

    async def test_desconectado_consulta_todas(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        desconectar(watcher)
        await watcher.poll()
        self.assertEqual([m for m, _ in rpc.llamadas], ["getSignaturesForAddress"])

    async def test_pasada_completa_periodica(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        pedir_pasada_completa(watcher)
        await watcher.poll()
        self.assertEqual(len(rpc.llamadas), 1)
        rpc.llamadas.clear()
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])

    async def test_al_reconectar_los_ids_de_la_conexion_anterior_no_valen(self):
        class _WsQueCierra(_FakeWs):
            async def receive(self, timeout=None):
                return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE)

        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, _ = await self._conectado(rpc)
        ws = _WsQueCierra()
        await watcher._serve(ws)
        # La nueva conexión vuelve a suscribir la wallet desde cero...
        self.assertEqual([m["method"] for m in ws.enviados], ["logsSubscribe"])
        # ...y un aviso con la suscripción 77 de la anterior ya no se apunta.
        watcher.handle_message(_aviso(77, "NUEVA"))
        self.assertEqual(pendientes(watcher), {})

    async def test_quitar_una_wallet_del_fichero_la_desuscribe(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher, ws = await self._conectado(rpc)
        self.path.write_text("", encoding="utf-8")
        # Escrito en el mismo instante que la lectura anterior, el fichero
        # puede conservar la fecha: sin esto reload() no lo vería cambiado.
        forzar_relectura(watcher)
        watcher.reload()
        await watcher.sync_subscriptions(ws)
        self.assertEqual(ws.enviados[-1]["method"], "logsUnsubscribe")
        self.assertEqual(ws.enviados[-1]["params"], [77])
        watcher.handle_message(_aviso(77))
        self.assertEqual(pendientes(watcher), {})


if __name__ == "__main__":
    unittest.main()
