"""Tests del orquestador: selección del pool de cada token. Los avisos de
wallets están en test_wallets.py, que reutiliza los helpers de acá."""

import time
import unittest

from sigpump.config import Config
from sigpump.radar import MemecoinRadar
from sigpump.wallets import WalletBuy, WalletSignal


def _pair(address, liquidity=100_000, volume=50_000, change=50, created_at=None, symbol="X"):
    pair = {
        "chainId": "solana",
        "baseToken": {"address": address, "symbol": symbol, "name": symbol},
        "liquidity": {"usd": liquidity},
        # Volumen de 5 min al triple de ritmo y 75% de compras: acelerando.
        "volume": {"h1": volume, "m5": volume / 4},
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "priceChange": {"m5": 2, "h1": change, "h6": change},
        "marketCap": 1_000_000,
    }
    if created_at is not None:
        pair["pairCreatedAt"] = created_at
    return pair


def _hace_horas(horas: float) -> int:
    """pairCreatedAt en epoch de milisegundos, como lo manda DexScreener."""
    return int((time.time() - horas * 3600) * 1000)


class _FakeClient:
    def __init__(self, pairs=None):
        self._pairs = pairs if pairs is not None else []
        self.requested: list[str] = []

    async def get_pairs_for_tokens(self, chain_id, addresses):
        self.requested = list(addresses)
        return self._pairs


def _config(**kwargs):
    # Sin registro de alertas para no crear la base al correr los tests.
    base = dict(alert_log_path="")
    base.update(kwargs)
    return Config(**base)


def _con_quote(pair, symbol="SOL", address="So11111111111111111111111111111111111111112"):
    pair["quoteToken"] = {"symbol": symbol, "address": address}
    return pair


class TestBestPairPerToken(unittest.TestCase):
    def setUp(self):
        self.radar = MemecoinRadar(_config())

    def test_elige_el_pool_de_mayor_liquidez(self):
        """Un token cotiza en varios pools; antes se alertaba con los datos del
        primero que devolvía la API, no del más profundo."""
        chico = _pair("TOK", liquidity=5_000)
        grande = _pair("TOK", liquidity=900_000)
        for orden in ([chico, grande], [grande, chico]):
            with self.subTest(orden=[p["liquidity"]["usd"] for p in orden]):
                elegidos = self.radar._best_pair_per_token(orden, ["TOK"])
                self.assertEqual(len(elegidos), 1)
                self.assertEqual(elegidos[0]["liquidity"]["usd"], 900_000)

    def test_prefiere_el_pool_en_los_dex_y_quote_pedidos(self):
        """Un token con su pool más profundo en otro dex o contra USDC se
        evalúa con su pool contra SOL en pumpswap, aunque sea más chico."""
        radar = MemecoinRadar(_config(dex_ids=["pumpswap", "raydium"], quote_tokens=["SOL"]))
        bueno = {**_con_quote(_pair("TOK", liquidity=5_000)), "dexId": "pumpswap"}
        otro_dex = {**_con_quote(_pair("TOK", liquidity=900_000)), "dexId": "meteora"}
        otro_quote = {**_con_quote(_pair("TOK", liquidity=900_000), symbol="USDC", address="EPjF"),
                      "dexId": "raydium"}
        for orden in ([bueno, otro_dex, otro_quote], [otro_quote, otro_dex, bueno]):
            with self.subTest(orden=[p["dexId"] for p in orden]):
                [elegido] = radar._best_pair_per_token(orden, ["TOK"])
                self.assertIs(elegido, bueno)

    def test_sin_pool_valido_queda_el_de_mayor_liquidez(self):
        radar = MemecoinRadar(_config(dex_ids=["pumpswap"]))
        chico = {**_pair("TOK", liquidity=5_000), "dexId": "orca"}
        grande = {**_pair("TOK", liquidity=900_000), "dexId": "meteora"}
        [elegido] = radar._best_pair_per_token([chico, grande], ["TOK"])
        self.assertIs(elegido, grande)

    def test_descarta_pares_donde_el_token_pedido_es_el_quote(self):
        """En un par SOL/TOKEN el baseToken es SOL: alertar sobre él sería
        alertar sobre el token equivocado."""
        elegidos = self.radar._best_pair_per_token([_pair("SOL_WRAPPED")], ["TOK"])
        self.assertEqual(elegidos, [])

    def test_descarta_pares_sin_direccion(self):
        # "" como clave de cooldown hacía que dos tokens distintos la compartieran.
        sin_direccion = {"baseToken": {}, "liquidity": {"usd": 1}}
        self.assertEqual(self.radar._best_pair_per_token([sin_direccion], ["TOK"]), [])

    def test_direccion_null_no_se_vuelve_el_texto_none(self):
        # Con .get("address", "") un null quedaba como None y la clave era "None".
        self.assertEqual(self.radar._best_pair_per_token([{"baseToken": {"address": None}}], ["None"]), [])

    def test_conserva_un_par_por_cada_token(self):
        elegidos = self.radar._best_pair_per_token(
            [_pair("A"), _pair("B"), _pair("A")], ["A", "B"]
        )
        self.assertEqual({p["baseToken"]["address"] for p in elegidos}, {"A", "B"})


