"""Tests del sondeo de wallets: firmas nuevas, primera vuelta, castigos y transacciones que fallan."""

import time

from sigpump.solana import RpcClient
from sigpump.wallets import MAX_TRANSACTION_VERSION, MAX_TX_ATTEMPTS, WalletWatcher
from tests.helpers import (
    castigar,
    WALLET,
    TOKEN,
    _abrir_negra,
    _permanentes,
    _saldo,
    _tx,
    _FakeRpc,
    ConFicheroDeWallets,
)


class TestWalletWatcher(ConFicheroDeWallets):
    async def test_blacklist_deja_de_seguir_y_quita_del_fichero(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        watcher = WalletWatcher(RpcClient(_FakeRpc(firmas=[]), "http://rpc"), self.path, 0.1, negra)
        watcher.reload()
        watcher.blacklist(WALLET, "ballena: rug NGU -96%")
        self.assertEqual(watcher.wallets, {})
        watcher.reload()
        self.assertEqual(watcher.wallets, {})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertIn(WALLET, _permanentes(negra))

    async def test_blacklist_temporal_deja_de_consultarla_hasta_que_acaba(self):
        negra = _abrir_negra(self, self.path.with_name("negra.db"))
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, negra)
        await watcher.poll()
        watcher.blacklist(WALLET, "ballena: 12 tokens en 1h", seconds=3600)
        rpc.llamadas.clear()
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])
        self.assertIn(WALLET, watcher.wallets)
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n")
        # Al acabar vuelve a consultarse, desde su última firma.
        castigar(watcher, WALLET, time.time() - 1)
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas[0][1][1]["limit"], 1)

    async def test_la_confianza_llega_a_la_compra(self):
        self.path.write_text(f"{WALLET} *  # ballena\n", encoding="utf-8")
        rpc = _FakeRpc(firmas=["VIEJA"], txs={})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        [buy] = await watcher.poll()
        self.assertTrue(buy.trusted)


    async def test_la_primera_vuelta_no_avisa_del_historial(self):
        rpc = _FakeRpc(firmas=["VIEJA"], txs={"VIEJA": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)})
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        self.assertEqual(await watcher.poll(), [])
        self.assertNotIn("getTransaction", [m for m, _ in rpc.llamadas])

    async def test_procesa_solo_las_firmas_nuevas_en_orden(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA2", "NUEVA1", "VIEJA"]
        rpc.txs = {
            "NUEVA1": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1),
            "NUEVA2": _tx(post_tokens=[_saldo("OtroMint", 1)], sol_gastado=1),
        }
        buys = await watcher.poll()
        self.assertEqual([b.signature for b in buys], ["NUEVA1", "NUEVA2"])
        self.assertEqual(buys[0].label, "ballena")
        rpc.llamadas.clear()
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas[0][1][1]["until"], "NUEVA2")

    async def test_una_tx_aun_no_disponible_se_reintenta(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        # getTransaction devuelve null: el nodo todavía no la tiene.
        self.assertEqual(await watcher.poll(), [])
        rpc.txs = {"NUEVA": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)}
        self.assertEqual(len(await watcher.poll()), 1)

class TestTransaccionesQueFallan(ConFicheroDeWallets):

    async def test_se_salta_tras_varios_intentos_y_sigue_con_las_demas(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        await watcher.poll()
        # ROTA nunca se puede leer (p. ej. una versión de transacción nueva).
        rpc.firmas = ["BUENA", "ROTA", "VIEJA"]
        rpc.txs = {"BUENA": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)}
        for _ in range(MAX_TX_ATTEMPTS - 1):
            self.assertEqual(await watcher.poll(), [])
        [buy] = await watcher.poll()
        self.assertEqual(buy.signature, "BUENA")

    async def test_pide_la_version_de_transaccion_soportada(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        await watcher.poll()
        [params] = [p for m, p in rpc.llamadas if m == "getTransaction"]
        self.assertEqual(params[1]["maxSupportedTransactionVersion"], MAX_TRANSACTION_VERSION)


if __name__ == "__main__":
    unittest.main()
