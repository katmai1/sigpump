"""Tests del orquestador: descubrimiento, selección de pool, filtros duros,
cooldown y tolerancia a fallos de las fuentes o del envío."""

import logging
import time
import unittest

from sigpump.config import Config
from sigpump.radar import MemecoinRadar


def _pair(address, liquidity=100_000, volume=50_000, change=50, created_at=None, symbol="X"):
    pair = {
        "chainId": "solana",
        "baseToken": {"address": address, "symbol": symbol, "name": symbol},
        "liquidity": {"usd": liquidity},
        "volume": {"h1": volume},
        "priceChange": {"h1": change, "h6": change},
        "marketCap": 1_000_000,
    }
    if created_at is not None:
        pair["pairCreatedAt"] = created_at
    return pair


def _hace_horas(horas: float) -> int:
    """pairCreatedAt en epoch de milisegundos, como lo manda DexScreener."""
    return int((time.time() - horas * 3600) * 1000)


class _FakeClient:
    def __init__(self, latest=None, top=None, profiles=None, pairs=None):
        self._latest = latest if latest is not None else []
        self._top = top if top is not None else []
        self._profiles = profiles if profiles is not None else []
        self._pairs = pairs if pairs is not None else []
        self.requested: list[str] = []

    async def _maybe_raise(self, value):
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_latest_boosted(self):
        return await self._maybe_raise(self._latest)

    async def get_top_boosted(self):
        return await self._maybe_raise(self._top)

    async def get_latest_profiles(self):
        return await self._maybe_raise(self._profiles)

    async def get_pairs_for_tokens(self, chain_id, addresses):
        self.requested = list(addresses)
        return self._pairs


class _FakeAlerter:
    def __init__(self, fail=False):
        self.sent: list[tuple[dict, float]] = []
        self.fail = fail

    async def send(self, pair, score):
        if self.fail:
            raise RuntimeError("Telegram caído")
        self.sent.append((pair, score))


def _config(**kwargs):
    base = dict(min_market_cap_usd=0.0, min_pair_age_minutes=0.0)
    base.update(kwargs)
    return Config(**base)


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


class TestDiscoverCandidates(unittest.IsolatedAsyncioTestCase):
    def _items(self, *addresses, chain="solana"):
        return [{"chainId": chain, "tokenAddress": a} for a in addresses]

    async def test_orden_determinista_con_boosted_primero(self):
        """Antes se construía desde sets: el recorte a top_n descartaba tokens
        distintos en cada pasada, sin criterio."""
        client = _FakeClient(
            latest=self._items("B1", "B2"),
            top=self._items("B3"),
            profiles=self._items("P1", "P2"),
        )
        radar = MemecoinRadar(_config())
        for _ in range(5):
            addresses, boosted = await radar._discover_candidates(client)
            self.assertEqual(addresses, ["B1", "B2", "B3", "P1", "P2"])
            self.assertEqual(boosted, {"B1", "B2", "B3"})

    async def test_recorta_a_top_n_conservando_los_boosted(self):
        client = _FakeClient(latest=self._items("B1", "B2"), profiles=self._items("P1"))
        radar = MemecoinRadar(_config(top_n_candidates=2))
        addresses, _ = await radar._discover_candidates(client)
        self.assertEqual(addresses, ["B1", "B2"])

    async def test_deduplica_conservando_la_primera_aparicion(self):
        client = _FakeClient(latest=self._items("A"), top=self._items("A"), profiles=self._items("A"))
        addresses, _ = await MemecoinRadar(_config())._discover_candidates(client)
        self.assertEqual(addresses, ["A"])

    async def test_ignora_otras_chains_y_entradas_sin_direccion(self):
        client = _FakeClient(
            latest=self._items("B1") + self._items("OTRA", chain="base"),
            profiles=[{"chainId": "solana"}],
        )
        addresses, boosted = await MemecoinRadar(_config())._discover_candidates(client)
        self.assertEqual(addresses, ["B1"])
        self.assertEqual(boosted, {"B1"})

    async def test_una_fuente_caida_no_pierde_las_otras(self):
        """gather() sin return_exceptions hacía que el fallo de una fuente
        tirara abajo la pasada completa."""
        client = _FakeClient(
            latest=RuntimeError("502"),
            top=self._items("B3"),
            profiles=self._items("P1"),
        )
        with self.assertLogs(level=logging.WARNING):
            addresses, boosted = await MemecoinRadar(_config())._discover_candidates(client)
        self.assertEqual(addresses, ["B3", "P1"])
        self.assertEqual(boosted, {"B3"})


