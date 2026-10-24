"""Tests de la comprobación de autoridades del mint: qué se considera
peligroso, el cacheo y la tolerancia a fallos del RPC."""

import dataclasses
import logging
import time
import unittest
from unittest.mock import patch

import aiohttp

from sigpump.solana import (
    MAX_ACCOUNTS_PER_CALL,
    RPC_MAX_RETRIES,
    UNSAFE_TTL_SECONDS,
    RateLimiter,
    RpcClient,
    TokenAuthorities,
    redact,
    rpc_call,
)


def _cuenta(mint=None, freeze=None, fee_bps=None, nueva_fee_bps=None):
    info = {"mintAuthority": mint, "freezeAuthority": freeze}
    if fee_bps is not None:
        info["extensions"] = [
            {"extension": "metadataPointer", "state": {}},
            {"extension": "transferFeeConfig", "state": {
                "olderTransferFee": {"epoch": 1, "maximumFee": 10**15, "transferFeeBasisPoints": fee_bps},
                "newerTransferFee": {"epoch": 2, "maximumFee": 10**15,
                                     "transferFeeBasisPoints": fee_bps if nueva_fee_bps is None else nueva_fee_bps},
            }},
        ]
    return {"data": {"parsed": {"info": info}}}


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
        reasons = await TokenAuthorities(RpcClient(session)).unsafe_reasons(["LIMPIO", "MINT", "FREEZE", "AMBAS"])
        self.assertEqual(set(reasons), {"MINT", "FREEZE", "AMBAS"})
        self.assertIn("acuñar", reasons["MINT"])
        self.assertIn("congelar", reasons["FREEZE"])
        self.assertIn("y", reasons["AMBAS"])

    async def test_no_vuelve_a_consultar_un_token_limpio(self):
        session = _FakeSession(_respuesta(_cuenta()))
        authorities = TokenAuthorities(RpcClient(session))
        self.assertEqual(await authorities.unsafe_reasons(["LIMPIO"]), {})
        # Las autoridades solo se pueden renunciar: limpio es para siempre.
        self.assertEqual(await authorities.unsafe_reasons(["LIMPIO"]), {})
        self.assertEqual(len(session.peticiones), 1)

    async def test_un_token_con_autoridades_se_revisa_de_nuevo(self):
        session = _FakeSession(_respuesta(_cuenta(mint="Alguien")), _respuesta(_cuenta()))
        authorities = TokenAuthorities(RpcClient(session))
        self.assertIn("MINT", await authorities.unsafe_reasons(["MINT"]))
        # Pasado el TTL puede haberlas renunciado.
        authorities._cache["MINT"] = dataclasses.replace(
            authorities._cache["MINT"], checked_at=time.time() - UNSAFE_TTL_SECONDS - 1
        )
        self.assertEqual(await authorities.unsafe_reasons(["MINT"]), {})
        self.assertEqual(len(session.peticiones), 2)

    async def test_cuenta_inexistente_o_rara_no_bloquea_ni_se_cachea(self):
        session = _FakeSession(_respuesta(None, {"data": "no parseado"}), _respuesta(_cuenta(), _cuenta()))
        authorities = TokenAuthorities(RpcClient(session))
        self.assertEqual(await authorities.unsafe_reasons(["NADA", "RARO"]), {})
        await authorities.unsafe_reasons(["NADA", "RARO"])
        self.assertEqual(len(session.peticiones), 2)

    async def test_lotes_de_100(self):
        direcciones = [f"T{i}" for i in range(MAX_ACCOUNTS_PER_CALL + 10)]
        session = _FakeSession(
            _respuesta(*[_cuenta()] * MAX_ACCOUNTS_PER_CALL), _respuesta(*[_cuenta()] * 10)
        )
        await TokenAuthorities(RpcClient(session)).unsafe_reasons(direcciones)
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
                authorities = TokenAuthorities(RpcClient(session))
                self.assertEqual(await authorities.unsafe_reasons(["TOK"]), {})
                # No se cacheó: al siguiente intento se vuelve a preguntar.
                self.assertIn("TOK", await authorities.unsafe_reasons(["TOK"]))

    async def test_un_429_se_reintenta_y_no_deja_pasar_el_token(self):
        """Antes un 429 dejaba el lote sin comprobar y el aviso salía como limpio."""
        session = _FakeSession(_FakeResponse({}, 429), _respuesta(_cuenta(mint="Alguien")))
        with patch("sigpump.solana.asyncio.sleep"):
            reasons = await TokenAuthorities(RpcClient(session)).unsafe_reasons(["TOK"])
        self.assertIn("TOK", reasons)
        self.assertEqual(len(session.peticiones), 2)

    async def test_usa_el_limitador_que_se_le_da(self):
        limiter = RateLimiter(1000)
        llamadas = []
        original = limiter.wait

        async def wait():
            llamadas.append(1)
            await original()

        limiter.wait = wait
        await TokenAuthorities(RpcClient(_FakeSession(_respuesta(_cuenta())), limiter=limiter)).unsafe_reasons(["TOK"])
        self.assertEqual(llamadas, [1])

    async def test_comision_de_transferencia(self):
        session = _FakeSession(_respuesta(_cuenta(fee_bps=300), _cuenta(), _cuenta(fee_bps=100, nueva_fee_bps=500)))
        authorities = TokenAuthorities(RpcClient(session))
        await authorities.unsafe_reasons(["FEE", "SIN", "SUBE"])
        # De la vigente y la programada, la mayor: el aviso no debe subestimarla.
        self.assertEqual(
            [authorities.transfer_fee_bps(m) for m in ("FEE", "SIN", "SUBE", "NO_CONSULTADO")], [300, 0, 500, None]
        )

    async def test_un_token_con_comision_se_revisa_de_nuevo(self):
        # La autoridad de la comisión puede cambiarla: no se cachea para siempre.
        session = _FakeSession(_respuesta(_cuenta(fee_bps=100)), _respuesta(_cuenta(fee_bps=300)))
        authorities = TokenAuthorities(RpcClient(session))
        await authorities.unsafe_reasons(["FEE"])
        await authorities.unsafe_reasons(["FEE"])
        self.assertEqual(len(session.peticiones), 1)
        authorities._cache["FEE"] = dataclasses.replace(
            authorities._cache["FEE"], checked_at=time.time() - UNSAFE_TTL_SECONDS - 1
        )
        await authorities.unsafe_reasons(["FEE"])
        self.assertEqual(authorities.transfer_fee_bps("FEE"), 300)

    async def test_pide_el_mint_parseado_al_rpc(self):
        session = _FakeSession(_respuesta(_cuenta()))
        await TokenAuthorities(RpcClient(session, "https://rpc.ejemplo")).unsafe_reasons(["TOK", "TOK"])
        self.assertEqual(session.peticiones, [["TOK"]])


