"""Tests del orquestador: descubrimiento, selección de pool, filtros duros,
cooldown y tolerancia a fallos de las fuentes o del envío."""

import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.config import Config
from sigpump.radar import (
    CANDLE_LOOKBACK_MINUTES,
    MAX_VERIFICATIONS_PER_PASS,
    MemecoinRadar,
    _max_candle_drop_pct,
)


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
    def __init__(
        self, latest=None, top=None, profiles=None, pairs=None,
        takeovers=None, ads=None, trending=None, gecko_pools=None, candles=None,
    ):
        self._latest = latest if latest is not None else []
        self._top = top if top is not None else []
        self._profiles = profiles if profiles is not None else []
        self._takeovers = takeovers if takeovers is not None else []
        self._ads = ads if ads is not None else []
        self._trending = trending if trending is not None else []
        self._pairs = pairs if pairs is not None else []
        self._gecko_pools = gecko_pools if gecko_pools is not None else {}
        # pool address -> velas; los pools ausentes devuelven [].
        self._candles = candles if candles is not None else {}
        self.requested: list[str] = []
        self.trending_args: tuple | None = None
        self.gecko_pools_requested: list[str] | None = None
        self.candles_requested: list[tuple] = []

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

    async def get_latest_takeovers(self):
        return await self._maybe_raise(self._takeovers)

    async def get_latest_ads(self):
        return await self._maybe_raise(self._ads)

    async def get_trending_tokens(self, chain_id, pages):
        self.trending_args = (chain_id, pages)
        return await self._maybe_raise(self._trending)

    async def get_pairs_for_tokens(self, chain_id, addresses):
        self.requested = list(addresses)
        return self._pairs

    async def get_gecko_pools(self, chain_id, pool_addresses):
        self.gecko_pools_requested = list(pool_addresses)
        return self._gecko_pools

    async def get_pool_candles(self, chain_id, pool_address, token_address, limit):
        self.candles_requested.append((pool_address, token_address, limit))
        return self._candles.get(pool_address, [])


class _FakeAlerter:
    def __init__(self, fail=False):
        self.sent: list[tuple[dict, float]] = []
        self.candles: list = []
        self.fail = fail

    async def send(self, pair, score, candles=None):
        if self.fail:
            raise RuntimeError("Telegram caído")
        self.sent.append((pair, score))
        self.candles.append(candles)


def _config(**kwargs):
    # Filtros que dependen de txns o de GeckoTerminal apagados: cada test
    # prende solo los que ejercita. Sin registro de alertas para no crear
    # la base al correr los tests.
    base = dict(
        min_market_cap_usd=0.0,
        min_pair_age_minutes=0.0,
        min_txns_h1=0,
        min_sell_ratio_h1=0.0,
        verify_before_alert=False,
        alert_log_path="",
    )
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

    async def test_suma_trending_takeovers_y_ads_en_orden(self):
        """Con solo boosts y perfiles quedaban ~36 direcciones por pasada y
        casi siempre las mismas."""
        client = _FakeClient(
            latest=self._items("B1"),
            profiles=self._items("P1"),
            takeovers=self._items("C1", "B1"),
            ads=self._items("AD1"),
            trending=["T1", "P1", "T2"],
        )
        radar = MemecoinRadar(_config(geckoterminal_pages=3))
        addresses, boosted = await radar._discover_candidates(client)
        self.assertEqual(addresses, ["B1", "T1", "P1", "T2", "C1", "AD1"])
        self.assertEqual(boosted, {"B1"})
        self.assertEqual(client.trending_args, ("solana", 3))

    async def test_trending_caido_no_pierde_las_otras(self):
        client = _FakeClient(latest=self._items("B1"), trending=RuntimeError("429"))
        with self.assertLogs(level=logging.WARNING):
            addresses, _ = await MemecoinRadar(_config())._discover_candidates(client)
        self.assertEqual(addresses, ["B1"])

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

    async def test_direcciones_evm_coinciden_sin_importar_mayusculas(self):
        """GeckoTerminal manda las direcciones EVM en minúsculas y DexScreener
        con checksum: la comparación exacta descartaba todos los trending."""
        pair = _pair("0xAbCdEf", change=0)
        pair["chainId"] = "ethereum"
        client = _FakeClient(
            latest=[{"chainId": "ethereum", "tokenAddress": "0xABCDEF"}],
            trending=["0xabcdef"],
            pairs=[pair],
        )
        radar = MemecoinRadar(_config(chain_id="ethereum", score_alert_threshold=70.0))
        alerter = _FakeAlerter()
        await radar._scan_once(client, alerter)
        # Un solo candidato (deduplicado) y el par checksummed no se descarta.
        self.assertEqual(client.requested, ["0xabcdef"])
        self.assertEqual(len(alerter.sent), 1)
        # 20 volumen + 25 aceleración + 10 compras + 10 liquidez + 10 boost:
        # sin reconocer el boost serían 65.
        self.assertEqual(alerter.sent[0][1], 75.0)
        self.assertIn("0xabcdef", radar._alerted)

    async def test_direcciones_solana_no_se_pasan_a_minusculas(self):
        client = _FakeClient(latest=[{"chainId": "solana", "tokenAddress": "AbC"}], pairs=[])
        await MemecoinRadar(_config())._scan_once(client, _FakeAlerter())
        self.assertEqual(client.requested, ["AbC"])

    async def test_sin_candidatos_no_pide_datos_de_mercado(self):
        client = _FakeClient()
        alerter = _FakeAlerter()
        await MemecoinRadar(_config())._scan_once(client, alerter)
        self.assertEqual(client.requested, [])
        self.assertEqual(alerter.sent, [])


