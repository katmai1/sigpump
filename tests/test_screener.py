"""Tests del cliente HTTP: reintentos, backoff, batching y tolerancia a
respuestas con forma inesperada. No tocan la red: la sesión es un doble."""

import logging
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from sigpump.screener import (
    GECKO_MAX_PAGES,
    GECKO_MAX_POOLS_PER_CALL,
    GECKO_MAX_RETRIES,
    GECKO_RATE_LIMIT_WAIT_SECONDS,
    GECKO_REQUEST_INTERVAL_SECONDS,
    MAX_ADDRESSES_PER_CALL,
    MAX_RETRIES,
    DexScreenerClient,
)


class _FakeResponse:
    """Imita lo que aiohttp devuelve al entrar en `async with session.get(...)`."""

    def __init__(self, status: int, payload=None):
        self.status = status
        self._payload = payload

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status
            )

    async def json(self, content_type=None):
        # Un payload excepción imita un cuerpo que no es JSON (JSONDecodeError).
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRaisingContext:
    """session.get(...) que explota al entrar, como un timeout o un corte de red."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            return _FakeRaisingContext(item)
        return item


class ScreenerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Los warnings de reintento son esperados acá: no ensucian la salida.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        # El backoff real haría que la suite tarde 14s; acá solo importa que
        # se espere entre intentos, no cuánto.
        sleep = patch("sigpump.screener.asyncio.sleep", new=AsyncMock())
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)


class TestRetries(ScreenerTestCase):
    async def test_429_se_reintenta_y_devuelve_datos(self):
        session = _FakeSession(_FakeResponse(429), _FakeResponse(200, [{"ok": 1}]))
        client = DexScreenerClient(session)
        self.assertEqual(await client._get("/x"), [{"ok": 1}])
        self.assertEqual(len(session.urls), 2)

    async def test_500_se_reintenta(self):
        """Antes solo se reintentaba el 429: un 502 pasaba por raise_for_status()
        y tiraba abajo la pasada completa."""
        session = _FakeSession(_FakeResponse(502), _FakeResponse(200, []))
        self.assertEqual(await DexScreenerClient(session)._get("/x"), [])

    async def test_timeout_de_red_se_reintenta(self):
        session = _FakeSession(TimeoutError(), _FakeResponse(200, {"pairs": []}))
        self.assertEqual(await DexScreenerClient(session)._get("/x"), {"pairs": []})

    async def test_error_de_conexion_se_reintenta(self):
        session = _FakeSession(aiohttp.ClientConnectionError("dns"), _FakeResponse(200, []))
        self.assertEqual(await DexScreenerClient(session)._get("/x"), [])

    async def test_404_no_se_reintenta(self):
        # Un 4xx (que no sea 429) es error nuestro: reintentarlo no arregla nada.
        session = _FakeSession(_FakeResponse(404))
        with self.assertRaises(aiohttp.ClientResponseError):
            await DexScreenerClient(session)._get("/x")
        self.assertEqual(len(session.urls), 1)

    async def test_reintentos_agotados_devuelve_none(self):
        session = _FakeSession(*[_FakeResponse(429)] * (MAX_RETRIES + 1))
        self.assertIsNone(await DexScreenerClient(session)._get("/x"))
        self.assertEqual(len(session.urls), MAX_RETRIES + 1)
        self.assertEqual(self.sleep.await_count, MAX_RETRIES)

    async def test_backoff_exponencial(self):
        session = _FakeSession(*[_FakeResponse(429)] * (MAX_RETRIES + 1))
        await DexScreenerClient(session)._get("/x")
        esperas = [call.args[0] for call in self.sleep.await_args_list]
        self.assertEqual(esperas, [2.0, 4.0, 8.0])


class TestListEndpoints(ScreenerTestCase):
    async def test_respuesta_no_lista_devuelve_vacio(self):
        # Si la API cambia de forma, mejor "sin datos" que un AttributeError.
        session = _FakeSession(_FakeResponse(200, {"error": "nope"}))
        self.assertEqual(await DexScreenerClient(session).get_latest_boosted(), [])

    async def test_filtra_elementos_que_no_son_dict(self):
        session = _FakeSession(_FakeResponse(200, [{"a": 1}, "basura", None]))
        self.assertEqual(await DexScreenerClient(session).get_top_boosted(), [{"a": 1}])

    async def test_sin_datos_devuelve_vacio(self):
        session = _FakeSession(*[_FakeResponse(429)] * (MAX_RETRIES + 1))
        self.assertEqual(await DexScreenerClient(session).get_latest_profiles(), [])


def _pool(base, quote="SOL", network="solana"):
    return {
        "relationships": {
            "base_token": {"data": {"id": f"{network}_{base}"}},
            "quote_token": {"data": {"id": f"{network}_{quote}"}},
        }
    }


class TestGetTrendingTokens(ScreenerTestCase):
    async def test_pagina_y_conserva_el_orden_sin_repetidos(self):
        session = _FakeSession(
            _FakeResponse(200, {"data": [_pool("A"), _pool("B")]}),
            _FakeResponse(200, {"data": [_pool("A"), _pool("C")]}),
        )
        tokens = await DexScreenerClient(session).get_trending_tokens("solana", 2)
        self.assertEqual(tokens, ["A", "B", "C"])
        self.assertIn("api.geckoterminal.com", session.urls[0])
        self.assertTrue(session.urls[1].endswith("/networks/solana/trending_pools?page=2"))

    async def test_descarta_bases_que_son_quote_en_otro_pool(self):
        # Un pool SOL/USDC no debe meter a SOL como memecoin trending.
        session = _FakeSession(
            _FakeResponse(200, {"data": [_pool("SOL", quote="USDC"), _pool("MEME")]})
        )
        tokens = await DexScreenerClient(session).get_trending_tokens("solana", 1)
        self.assertEqual(tokens, ["MEME"])

    async def test_mapea_el_network_de_geckoterminal(self):
        session = _FakeSession(_FakeResponse(200, {"data": [_pool("A", network="eth")]}))
        tokens = await DexScreenerClient(session).get_trending_tokens("ethereum", 1)
        self.assertEqual(tokens, ["A"])
        self.assertIn("/networks/eth/", session.urls[0])

    async def test_pagina_vacia_corta_la_paginacion(self):
        session = _FakeSession(_FakeResponse(200, {"data": []}))
        self.assertEqual(await DexScreenerClient(session).get_trending_tokens("solana", 5), [])
        self.assertEqual(len(session.urls), 1)

    async def test_no_pasa_del_maximo_de_paginas(self):
        session = _FakeSession(
            *[_FakeResponse(200, {"data": [_pool(f"T{i}")]}) for i in range(GECKO_MAX_PAGES)]
        )
        await DexScreenerClient(session).get_trending_tokens("solana", 50)
        self.assertEqual(len(session.urls), GECKO_MAX_PAGES)

    async def test_cero_paginas_no_hace_requests(self):
        session = _FakeSession()
        self.assertEqual(await DexScreenerClient(session).get_trending_tokens("solana", 0), [])
        self.assertEqual(session.urls, [])

    async def test_forma_inesperada_no_rompe(self):
        payload = {"data": ["basura", {"relationships": None}, {"relationships": {}}, _pool("A")]}
        session = _FakeSession(_FakeResponse(200, payload))
        self.assertEqual(await DexScreenerClient(session).get_trending_tokens("solana", 1), ["A"])


class TestGetPairsForTokens(ScreenerTestCase):
    async def test_batching_respeta_el_limite_de_la_api(self):
        addresses = [f"A{i}" for i in range(MAX_ADDRESSES_PER_CALL + 5)]
        session = _FakeSession(_FakeResponse(200, {"pairs": []}), _FakeResponse(200, {"pairs": []}))
        await DexScreenerClient(session).get_pairs_for_tokens("solana", addresses)
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(session.urls[0].count(","), MAX_ADDRESSES_PER_CALL - 1)
        self.assertTrue(session.urls[1].endswith(",".join(addresses[MAX_ADDRESSES_PER_CALL:])))

    async def test_filtra_otras_chains_y_basura(self):
        payload = {
            "pairs": [
                {"chainId": "solana", "id": 1},
                {"chainId": "base", "id": 2},
                "no soy un dict",
            ]
        }
        session = _FakeSession(_FakeResponse(200, payload))
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", ["A"])
        self.assertEqual(pairs, [{"chainId": "solana", "id": 1}])

    async def test_acepta_lista_pelada(self):
        session = _FakeSession(_FakeResponse(200, [{"chainId": "solana", "id": 1}]))
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", ["A"])
        self.assertEqual(len(pairs), 1)

    async def test_usa_el_endpoint_tokens_v1_con_la_chain(self):
        """/latest/dex/tokens corta en 30 pares por respuesta en total: los
        tokens con muchos pools dejaban sin datos a otros del lote."""
        session = _FakeSession(_FakeResponse(200, []))
        await DexScreenerClient(session).get_pairs_for_tokens("solana", ["A", "B"])
        self.assertTrue(session.urls[0].endswith("/tokens/v1/solana/A,B"))

    async def test_4xx_en_un_lote_no_corta_los_siguientes(self):
        """Un 403 de Cloudflare subía hasta el loop y la pasada no alertaba nada."""
        addresses = [f"A{i}" for i in range(MAX_ADDRESSES_PER_CALL + 1)]
        session = _FakeSession(
            _FakeResponse(403), _FakeResponse(200, [{"chainId": "solana", "id": 9}])
        )
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", addresses)
        self.assertEqual(pairs, [{"chainId": "solana", "id": 9}])

    async def test_cuerpo_no_json_en_un_lote_no_corta_los_siguientes(self):
        addresses = [f"A{i}" for i in range(MAX_ADDRESSES_PER_CALL + 1)]
        session = _FakeSession(
            _FakeResponse(200, ValueError("<html>")),
            _FakeResponse(200, [{"chainId": "solana", "id": 9}]),
        )
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", addresses)
        self.assertEqual(pairs, [{"chainId": "solana", "id": 9}])

    async def test_lote_sin_datos_no_corta_los_siguientes(self):
        addresses = [f"A{i}" for i in range(MAX_ADDRESSES_PER_CALL + 1)]
        session = _FakeSession(
            *[_FakeResponse(429)] * (MAX_RETRIES + 1),
            _FakeResponse(200, {"pairs": [{"chainId": "solana", "id": 9}]}),
        )
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", addresses)
        self.assertEqual(pairs, [{"chainId": "solana", "id": 9}])


class TestGeckoSpacing(ScreenerTestCase):
    async def test_requests_seguidos_a_gecko_se_espacian(self):
        session = _FakeSession(_FakeResponse(200, {}), _FakeResponse(200, {}))
        client = DexScreenerClient(session)
        await client._gecko_get("/a")
        self.sleep.assert_not_awaited()
        await client._gecko_get("/b")
        self.sleep.assert_awaited_once()
        self.assertGreater(self.sleep.await_args.args[0], 0)
        self.assertLessEqual(self.sleep.await_args.args[0], GECKO_REQUEST_INTERVAL_SECONDS)

    async def test_429_de_gecko_espera_la_ventana_del_minuto(self):
        """Con backoff de 2s/4s/8s los reintentos caían en la misma ventana,
        daban 429 igual y la verificación se quedaba sin velas."""
        session = _FakeSession(_FakeResponse(429), _FakeResponse(200, {"ok": 1}))
        self.assertEqual(await DexScreenerClient(session)._gecko_get("/a"), {"ok": 1})
        self.assertEqual(
            [call.args[0] for call in self.sleep.await_args_list], [GECKO_RATE_LIMIT_WAIT_SECONDS]
        )

    async def test_5xx_de_gecko_mantiene_el_backoff_corto(self):
        session = _FakeSession(_FakeResponse(502), _FakeResponse(200, {}))
        await DexScreenerClient(session)._gecko_get("/a")
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [2.0])

    async def test_429_de_gecko_agota_sus_propios_reintentos(self):
        session = _FakeSession(*[_FakeResponse(429)] * (GECKO_MAX_RETRIES + 1))
        self.assertIsNone(await DexScreenerClient(session)._gecko_get("/a"))
        self.assertEqual(len(session.urls), GECKO_MAX_RETRIES + 1)


def _gecko_pool(address, base, quote, base_price, quote_price, reserve):
    return {
        "id": f"solana_{address}",
        "attributes": {
            "address": address,
            "base_token_price_usd": base_price,
            "quote_token_price_usd": quote_price,
            "reserve_in_usd": reserve,
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{base}"}},
            "quote_token": {"data": {"id": f"solana_{quote}"}},
        },
    }


class TestGetGeckoPools(ScreenerTestCase):
    async def test_indexa_por_pool_y_precios_por_token(self):
        payload = {"data": [_gecko_pool("P1", "MEME", "SOL", "0.000296", "150.5", "53158.68")]}
        session = _FakeSession(_FakeResponse(200, payload))
        pools = await DexScreenerClient(session).get_gecko_pools("solana", ["P1", "P2"])
        self.assertTrue(session.urls[0].endswith("/networks/solana/pools/multi/P1,P2"))
        self.assertEqual(
            pools,
            {"P1": {"reserve_usd": 53158.68, "token_prices": {"MEME": 0.000296, "SOL": 150.5}}},
        )

    async def test_direcciones_evm_normalizadas(self):
        payload = {"data": [_gecko_pool("0xABc", "0xDeF", "0x111", "1", "2", "3")]}
        session = _FakeSession(_FakeResponse(200, payload))
        pools = await DexScreenerClient(session).get_gecko_pools("ethereum", ["0xABc"])
        self.assertIn("/networks/eth/", session.urls[0])
        self.assertEqual(set(pools), {"0xabc"})
        self.assertIn("0xdef", pools["0xabc"]["token_prices"])

    async def test_4xx_devuelve_vacio(self):
        session = _FakeSession(_FakeResponse(404))
        self.assertEqual(await DexScreenerClient(session).get_gecko_pools("solana", ["P"]), {})

    async def test_forma_inesperada_no_rompe(self):
        payload = {"data": ["basura", {"attributes": None}, {"attributes": {}, "relationships": {}}]}
        session = _FakeSession(_FakeResponse(200, payload))
        self.assertEqual(await DexScreenerClient(session).get_gecko_pools("solana", ["P"]), {})

    async def test_lotes_de_30(self):
        addresses = [f"P{i}" for i in range(GECKO_MAX_POOLS_PER_CALL + 1)]
        session = _FakeSession(_FakeResponse(200, {"data": []}), _FakeResponse(200, {"data": []}))
        await DexScreenerClient(session).get_gecko_pools("solana", addresses)
        self.assertEqual(len(session.urls), 2)


class TestGetPoolCandles(ScreenerTestCase):
    async def test_ordena_de_la_mas_vieja_a_la_mas_nueva(self):
        payload = {
            "data": {
                "attributes": {
                    "ohlcv_list": [
                        [120, 3, 3, 3, 3, 10],
                        [60, "2", 2, 2, 2, 10],
                        "basura",
                        [0, 1, 1, 1],
                    ]
                }
            }
        }
        session = _FakeSession(_FakeResponse(200, payload))
        candles = await DexScreenerClient(session).get_pool_candles("solana", "P", "MEME", 60)
        self.assertEqual(candles, [(60.0, 2.0, 2.0, 2.0, 2.0), (120.0, 3.0, 3.0, 3.0, 3.0)])
        self.assertIn("/networks/solana/pools/P/ohlcv/minute?", session.urls[0])
        self.assertIn("limit=60", session.urls[0])
        # Precio del token pedido aunque GeckoTerminal oriente el par al revés.
        self.assertIn("token=MEME", session.urls[0])

    async def test_error_devuelve_vacio(self):
        for response in (_FakeResponse(404), _FakeResponse(200, {"data": None})):
            with self.subTest(status=response.status):
                session = _FakeSession(response)
                self.assertEqual(
                    await DexScreenerClient(session).get_pool_candles("solana", "P", "M", 60), []
                )


if __name__ == "__main__":
    unittest.main()
