"""Tests de la comprobación de autoridades del mint: qué se considera
peligroso, el cacheo y la tolerancia a fallos del RPC."""

import asyncio
import logging
import time
import unittest

import aiohttp

from sigpump.solana import MAX_ACCOUNTS_PER_CALL, UNSAFE_TTL_SECONDS, TokenAuthorities


def _cuenta(mint=None, freeze=None):
    return {"data": {"parsed": {"info": {"mintAuthority": mint, "freezeAuthority": freeze}}}}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(request_info=None, history=(), status=self.status)

    async def json(self, content_type=None):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.peticiones: list[list[str]] = []

    def post(self, url, json=None, timeout=None):
        self.peticiones.append(json["params"][0])
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _respuesta(*cuentas):
    return _FakeResponse({"jsonrpc": "2.0", "result": {"value": list(cuentas)}})


class TestTokenAuthorities(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def test_detecta_minteables_y_congelables(self):
        session = _FakeSession(_respuesta(
            _cuenta(),
            _cuenta(mint="Alguien"),
            _cuenta(freeze="Alguien"),
            _cuenta(mint="Alguien", freeze="Alguien"),
        ))
        reasons = await TokenAuthorities(session).unsafe_reasons(["LIMPIO", "MINT", "FREEZE", "AMBAS"])
        self.assertEqual(set(reasons), {"MINT", "FREEZE", "AMBAS"})
        self.assertIn("acuñar", reasons["MINT"])
        self.assertIn("congelar", reasons["FREEZE"])
        self.assertIn("y", reasons["AMBAS"])

    async def test_no_vuelve_a_consultar_un_token_limpio(self):
        session = _FakeSession(_respuesta(_cuenta()))
        authorities = TokenAuthorities(session)
        self.assertEqual(await authorities.unsafe_reasons(["LIMPIO"]), {})
        # Las autoridades solo se pueden renunciar: limpio es para siempre.
        self.assertEqual(await authorities.unsafe_reasons(["LIMPIO"]), {})
        self.assertEqual(len(session.peticiones), 1)

    async def test_un_token_con_autoridades_se_revisa_de_nuevo(self):
        session = _FakeSession(_respuesta(_cuenta(mint="Alguien")), _respuesta(_cuenta()))
        authorities = TokenAuthorities(session)
        self.assertIn("MINT", await authorities.unsafe_reasons(["MINT"]))
        # Pasado el TTL puede haberlas renunciado.
        reason, _ = authorities._cache["MINT"]
        authorities._cache["MINT"] = (reason, time.time() - UNSAFE_TTL_SECONDS - 1)
        self.assertEqual(await authorities.unsafe_reasons(["MINT"]), {})
        self.assertEqual(len(session.peticiones), 2)

    async def test_cuenta_inexistente_o_rara_no_bloquea_ni_se_cachea(self):
        session = _FakeSession(_respuesta(None, {"data": "no parseado"}), _respuesta(_cuenta(), _cuenta()))
        authorities = TokenAuthorities(session)
        self.assertEqual(await authorities.unsafe_reasons(["NADA", "RARO"]), {})
        await authorities.unsafe_reasons(["NADA", "RARO"])
        self.assertEqual(len(session.peticiones), 2)

    async def test_lotes_de_100(self):
        direcciones = [f"T{i}" for i in range(MAX_ACCOUNTS_PER_CALL + 10)]
        session = _FakeSession(
            _respuesta(*[_cuenta()] * MAX_ACCOUNTS_PER_CALL), _respuesta(*[_cuenta()] * 10)
        )
        await TokenAuthorities(session).unsafe_reasons(direcciones)
        self.assertEqual([len(p) for p in session.peticiones], [MAX_ACCOUNTS_PER_CALL, 10])

    async def test_rpc_caido_deja_pasar_y_no_cachea(self):
        """Es preferible dejar pasar una alerta a perderlas todas porque el RPC
        no responda."""
        casos = (
            aiohttp.ClientConnectionError("sin red"),
            _FakeResponse({"error": {"code": -32005, "message": "rate limit"}}),
            _FakeResponse({"result": {"value": []}}),  # menos cuentas de las pedidas
            _FakeResponse(ValueError("<html>")),
        )
        for caso in casos:
            with self.subTest(caso=type(caso).__name__):
                session = _FakeSession(caso, _respuesta(_cuenta(mint="Alguien")))
                authorities = TokenAuthorities(session)
                self.assertEqual(await authorities.unsafe_reasons(["TOK"]), {})
                # No se cacheó: al siguiente intento se vuelve a preguntar.
                self.assertIn("TOK", await authorities.unsafe_reasons(["TOK"]))

    async def test_pide_el_mint_parseado_al_rpc(self):
        session = _FakeSession(_respuesta(_cuenta()))
        await TokenAuthorities(session, "https://rpc.ejemplo").unsafe_reasons(["TOK", "TOK"])
        self.assertEqual(session.peticiones, [["TOK"]])


if __name__ == "__main__":
    unittest.main()