def _con_txns(pair, buys=80, sells=40):
    pair["txns"] = {"h1": {"buys": buys, "sells": sells}}
    return pair


def _jupcat():
    """Par real de JUPCAT (AaEhFTX4...) tal como lo informó DexScreener: el
    precio en USD salió ~5000x inflado por un precio de JUP roto, y con él
    volumen, liquidez, market cap y cambio 1h. Salió como candidato con score
    alto y el gráfico mostraba subidas y caídas enormes en segundos."""
    return {
        "chainId": "solana",
        "pairAddress": "BdFK8v9bVSe9pSXSdGZfL4pzjRpeSyfrkePGiM1cZEdt",
        "baseToken": {
            "address": "AaEhFTX4naHSWSXz9TVe5QgLbtSLT8ZqYJGZzDDcoroh",
            "symbol": "JUPCAT",
            "name": "Jupiter Cat",
        },
        "priceUsd": "1.47",
        "priceNative": "0.001233",
        "marketCap": 1473692536,
        "fdv": 1473692536,
        "pairCreatedAt": _hace_horas(200),
        "volume": {"h24": 11960162.37, "h6": 8376014.42, "h1": 8336483.73, "m5": 2959832.6},
        "priceChange": {"m5": 3.33, "h1": 440684, "h6": 566606, "h24": 443323},
        "txns": {"h1": {"buys": 76, "sells": 19}, "h6": {"buys": 306, "sells": 180}},
        "liquidity": {"usd": 261918162.45, "base": 88864588, "quote": 109593},
    }


# Lo que GeckoTerminal informaba del mismo pool en ese momento.
_JUPCAT_GECKO = {
    "BdFK8v9bVSe9pSXSdGZfL4pzjRpeSyfrkePGiM1cZEdt": {
        "reserve_usd": 53158.68,
        "token_prices": {
            "AaEhFTX4naHSWSXz9TVe5QgLbtSLT8ZqYJGZzDDcoroh": 0.000296,
            "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 0.242,
        },
    },
}


def _vela(t, open_, high, low, close):
    return (float(t), float(open_), float(high), float(low), float(close))


_VELAS_TRANQUILAS = [_vela(i * 60, 100, 104, 97, 101) for i in range(60)]


class TestMaxCandleDrop(unittest.TestCase):
    def test_vela_alcista_da_cero(self):
        self.assertEqual(_max_candle_drop_pct([_vela(0, 100, 150, 100, 150)]), 0.0)

    def test_mecha_de_dump_cuenta_aunque_se_recupere(self):
        # Dumpeo al 50% y recompra en la misma vela: cierre casi igual a la apertura.
        self.assertEqual(_max_candle_drop_pct([_vela(0, 100, 100, 50, 98)]), 50.0)

    def test_subida_vertical_que_se_desploma_en_la_vela(self):
        self.assertAlmostEqual(_max_candle_drop_pct([_vela(0, 100, 300, 100, 120)]), 60.0)

    def test_usa_el_cierre_anterior_si_abrio_mas_arriba(self):
        velas = [_vela(0, 90, 200, 90, 200), _vela(60, 120, 120, 100, 110)]
        self.assertEqual(_max_candle_drop_pct(velas), 50.0)

    def test_sin_velas_da_cero(self):
        self.assertEqual(_max_candle_drop_pct([]), 0.0)