class _Autoridades:
    def __init__(self):
        self.pedidas: list[str] = []

    async def unsafe_reasons(self, addresses):
        self.pedidas += addresses
        return {}


class TestUnsafeReasons(unittest.IsolatedAsyncioTestCase):
    async def test_par_con_direccion_null_se_pide_vacia(self):
        radar = MemecoinRadar(_config())
        radar._authorities = _Autoridades()
        await radar._unsafe_reasons([{"baseToken": {"address": None}}, _pair("TOK")])
        self.assertEqual(radar._authorities.pedidas, ["", "TOK"])


def _signal(wallet="W1", mint="TOK", trusted=False, others=(), curve=False):
    buy = WalletBuy(
        wallet=wallet, label=wallet, mint=mint, sol_spent=1.0, stable_spent=0.0,
        new_position=True, signature="SIG", ts=time.time(), trusted=trusted,
    )
    return WalletSignal(buy, tuple(others), bonding_curve=curve)


class TestDecide(unittest.TestCase):
    """La decisión de avisar o no, sin el bucle, la red ni Telegram."""

    def test_avisa_si_nada_lo_impide(self):
        decision = MemecoinRadar(_config())._decide(_signal(), _pair("TOK"))
        self.assertEqual((decision.reason, decision.pair_ok, decision.update), (None, True, False))

    def test_autoridades_y_filtros_del_par_no_pasan_el_par(self):
        radar = MemecoinRadar(_config(wallets_max_price_change_h1_pct=10))
        por_autoridades = radar._decide(_signal(), _pair("TOK", change=5), "se pueden congelar")
        por_par = radar._decide(_signal(), _pair("TOK", change=50))
        self.assertEqual((por_autoridades.reason, por_autoridades.pair_ok), ("se pueden congelar", False))
        self.assertEqual((por_par.reason, por_par.pair_ok), ("cambio 1h +50% > 10%", False))

    def test_min_wallets_salvo_las_de_confianza(self):
        radar = MemecoinRadar(_config(wallets_min_wallets=2))
        decision = radar._decide(_signal(), _pair("TOK"))
        self.assertEqual((decision.reason, decision.pair_ok), ("1 de 2 wallets", True))
        self.assertIsNone(radar._decide(_signal(trusted=True), _pair("TOK")).reason)
        self.assertIsNone(radar._decide(_signal(others=("W2",)), _pair("TOK")).reason)

    def test_hiperactiva(self):
        radar = MemecoinRadar(_config(wallets_max_tokens_per_hour=1))
        for mint in ("A", "B"):
            radar._cooldowns.add_activity(_signal(mint=mint).buy)
        self.assertEqual(radar._decide(_signal(), _pair("TOK")).reason, "wallet hiperactiva (2 tokens en 1h)")

    def test_token_ya_avisado_y_reaviso(self):
        radar = MemecoinRadar(_config(wallets_token_cooldown_minutes=60, wallets_realert_new_wallets=2))
        radar._cooldowns.mark_token_alerted("TOK", 1)
        self.assertEqual(radar._decide(_signal(others=("W2",)), _pair("TOK")).reason, "token ya avisado con 1 wallets")
        reaviso = radar._decide(_signal(others=("W2", "W3")), _pair("TOK"))
        self.assertEqual((reaviso.reason, reaviso.update), (None, True))

    def test_bonding_curve_se_salta_los_filtros_de_amm(self):
        radar = MemecoinRadar(_config(dex_ids=["pumpswap"], wallets_min_market_cap_usd=5_000_000))
        self.assertIsNotNone(radar._decide(_signal(), _pair("TOK")).reason)
        self.assertIsNone(radar._decide(_signal(curve=True), _pair("TOK")).reason)