class TestRpcCall(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("sigpump.solana.asyncio.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    async def _llamar(self, session):
        return await rpc_call(session, "https://rpc/?api-key=SECRETO", "getX", ["a"], RateLimiter(1000), 15)

    async def test_rpc_client_cuenta_las_llamadas_sin_los_reintentos(self):
        session = _FakeSession(_FakeResponse({}, 429), _FakeResponse({"result": 1}), _FakeResponse({"result": 2}))
        rpc = RpcClient(session, limiter=RateLimiter(1000))
        await rpc.call("getX", ["a"])
        await rpc.call("getX", ["b"])
        self.assertEqual((rpc.calls, len(session.peticiones)), (2, 3))

    async def test_429_se_reintenta_y_devuelve_el_resultado(self):
        session = _FakeSession(_FakeResponse({}, 429), _FakeResponse({}, 503), _FakeResponse({"result": 7}))
        self.assertEqual(await self._llamar(session), 7)
        self.assertEqual(len(session.peticiones), 3)

    async def test_agotados_los_reintentos_lanza_valueerror_sin_la_key(self):
        session = _FakeSession(*[_FakeResponse({}, 429)] * (RPC_MAX_RETRIES + 1))
        with self.assertRaises(ValueError) as ctx:
            await self._llamar(session)
        self.assertIn("429", str(ctx.exception))
        self.assertNotIn("SECRETO", str(ctx.exception))
        self.assertEqual(len(session.peticiones), RPC_MAX_RETRIES + 1)

    async def test_4xx_no_se_reintenta(self):
        session = _FakeSession(_FakeResponse({}, 401))
        with self.assertRaises(ValueError):
            await self._llamar(session)
        self.assertEqual(len(session.peticiones), 1)

    async def test_error_del_rpc(self):
        session = _FakeSession(_FakeResponse({"error": {"code": -32602}}))
        with self.assertRaises(ValueError):
            await self._llamar(session)


class TestRedact(unittest.TestCase):
    def test_quita_la_api_key(self):
        texto = "429, message='Too Many Requests', url='https://mainnet.helius-rpc.com/?api-key=5836-abc'"
        self.assertEqual(
            redact(texto), "429, message='Too Many Requests', url='https://mainnet.helius-rpc.com/?api-key=***'"
        )

    def test_sin_key_no_cambia(self):
        self.assertEqual(redact("https://api.mainnet-beta.solana.com"), "https://api.mainnet-beta.solana.com")


if __name__ == "__main__":
    unittest.main()
