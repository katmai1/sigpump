"""Tests del seguimiento de wallets: lectura del fichero, detección de
compras por saldos, consulta incremental al RPC y avisos del radar."""

import asyncio
import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from sigpump.radar import MemecoinRadar
from sigpump.telegram import TelegramAlerter
from sigpump.wallets import (
    LAMPORTS_PER_SOL,
    MAX_TRANSACTION_VERSION,
    MAX_TX_ATTEMPTS,
    WSOL_MINT,
    FollowedWallet,
    WalletBuy,
    WalletSignal,
    WalletWatcher,
    add_to_blacklist,
    load_blacklist,
    load_temporary_blacklist,
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
        self.assertEqual(wallets[WALLET], FollowedWallet("ballena 1"))
        # Sin etiqueta, la dirección abreviada.
        self.assertEqual(wallets[OTRA].label, "7Q54…e4j9")

    def test_duplicadas_se_eliminan_del_fichero(self):
        self.path.write_text(
            f"# mis wallets\n{WALLET}  # ballena 1\n{OTRA}\n{WALLET} # repetida\n{OTRA}\n",
            encoding="utf-8",
        )
        wallets = load_wallets(self.path)
        self.assertEqual(list(wallets), [WALLET, OTRA])
        # Se queda la primera aparición, con su etiqueta.
        self.assertEqual(wallets[WALLET].label, "ballena 1")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            f"# mis wallets\n{WALLET}  # ballena 1\n{OTRA}\n",
        )

    def test_sin_duplicadas_no_toca_el_fichero(self):
        self.path.write_text(f"{WALLET}\n{OTRA}\n", encoding="utf-8")
        mtime = self.path.stat().st_mtime_ns
        load_wallets(self.path)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_asterisco_marca_wallet_de_confianza(self):
        self.path.write_text(f"{WALLET} *  # ballena\n{OTRA}*\n", encoding="utf-8")
        wallets = load_wallets(self.path)
        self.assertEqual(wallets[WALLET], FollowedWallet("ballena", trusted=True))
        self.assertTrue(wallets[OTRA].trusted)

    def test_las_de_la_lista_negra_se_eliminan_del_fichero(self):
        self.path.write_text(f"{WALLET}  # ballena\n{OTRA}  # rug\n", encoding="utf-8")
        negra = self.path.with_name("negra.txt")
        add_to_blacklist(negra, OTRA, "rug NGU -96%")
        wallets = load_wallets(self.path, load_blacklist(negra))
        self.assertEqual(list(wallets), [WALLET])
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n")

    def test_lista_negra_sin_fichero_esta_vacia(self):
        self.assertEqual(load_blacklist(self.path.with_name("no-existe.txt")), set())

    def test_las_temporales_no_se_eliminan_del_fichero(self):
        self.path.write_text(f"{WALLET}  # ballena\n{OTRA}  # bot\n", encoding="utf-8")
        negra = self.path.with_name("negra.txt")
        hasta = time.time() + 3600
        add_to_blacklist(negra, OTRA, "bot: 12 tokens en 1h", until=hasta)
        self.assertEqual(load_blacklist(negra), set())
        self.assertAlmostEqual(load_temporary_blacklist(negra)[OTRA], hasta, delta=1)
        self.assertEqual(list(load_wallets(self.path, load_blacklist(negra))), [WALLET, OTRA])

    def test_las_temporales_vencidas_se_ignoran_y_se_limpian(self):
        negra = self.path.with_name("negra.txt")
        add_to_blacklist(negra, OTRA, "bot", until=time.time() - 1)
        self.assertEqual(load_temporary_blacklist(negra), {})
        add_to_blacklist(negra, WALLET, "rug")
        self.assertEqual(negra.read_text(encoding="utf-8"), f"{WALLET}  # rug\n")

    def test_la_permanente_gana_a_la_temporal(self):
        negra = self.path.with_name("negra.txt")
        add_to_blacklist(negra, OTRA, "bot", until=time.time() + 3600)
        add_to_blacklist(negra, OTRA, "rug")
        self.assertEqual(load_blacklist(negra), {OTRA})
        self.assertEqual(load_temporary_blacklist(negra), {})


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
    async def test_blacklist_deja_de_seguir_y_quita_del_fichero(self):
        negra = self.path.with_name("negra.txt")
        watcher = WalletWatcher(_FakeRpc(firmas=[]), "http://rpc", self.path, 0.1, negra)
        watcher.reload()
        watcher.blacklist(WALLET, "ballena: rug NGU -96%")
        self.assertEqual(watcher.wallets, {})
        watcher.reload()
        self.assertEqual(watcher.wallets, {})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertIn(WALLET, load_blacklist(negra))

    async def test_blacklist_temporal_deja_de_consultarla_hasta_que_acaba(self):
        negra = self.path.with_name("negra.txt")
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = WalletWatcher(rpc, "http://rpc", self.path, 0.1, negra)
        await watcher.poll()
        watcher.blacklist(WALLET, "ballena: 12 tokens en 1h", seconds=3600)
        rpc.llamadas.clear()
        await watcher.poll()
        self.assertEqual(rpc.llamadas, [])
        self.assertIn(WALLET, watcher.wallets)
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n")
        # Al acabar vuelve a consultarse, desde su última firma.
        watcher._banned_until[WALLET] = time.time() - 1
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(rpc.llamadas[0][1][1]["limit"], 1)

    async def test_la_confianza_llega_a_la_compra(self):
        self.path.write_text(f"{WALLET} *  # ballena\n", encoding="utf-8")
        rpc = _FakeRpc(firmas=["VIEJA"], txs={})
        watcher = WalletWatcher(rpc, "http://rpc", self.path, min_sol=0.1)
        await watcher.poll()
        rpc.firmas = ["NUEVA", "VIEJA"]
        rpc.txs["NUEVA"] = _tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=1)
        [buy] = await watcher.poll()
        self.assertTrue(buy.trusted)

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


