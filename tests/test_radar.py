"""Tests del orquestador: selección del pool de cada token. Los avisos de
wallets están en test_wallets.py, que reutiliza los helpers de acá."""

import time
import unittest

from sigpump.config import Config
from sigpump.radar import MemecoinRadar


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

    def test_conserva_un_par_por_cada_token(self):
        elegidos = self.radar._best_pair_per_token(
            [_pair("A"), _pair("B"), _pair("A")], ["A", "B"]
        )
        self.assertEqual({p["baseToken"]["address"] for p in elegidos}, {"A", "B"})