class TestFiltrosAntiManipulacion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def _scan(self, config, pairs, **client_kwargs):
        addresses = [p["baseToken"]["address"] for p in pairs]
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": a} for a in addresses],
            pairs=pairs,
            **client_kwargs,
        )
        radar = MemecoinRadar(config)
        alerter = _FakeAlerter()
        await radar._scan_once(client, alerter)
        return radar, alerter, client

    async def test_jupcat_no_pasa_los_filtros_por_defecto(self):
        """Regresión: con la config por defecto JUPCAT llegaba a alertar."""
        # Defaults salvo el registro: si no, el test crea alertas.db en el repo.
        config = Config(alert_log_path="")
        radar = MemecoinRadar(config)
        self.assertIsNotNone(radar._rejection_reason(_jupcat()))
        _, alerter, client = await self._scan(config, [_jupcat()])
        self.assertEqual(alerter.sent, [])
        # Se descarta con los datos de DexScreener, sin gastar requests en verificar.
        self.assertIsNone(client.gecko_pools_requested)

    async def test_jupcat_cae_en_la_verificacion_aunque_pase_los_filtros(self):
        # Aunque el wash trading tuviera más txns y la subida fuera menor,
        # el precio no coincide con el de GeckoTerminal.
        # Sin penalización por subida: con +440.000% en 1h dejaría el score en 0
        # y no llegaría a verificarse, que es lo que prueba este test.
        config = _config(
            max_avg_trade_usd=0.0,
            max_price_change_h1_pct=0.0,
            late_penalty_start_h1_pct=0.0,
            verify_before_alert=True,
        )
        radar, alerter, client = await self._scan(
            config,
            [_jupcat()],
            gecko_pools=_JUPCAT_GECKO,
            candles={_jupcat()["pairAddress"]: _VELAS_TRANQUILAS},
        )
        self.assertEqual(alerter.sent, [])
        self.assertEqual(client.gecko_pools_requested, [_jupcat()["pairAddress"]])
        # Descartado por precio: ni siquiera se piden velas.
        self.assertEqual(client.candles_requested, [])
        self.assertEqual(radar._alerted, {})

    async def test_trade_medio_demasiado_grande(self):
        pair = _con_txns(_pair("TOK", volume=50_000), buys=5, sells=5)  # $5.000 por trade
        _, alerter, _ = await self._scan(_config(max_avg_trade_usd=4_000.0), [pair])
        self.assertEqual(alerter.sent, [])
        _, alerter, _ = await self._scan(_config(max_avg_trade_usd=0.0), [pair])
        self.assertEqual(len(alerter.sent), 1)

    async def test_subida_1h_absurda(self):
        pair = _pair("TOK", change=5_000)
        _, alerter, _ = await self._scan(_config(max_price_change_h1_pct=1_000.0), [pair])
        self.assertEqual(alerter.sent, [])
        # La penalización por subida ya hecha lo dejaría bajo el umbral: aquí
        # se prueba solo el filtro duro.
        config = _config(max_price_change_h1_pct=0.0, late_penalty_start_h1_pct=0.0)
        _, alerter, _ = await self._scan(config, [pair])
        self.assertEqual(len(alerter.sent), 1)

    async def test_pocas_txns(self):
        pair = _con_txns(_pair("TOK"), buys=10, sells=10)
        _, alerter, _ = await self._scan(_config(min_txns_h1=50), [pair])
        self.assertEqual(alerter.sent, [])

    async def test_sin_ventas_es_honeypot(self):
        pair = _con_txns(_pair("TOK"), buys=200, sells=0)
        _, alerter, _ = await self._scan(_config(min_sell_ratio_h1=0.1), [pair])
        self.assertEqual(alerter.sent, [])

    async def test_par_sano_pasa_los_filtros_por_defecto(self):
        pair = _con_txns(_pair("TOK", volume=50_000), buys=300, sells=200)
        pair["pairCreatedAt"] = _hace_horas(24)
        self.assertIsNone(MemecoinRadar(Config())._rejection_reason(pair))


def _pair_verificable(address="TOK", price="0.01", volume=50_000, change=50):
    pair = _pair(address, volume=volume, change=change)
    pair["pairAddress"] = f"POOL_{address}"
    pair["priceUsd"] = price
    return pair


def _gecko(address="TOK", price=0.01, reserve=100_000):
    return {f"POOL_{address}": {"reserve_usd": reserve, "token_prices": {address: price}}}