def _buy(wallet=WALLET, label="ballena", mint="TOK", ts=None, new=True, trusted=False):
    return WalletBuy(
        wallet=wallet, label=label, mint=mint, sol_spent=1.5, stable_spent=0.0,
        new_position=new, signature="SIG", ts=time.time() if ts is None else ts,
        trusted=trusted,
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

    def _radar(self, *rondas, unsafe=None, **kwargs):
        radar = MemecoinRadar(_config(alert_log_path=str(self.path), **kwargs))
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

    async def test_min_wallets_espera_a_la_segunda_wallet(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA, label="ballena 2")], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        self.assertEqual(alerter.wallet, [])
        await radar._wallets_once(client, alerter)
        [(_, signal)] = alerter.wallet
        self.assertEqual((signal.buy.label, signal.others), ("ballena 2", ("ballena",)))
        filas = self._rows()
        self.assertEqual([(f["enviada"], f["motivo_descarte"]) for f in filas],
                         [(0, "1 de 2 wallets"), (1, None)])

    async def test_min_wallets_no_cuenta_dos_veces_la_misma_wallet(self):
        radar = self._radar([_buy()], [_buy()], wallets_min_wallets=2, wallets_cooldown_minutes=0)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_dos_wallets_con_la_misma_etiqueta_cuentan_como_dos(self):
        radar = self._radar([_buy()], [_buy(wallet=OTRA)], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        self.assertEqual(len(alerter.wallet), 1)

    async def test_la_confluencia_sobrevive_a_un_reinicio(self):
        radar = self._radar([_buy()], wallets_min_wallets=2)
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(wallet=OTRA, label="ballena 2")], wallets_min_wallets=2)
        reiniciado._restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado._wallets_once(client, alerter)
        [(_, signal)] = alerter.wallet
        self.assertEqual(signal.others, ("ballena",))

    async def test_wallet_de_confianza_avisa_sola(self):
        radar = self._radar([_buy(trusted=True)], wallets_min_wallets=2)
        alerter = _FakeAlerter()
        await radar._wallets_once(_FakeClient(pairs=[_pair("TOK")]), alerter)
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
            await radar._wallets_once(client, alerter)
        # 1 wallet: aviso; 2: ya avisado; 3: dos más que en el aviso, actualización.
        self.assertEqual([(1 + len(s.others), s.update) for _, s in alerter.wallet], [(1, False), (3, True)])
        self.assertEqual(
            [f["motivo_descarte"] for f in self._rows()],
            [None, "token ya avisado con 1 wallets", None, "token ya avisado con 3 wallets"],
        )

    async def test_el_aviso_por_token_sobrevive_a_un_reinicio(self):
        radar = self._radar([_buy()], wallets_token_cooldown_minutes=120)
        client = _FakeClient(pairs=[_pair("TOK")])
        await radar._wallets_once(client, _FakeAlerter())
        reiniciado = self._radar([_buy(wallet=OTRA)], wallets_token_cooldown_minutes=120)
        reiniciado._restore_cooldowns()
        alerter = _FakeAlerter()
        await reiniciado._wallets_once(client, alerter)
        self.assertEqual(alerter.wallet, [])

    async def test_wallet_hiperactiva_no_avisa_ni_cuenta_para_la_confluencia(self):
        compras = [_buy(mint=f"T{i}") for i in range(3)] + [_buy(mint="TOK")]
        radar = self._radar(compras, [_buy(wallet=OTRA)], wallets_max_tokens_per_hour=3)
        alerter = _FakeAlerter()
        pairs = [_pair(m) for m in ("T0", "T1", "T2", "TOK")]
        client = _FakeClient(pairs=pairs)
        await radar._wallets_once(client, alerter)
        await radar._wallets_once(client, alerter)
        motivos = [f["motivo_descarte"] for f in self._rows()]
        self.assertEqual(motivos[:4], ["wallet hiperactiva (4 tokens en 1h)"] * 4)
        [(_, signal)] = alerter.wallet
        self.assertEqual((signal.buy.wallet, signal.others), (OTRA, ()))

    async def test_wallet_hiperactiva_va_a_la_lista_negra_una_hora(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", tmp / "negra.txt"
        wallets_txt.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")
        compras = [_buy(mint=f"T{i}") for i in range(4)]
        radar = self._radar(wallets_blacklist_file=str(negra), wallets_max_tokens_per_hour=3)
        watcher = WalletWatcher(None, "http://rpc", wallets_txt, 0.1, negra)
        watcher.reload()
        watcher.poll = AsyncMock(return_value=compras)
        radar._wallet_watcher = watcher
        await radar._wallets_once(_FakeClient(pairs=[_pair(f"T{i}") for i in range(4)]), _FakeAlerter())
        self.assertTrue(watcher.banned(WALLET))
        self.assertAlmostEqual(load_temporary_blacklist(negra)[WALLET], time.time() + 3600, delta=5)
        self.assertEqual(negra.read_text(encoding="utf-8").count(WALLET), 1)
        self.assertIn("ballena: 4 tokens en 1h", negra.read_text(encoding="utf-8"))
        watcher.reload()
        self.assertEqual(list(watcher.wallets), [WALLET])

    async def test_wallet_que_compra_un_rug_va_a_la_lista_negra(self):
        tmp = self.path.parent
        wallets_txt, negra = tmp / "wallets.txt", tmp / "negra.txt"
        wallets_txt.write_text(f"{WALLET}  # ballena\n{OTRA}  # buena\n", encoding="utf-8")
        radar = self._radar([_buy()], wallets_blacklist_file=str(negra), wallets_rug_drop_pct=90)
        await radar._wallets_once(_FakeClient(pairs=[_pair("TOK", symbol="NGU")]), _FakeAlerter())
        radar._tracker.update_row(self._rows()[0]["id"], {"peor_ret_30m_pct": -96.0})
        radar._wallet_watcher = WalletWatcher(None, "http://rpc", wallets_txt, 0.1, negra)
        radar._wallet_watcher.reload()
        radar._blacklist_ruggers()
        self.assertEqual(list(radar._wallet_watcher.wallets), [OTRA])
        self.assertIn("rug NGU -96%", negra.read_text(encoding="utf-8"))
        radar._wallet_watcher.reload()
        self.assertEqual(wallets_txt.read_text(encoding="utf-8"), f"{OTRA}  # buena\n")

    async def test_solo_wallets_muestrea_el_precio_de_las_senales(self):
        """Sin alertas ni prealertas no corre otro bucle que actualice el registro."""
        radar = self._radar(alerts_enabled=False, watch_enabled=False, wallets_enabled=True)
        radar._tracker.update = AsyncMock()
        await radar._wallets_once(_FakeClient(pairs=[]), _FakeAlerter())
        radar._tracker.update.assert_awaited_once()

    async def test_con_alertas_el_precio_lo_muestrea_la_pasada(self):
        radar = self._radar()
        radar._tracker.update = AsyncMock()
        await radar._wallets_once(_FakeClient(pairs=[]), _FakeAlerter())
        radar._tracker.update.assert_not_awaited()


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
        self.assertNotIn("ACTUALIZACIÓN", text)

    def test_encabezado_de_actualizacion_y_confianza(self):
        alerter = TelegramAlerter("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", "-100123")
        signal = WalletSignal(_buy(trusted=True), others=("a", "b"), update=True)
        text = alerter.format_message(_pair("TOK", symbol="PEPE"), 50.0, wallet=signal)
        self.assertTrue(text.startswith("🔁 <b>ACTUALIZACIÓN</b>"))
        self.assertIn("⭐ ballena compró", text)
