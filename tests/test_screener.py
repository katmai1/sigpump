"""Tests del cliente HTTP: reintentos, backoff, batching y tolerancia a
respuestas con forma inesperada. No tocan la red: la sesión es un doble."""

import logging
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from sigpump.screener import MAX_ADDRESSES_PER_CALL, MAX_RETRIES, DexScreenerClient


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

    async def test_lote_sin_datos_no_corta_los_siguientes(self):
        addresses = [f"A{i}" for i in range(MAX_ADDRESSES_PER_CALL + 1)]
        session = _FakeSession(
            *[_FakeResponse(429)] * (MAX_RETRIES + 1),
            _FakeResponse(200, {"pairs": [{"chainId": "solana", "id": 9}]}),
        )
        pairs = await DexScreenerClient(session).get_pairs_for_tokens("solana", addresses)
        self.assertEqual(pairs, [{"chainId": "solana", "id": 9}])


if __name__ == "__main__":
    unittest.main()