class TestVerificacion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def _scan(self, pairs, config=None, **client_kwargs):
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": p["baseToken"]["address"]} for p in pairs],
            pairs=pairs,
            **client_kwargs,
        )
        radar = MemecoinRadar(config or _config(verify_before_alert=True))
        alerter = _FakeAlerter()
        await radar._scan_once(client, alerter)
        return radar, alerter, client

    async def test_par_verificado_alerta(self):
        _, alerter, client = await self._scan(
            [_pair_verificable()],
            gecko_pools=_gecko(price=0.012),
            candles={"POOL_TOK": _VELAS_TRANQUILAS},
        )
        self.assertEqual(len(alerter.sent), 1)
        self.assertEqual(client.candles_requested, [("POOL_TOK", "TOK", CANDLE_LOOKBACK_MINUTES)])

    async def test_sin_datos_de_gecko_no_alerta_ni_marca_cooldown(self):
        # Sin verificar no se alerta; se reintenta la próxima pasada.
        radar, alerter, _ = await self._scan([_pair_verificable()])
        self.assertEqual(alerter.sent, [])
        self.assertEqual(radar._alerted, {})

    async def test_precio_distinto_entre_fuentes(self):
        for gecko_price in (0.001, 0.1):  # 10x más bajo y 10x más alto
            with self.subTest(gecko_price=gecko_price):
                _, alerter, _ = await self._scan(
                    [_pair_verificable()],
                    gecko_pools=_gecko(price=gecko_price),
                    candles={"POOL_TOK": _VELAS_TRANQUILAS},
                )
                self.assertEqual(alerter.sent, [])

    async def test_liquidez_real_bajo_el_minimo(self):
        _, alerter, _ = await self._scan(
            [_pair_verificable()],
            config=_config(verify_before_alert=True, min_liquidity_usd=50_000),
            gecko_pools=_gecko(reserve=10_000),
            candles={"POOL_TOK": _VELAS_TRANQUILAS},
        )
        self.assertEqual(alerter.sent, [])

    async def test_desplome_en_una_vela(self):
        velas = _VELAS_TRANQUILAS[:-1] + [_vela(3600, 101, 101, 40, 45)]
        _, alerter, _ = await self._scan(
            [_pair_verificable()], gecko_pools=_gecko(), candles={"POOL_TOK": velas}
        )
        self.assertEqual(alerter.sent, [])

    async def test_sin_velas_no_alerta(self):
        _, alerter, _ = await self._scan([_pair_verificable()], gecko_pools=_gecko())
        self.assertEqual(alerter.sent, [])

    async def test_filtros_de_velas_en_cero_no_piden_velas(self):
        _, alerter, client = await self._scan(
            [_pair_verificable()],
            config=_config(
                verify_before_alert=True,
                max_candle_drop_pct=0.0,
                max_rise_from_low_pct=0.0,
                max_drop_from_recent_high_pct=0.0,
            ),
            gecko_pools=_gecko(),
        )
        self.assertEqual(len(alerter.sent), 1)
        self.assertEqual(client.candles_requested, [])
        self.assertEqual(alerter.candles, [None])

    async def test_solo_filtros_de_llegar_tarde_tambien_piden_velas(self):
        _, alerter, client = await self._scan(
            [_pair_verificable()],
            config=_config(verify_before_alert=True, max_candle_drop_pct=0.0),
            gecko_pools=_gecko(),
            candles={"POOL_TOK": _VELAS_TRANQUILAS},
        )
        self.assertEqual(len(client.candles_requested), 1)
        self.assertEqual(len(alerter.sent), 1)

    async def test_la_alerta_lleva_la_posicion_en_las_velas(self):
        _, alerter, _ = await self._scan(
            [_pair_verificable()], gecko_pools=_gecko(), candles={"POOL_TOK": _VELAS_TRANQUILAS}
        )
        stats = alerter.candles[0]
        # Último cierre 101 sobre mínimo 97 y bajo máximo reciente 104.
        self.assertAlmostEqual(stats.rise_from_low_pct, (101 / 97 - 1) * 100)
        self.assertAlmostEqual(stats.drop_from_recent_high_pct, (1 - 101 / 104) * 100)

    async def test_descarta_si_ya_subio_mucho_sobre_el_minimo(self):
        # De 100 a 300 en la hora, sin desplomes dentro de ninguna vela.
        velas = [_vela(i * 60, 100 + i * 3.4, 100 + (i + 1) * 3.4, 100 + i * 3.4, 100 + (i + 1) * 3.4)
                 for i in range(59)]
        radar, alerter, _ = await self._scan(
            [_pair_verificable()],
            config=_config(verify_before_alert=True, max_rise_from_low_pct=150.0),
            gecko_pools=_gecko(),
            candles={"POOL_TOK": velas},
        )
        self.assertEqual(alerter.sent, [])
        # No marca cooldown: si consolida y vuelve a acelerar puede alertar.
        self.assertEqual(radar._alerted, {})

    async def test_descarta_si_ya_cae_desde_el_maximo_reciente(self):
        velas = _VELAS_TRANQUILAS[:-3] + [
            _vela(3420, 101, 140, 101, 140),
            _vela(3480, 140, 140, 118, 120),
            _vela(3540, 120, 121, 105, 106),
        ]
        _, alerter, _ = await self._scan(
            [_pair_verificable()],
            config=_config(
                verify_before_alert=True,
                max_candle_drop_pct=0.0,
                max_drop_from_recent_high_pct=20.0,
            ),
            gecko_pools=_gecko(),
            candles={"POOL_TOK": velas},
        )
        self.assertEqual(alerter.sent, [])

    async def test_sin_verificacion_no_aplica_llegar_tarde(self):
        _, alerter, client = await self._scan([_pair_verificable()], config=_config())
        self.assertEqual(len(alerter.sent), 1)
        self.assertEqual(client.candles_requested, [])

    async def test_verificacion_apagada_no_consulta_gecko(self):
        _, alerter, client = await self._scan([_pair_verificable()], config=_config())
        self.assertEqual(len(alerter.sent), 1)
        self.assertIsNone(client.gecko_pools_requested)

    async def test_tope_por_pasada_verifica_primero_los_de_mayor_score(self):
        n = MAX_VERIFICATIONS_PER_PASS + 2
        # Momentum decreciente: T0 tiene el score más alto.
        pairs = [_pair_verificable(f"T{i}", change=50 - i) for i in range(n)]
        gecko = {k: v for i in range(n) for k, v in _gecko(f"T{i}").items()}
        velas = {f"POOL_T{i}": _VELAS_TRANQUILAS for i in range(n)}
        radar, alerter, client = await self._scan(
            list(reversed(pairs)),
            config=_config(verify_before_alert=True, score_alert_threshold=50.0),
            gecko_pools=gecko,
            candles=velas,
        )
        esperados = [f"T{i}" for i in range(MAX_VERIFICATIONS_PER_PASS)]
        self.assertEqual([p["baseToken"]["address"] for p, _ in alerter.sent], esperados)
        self.assertEqual(client.gecko_pools_requested, [f"POOL_{a}" for a in esperados])
        # Los que no entraron no quedan en cooldown.
        self.assertEqual(sorted(radar._alerted), esperados)


