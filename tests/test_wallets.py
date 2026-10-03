"""Tests del seguimiento de wallets: lectura del fichero, detección de
compras por saldos, consulta incremental al RPC y avisos del radar."""

import asyncio
import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.radar import MemecoinRadar
from sigpump.telegram import TelegramAlerter
from sigpump.wallets import (
    LAMPORTS_PER_SOL,
    MAX_TRANSACTION_VERSION,
    MAX_TX_ATTEMPTS,
    WSOL_MINT,
    WalletBuy,
    WalletSignal,
    WalletWatcher,
    load_wallets,
    parse_buys,
)
from tests.test_radar import _FakeClient, _config, _pair

WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
OTRA = "7Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j9"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "TokenMint1111111111111111111111111111111pump"


def _saldo(mint, cantidad, owner=WALLET, decimals=6):
    return {
        "accountIndex": 1,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(int(cantidad * 10**decimals)), "decimals": decimals},
    }


def _tx(pre_tokens=(), post_tokens=(), sol_gastado=0.0, fee=5000, err=None, wallet=WALLET):
    pre_lamports = 10 * LAMPORTS_PER_SOL
    return {
        "blockTime": 1_700_000_000,
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": [pre_lamports],
            "postBalances": [pre_lamports - int(sol_gastado * LAMPORTS_PER_SOL) - fee],
            "preTokenBalances": list(pre_tokens),
            "postTokenBalances": list(post_tokens),
        },
    }


class TestLoadWallets(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "wallets.txt"

    def test_etiquetas_comentarios_y_lineas_invalidas(self):
        self.path.write_text(
            f"# mis wallets\n\n{WALLET}  # ballena 1\n{OTRA}\nesto-no-es-una-wallet\n",
            encoding="utf-8",
        )
        wallets = load_wallets(self.path)
        self.assertEqual(list(wallets), [WALLET, OTRA])
        self.assertEqual(wallets[WALLET], "ballena 1")
        # Sin etiqueta, la dirección abreviada.
        self.assertEqual(wallets[OTRA], "7Q54…e4j9")


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


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRpc:
    """Responde getSignaturesForAddress con `firmas` (de la más nueva a la más
    vieja, cortadas en `until`) y getTransaction con `txs`."""

    def __init__(self, firmas=(), txs=None):
        self.firmas = list(firmas)
        self.txs = txs or {}
        self.llamadas: list[tuple[str, list]] = []

    def post(self, url, json=None, timeout=None):
        method, params = json["method"], json["params"]
        self.llamadas.append((method, params))
        if method == "getSignaturesForAddress":
            options = params[1]
            firmas = self.firmas
            if "until" in options:
                firmas = firmas[: firmas.index(options["until"])]
            result = [{"signature": f, "err": None} for f in firmas[: options["limit"]]]
        else:
            result = self.txs.get(params[0])
        return _FakeResponse({"jsonrpc": "2.0", "result": result})


class TestWalletWatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "wallets.txt"
        self.path.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")

    async def test_la_primera_vuelta_no_avisa_del_historial(self):
        rpc = _FakeRpc(firmas=["VIEJA"], txs={"VIEJA": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)})
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
        self.assertEqual(await watcher.poll(), [])
        self.assertNotIn("getTransaction", [m for m, _ in rpc.llamadas])

    async def test_procesa_solo_las_firmas_nuevas_en_orden(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
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
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        # getTransaction devuelve null: el nodo todavía no la tiene.
        self.assertEqual(await watcher.poll(), [])
        rpc.txs = {"NUEVA": _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)}
        self.assertEqual(len(await watcher.poll()), 1)