class TestCooldown(unittest.TestCase):
    def test_sin_alerta_previa_no_hay_cooldown(self):
        self.assertFalse(MemecoinRadar(_config())._cooldown_active("TOK"))

    def test_alerta_reciente_activa_el_cooldown(self):
        radar = MemecoinRadar(_config(alert_cooldown_minutes=60))
        radar._alerted["TOK"] = time.time()
        self.assertTrue(radar._cooldown_active("TOK"))

    def test_alerta_vieja_no_activa_el_cooldown(self):
        radar = MemecoinRadar(_config(alert_cooldown_minutes=60))
        radar._alerted["TOK"] = time.time() - 61 * 60
        self.assertFalse(radar._cooldown_active("TOK"))

    def test_prune_limpia_solo_lo_expirado(self):
        radar = MemecoinRadar(_config(alert_cooldown_minutes=60))
        radar._alerted = {"VIEJO": time.time() - 7200, "NUEVO": time.time()}
        radar._prune_alerted()
        self.assertEqual(list(radar._alerted), ["NUEVO"])


class TestScanOnce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def _scan(self, config, pairs, alerter=None, addresses=("TOK",)):
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": a} for a in addresses],
            pairs=pairs,
        )
        radar = MemecoinRadar(config)
        alerter = alerter or _FakeAlerter()
        await radar._scan_once(client, alerter)
        return radar, alerter

    async def test_alerta_cuando_supera_el_umbral(self):
        _, alerter = await self._scan(_config(), [_pair("TOK")])
        self.assertEqual(len(alerter.sent), 1)
        pair, score = alerter.sent[0]
        self.assertEqual(pair["baseToken"]["address"], "TOK")
        self.assertGreaterEqual(score, _config().score_alert_threshold)

    async def test_no_alerta_bajo_el_umbral(self):
        _, alerter = await self._scan(
            _config(score_alert_threshold=99.0), [_pair("TOK", change=0)]
        )
        self.assertEqual(alerter.sent, [])

    async def test_filtro_de_liquidez(self):
        _, alerter = await self._scan(
            _config(min_liquidity_usd=50_000), [_pair("TOK", liquidity=1_000)]
        )
        self.assertEqual(alerter.sent, [])

    async def test_filtro_de_volumen(self):
        _, alerter = await self._scan(
            _config(min_volume_h1_usd=10_000), [_pair("TOK", volume=100)]
        )
        self.assertEqual(alerter.sent, [])

    async def test_filtro_de_market_cap_usa_fdv_si_no_hay_market_cap(self):
        pair = _pair("TOK")
        del pair["marketCap"]
        pair["fdv"] = 10_000
        _, alerter = await self._scan(_config(min_market_cap_usd=50_000), [pair])
        self.assertEqual(alerter.sent, [])

    async def test_filtro_de_edad_descarta_pares_nuevos(self):
        _, alerter = await self._scan(
            _config(min_pair_age_minutes=60), [_pair("TOK", created_at=_hace_horas(0.1))]
        )
        self.assertEqual(alerter.sent, [])

    async def test_filtro_de_edad_acepta_pares_maduros(self):
        _, alerter = await self._scan(
            _config(min_pair_age_minutes=60), [_pair("TOK", created_at=_hace_horas(5))]
        )
        self.assertEqual(len(alerter.sent), 1)

    async def test_pair_created_at_como_string_no_rompe_la_pasada(self):
        """Un cast directo tiraba TypeError y mataba la pasada entera, no
        solo este par."""
        _, alerter = await self._scan(
            _config(min_pair_age_minutes=60), [_pair("TOK", created_at=str(_hace_horas(5)))]
        )
        self.assertEqual(len(alerter.sent), 1)

    async def test_pair_created_at_invalido_no_descarta_el_par(self):
        # Sin dato de edad no se puede evaluar el filtro: no se descarta.
        _, alerter = await self._scan(
            _config(min_pair_age_minutes=60), [_pair("TOK", created_at="ayer")]
        )
        self.assertEqual(len(alerter.sent), 1)

    async def test_alerta_con_el_pool_mas_profundo(self):
        pairs = [_pair("TOK", liquidity=6_000), _pair("TOK", liquidity=800_000)]
        _, alerter = await self._scan(_config(), pairs)
        self.assertEqual(len(alerter.sent), 1)
        self.assertEqual(alerter.sent[0][0]["liquidity"]["usd"], 800_000)

    async def test_cooldown_evita_la_segunda_alerta(self):
        radar, alerter = await self._scan(_config(), [_pair("TOK")])
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": "TOK"}], pairs=[_pair("TOK")]
        )
        await radar._scan_once(client, alerter)
        self.assertEqual(len(alerter.sent), 1)

    async def test_fallo_de_envio_no_marca_cooldown_ni_corta_el_resto(self):
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": a} for a in ("A", "B")],
            pairs=[_pair("A"), _pair("B")],
        )
        radar = MemecoinRadar(_config())
        await radar._scan_once(client, _FakeAlerter(fail=True))
        # Ningún envío prosperó: el cooldown queda libre para reintentar.
        self.assertEqual(radar._alerted, {})

    async def test_sin_candidatos_no_pide_datos_de_mercado(self):
        client = _FakeClient()
        alerter = _FakeAlerter()
        await MemecoinRadar(_config())._scan_once(client, alerter)
        self.assertEqual(client.requested, [])
        self.assertEqual(alerter.sent, [])


if __name__ == "__main__":
    unittest.main()