class TestRegistroDeAlertas(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "alertas.db"

    def _rows(self):
        if not self.path.exists():
            return []
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM alertas ORDER BY id")]
        except sqlite3.OperationalError:  # la base existe pero sin tabla todavía
            return []
        finally:
            conn.close()

    async def _scan(self, radar, candles, alerter=None):
        pair = _pair_verificable()
        client = _FakeClient(
            latest=[{"chainId": "solana", "tokenAddress": "TOK"}],
            pairs=[pair],
            gecko_pools=_gecko(),
            candles={"POOL_TOK": candles},
        )
        await radar._scan_once(client, alerter or _FakeAlerter())

    def _radar(self, **kwargs):
        radar = MemecoinRadar(
            _config(verify_before_alert=True, alert_log_path=str(self.path), **kwargs)
        )
        self.addCleanup(radar._tracker.close)
        return radar

    async def test_registra_la_alerta_enviada(self):
        await self._scan(self._radar(), _VELAS_TRANQUILAS)
        rows = self._rows()
        self.assertEqual([(r["token"], r["enviada"]) for r in rows], [("TOK", 1)])
        self.assertIsNotNone(rows[0]["sobre_minimo_1h_pct"])

    async def test_envio_fallido_no_se_registra(self):
        await self._scan(self._radar(), _VELAS_TRANQUILAS, alerter=_FakeAlerter(fail=True))
        self.assertEqual(self._rows(), [])

    async def test_descarte_por_llegar_tarde_se_registra_una_sola_vez(self):
        """Mientras siga por encima del umbral se descarta en cada pasada:
        una fila por pasada ensuciaría los datos."""
        velas = _VELAS_TRANQUILAS[:-1] + [_vela(3540, 101, 101, 60, 60)]
        radar = self._radar(max_candle_drop_pct=0.0, max_drop_from_recent_high_pct=20.0)
        for _ in range(3):
            await self._scan(radar, velas)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["enviada"], 0)
        self.assertIn("máximo", rows[0]["motivo_descarte"])

    async def test_otros_descartes_de_verificacion_no_se_registran(self):
        velas = _VELAS_TRANQUILAS[:-1] + [_vela(3540, 101, 101, 40, 45)]  # desplome en una vela
        await self._scan(self._radar(), velas)
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