class TestTransaccionesQueFallan(unittest.IsolatedAsyncioTestCase):
    setUp = TestWalletWatcher.setUp

    async def test_se_salta_tras_varios_intentos_y_sigue_con_las_demas(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
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
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        await watcher.poll()
        [params] = [p for m, p in rpc.llamadas if m == "getTransaction"]
        self.assertEqual(params[1]["maxSupportedTransactionVersion"], MAX_TRANSACTION_VERSION)


def _buy(wallet=WALLET, label="ballena", mint="TOK", ts=None, new=True):
    return WalletBuy(
        wallet=wallet, label=label, mint=mint, sol_spent=1.5, stable_spent=0.0,
        new_position=new, signature="SIG", ts=time.time() if ts is None else ts,
    )


class _FakeWatcher:
    def __init__(self, *rondas):
        self.rondas = list(rondas)

    async def poll(self):
        return self.rondas.pop(0) if self.rondas else []


class _FakeAutoridades:
    def __init__(self, unsafe=None):
        self.unsafe = unsafe or {}

    async def unsafe_reasons(self, addresses):
        return {a: r for a, r in self.unsafe.items() if a in addresses}


class _FakeAlerter:
    def __init__(self):
        self.wallet: list[tuple[dict, WalletSignal]] = []

    async def send(self, pair, score, candles=None, early=None, wallet=None):
        await asyncio.sleep(0)
        self.wallet.append((pair, wallet))


class TestAvisosDeWallets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "alertas.db"

    def _radar(self, *rondas, unsafe=None):
        radar = MemecoinRadar(_config(alert_log_path=str(self.path)))
        self.addCleanup(radar._tracker.close)
        radar._wallet_watcher = _FakeWatcher(*rondas)
        radar._authorities = _FakeAutoridades(unsafe)
        return radar

    def _rows(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM alertas")]
        finally:
            conn.close()

    async def test_avisa_y_registra_la_compra(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar._wallets_once(_FakeClient(pairs=[{**_pair("TOK"), "dexId": "pumpswap"}]), alerter)
        [(pair, signal)] = alerter.wallet
        self.assertEqual(signal.buy.label, "ballena")
        fila = self._rows()[0]
        self.assertEqual((fila["tipo"], fila["enviada"]), ("wallet", 1))
        self.assertEqual(fila["wallet"], WALLET)
        self.assertEqual(fila["sol_gastado"], 1.5)
        self.assertEqual(fila["entrada_nueva"], 1)

    async def test_ignora_tokens_en_bonding_curve(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar._wallets_once(_FakeClient(pairs=[{**_pair("TOK"), "dexId": "pumpfun"}]), alerter)
        self.assertEqual(alerter.wallet, [])
        self.assertEqual(radar._tracker.last_sent("wallet", 0), {})

    async def test_sin_pool_en_dexscreener_se_ignora(self):
        radar = self._radar([_buy()])
        alerter = _FakeAlerter()
        await radar._wallets_once(_FakeClient(pairs=[]), alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_descarta_tokens_minteables_o_congelables(self):
        radar = self._radar([_buy()], unsafe={"TOK": "se pueden congelar"})
        alerter = _FakeAlerter()
        await radar._wallets_once(_FakeClient(pairs=[_pair("TOK")]), alerter)
        self.assertEqual(alerter.wallet, [])
        fila = self._rows()[0]
        self.assertEqual(fila["enviada"], 0)
        self.assertIn("congelar", fila["motivo_descarte"])

    async def test_no_repite_la_misma_wallet_en_el_mismo_token(self):
        radar = self._radar([_buy(), _buy()], [_buy()])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        self.assertEqual(len(alerter.wallet), 1)

    async def test_confluencia_de_otra_wallet(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA, label="ballena 2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        self.assertEqual([s.others for _, s in alerter.wallet], [(), ("ballena",)])
        self.assertEqual(self._rows()[1]["wallets_confluencia"], 1)

    async def test_la_confluencia_caduca(self):
        viejo = time.time() - 2 * 3600
        radar = self._radar([_buy(ts=viejo)], [_buy(wallet=OTRA, label="ballena 2")])
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        self.assertEqual(alerter.wallet[-1][1].others, ())


class TestMensajeDeWallet(unittest.TestCase):
    def test_encabezado_con_compra_y_confluencia(self):
        alerter = TelegramAlerter("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", "-100123")
        signal = WalletSignal(_buy(label="<ballena>", new=False), others=("otra",))
        text = alerter.format_message(_pair("TOK", symbol="PEPE"), 50.0, wallet=signal)
        self.assertIn("&lt;ballena&gt; compró PEPE", text)
        self.assertIn("Amplía posición", text)
        self.assertIn("1.50 SOL", text)
        self.assertIn("https://solscan.io/tx/SIG", text)
        self.assertIn("También entraron: <b>otra</b>", text)
