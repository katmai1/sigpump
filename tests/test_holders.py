"""Tests de la concentración del supply: qué holders cuentan, el creador
del token, las páginas de firmas permitidas, el cacheo y los fallos del RPC."""

import logging
import unittest

from sigpump.holders import SIGNATURES_PAGE, SYSTEM_PROGRAM, Holdings, TokenHolders
from sigpump.solana import RateLimiter, RpcClient
from sigpump.telegram import format_holdings
from tests.helpers import _FakeResponse

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_V4 = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


class _Cadena:
    """Sesión HTTP falsa que responde como un RPC de Solana con un solo
    token: `cuentas` es {token account: (dueño, cantidad)} y `programas`
    {dueño: programa dueño, o None si no existe}."""

    def __init__(self, cuentas, programas, supply=1000, firmas=None, creador="DEV", fallar=()):
        self.cuentas = cuentas
        self.programas = programas
        self.supply = supply
        self.firmas = ["S0"] if firmas is None else firmas  # de la más nueva a la más vieja
        self.creador = creador
        self.fallar = set(fallar)
        self.llamadas: list[str] = []

    def post(self, url, json=None, timeout=None):
        method, params = json["method"], json["params"]
        self.llamadas.append(method)
        if method in self.fallar:
            return _FakeResponse({"jsonrpc": "2.0", "error": {"code": -32000, "message": "caído"}})
        return _FakeResponse({"jsonrpc": "2.0", "result": self._resultado(method, params)})

    def _resultado(self, method, params):
        if method == "getTokenLargestAccounts":
            orden = sorted(self.cuentas, key=lambda c: -self.cuentas[c][1])[:20]
            return {"value": [{"address": c, "amount": str(self.cuentas[c][1])} for c in orden]}
        if method == "getMultipleAccounts" and params[1]["encoding"] == "jsonParsed":
            *cuentas, mint = params[0]
            value = [self._token_account(c) for c in cuentas]
            return {"value": value + [{"data": {"parsed": {"info": {"supply": str(self.supply), "decimals": 6}}}}]}
        if method == "getMultipleAccounts":
            return {"value": [
                None if self.programas.get(o) is None else {"owner": self.programas[o]} for o in params[0]
            ]}
        if method == "getTokenAccountsByOwner":
            return {"value": [
                {"account": self._token_account(c)} for c, (o, _) in self.cuentas.items() if o == params[0]
            ]}
        if method == "getSignaturesForAddress":
            firmas = self.firmas
            if "before" in params[1]:
                firmas = firmas[firmas.index(params[1]["before"]) + 1:]
            return [{"signature": f} for f in firmas[: params[1]["limit"]]]
        if method == "getTransaction":
            assert params[0] == self.firmas[-1], "tiene que leer la transacción más vieja"
            return {"transaction": {"message": {"accountKeys": [self.creador, "MINT"]}}}
        raise AssertionError(method)

    def _token_account(self, cuenta):
        dueño, cantidad = self.cuentas[cuenta]
        return {"data": {"parsed": {"info": {"owner": dueño, "tokenAmount": {"amount": str(cantidad)}}}}}


def _holders(cadena, paginas=3):
    return TokenHolders(RpcClient(cadena, "http://rpc", RateLimiter(10_000)), paginas)


# Pool del AMM con la mitad, el dev con 100 y diez wallets con 20 cada una
# (más una undécima con 10, que ya no entra en el top 10).
_CUENTAS = {
    "VAULT": ("POOL", 500),
    "A_DEV": ("DEV", 100),
    **{f"A{i}": (f"W{i}", 20) for i in range(10)},
    "A10": ("W10", 10),
}
_PROGRAMAS = {"POOL": PUMP_AMM, "DEV": SYSTEM_PROGRAM, **{f"W{i}": SYSTEM_PROGRAM for i in range(11)}}


class TestTokenHolders(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def test_cuenta_personas_y_no_el_pool(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS)
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        # Top 10: el dev (100) y nueve wallets de 20 = 280 de 1000. El pool no cuenta.
        self.assertAlmostEqual(h.top10_pct, 28.0)
        self.assertAlmostEqual(h.max_holder_pct, 10.0)
        self.assertEqual(h.dev_wallet, "DEV")
        self.assertAlmostEqual(h.dev_pct, 10.0)
        # El dev estaba entre los mayores: no hace falta pedir su saldo aparte.
        self.assertNotIn("getTokenAccountsByOwner", cadena.llamadas)

    async def test_autoridades_de_raydium_cuentas_inexistentes_y_quemados_no_son_personas(self):
        cuentas = {
            "V": (RAYDIUM_V4, 400),
            "PDA": ("SIN_SOL", 300),
            "Q": ("1nc1nerator11111111111111111111111111111111", 200),
            "W": ("W", 50),
        }
        programas = {RAYDIUM_V4: SYSTEM_PROGRAM, "W": SYSTEM_PROGRAM, "1nc1nerator11111111111111111111111111111111": SYSTEM_PROGRAM}
        h = (await _holders(_Cadena(cuentas, programas, creador="W")).holdings(["MINT"]))["MINT"]
        self.assertAlmostEqual(h.top10_pct, 5.0)
        self.assertAlmostEqual(h.dev_pct, 5.0)

    async def test_dev_fuera_de_los_mayores_se_pide_aparte(self):
        cadena = _Cadena({**_CUENTAS, "OTRA": ("DEV2", 3)}, _PROGRAMAS, creador="DEV2")
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        self.assertIn("getTokenAccountsByOwner", cadena.llamadas)
        self.assertAlmostEqual(h.dev_pct, 0.3)

    async def test_dev_que_vendio_todo_tiene_cero(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, creador="VENDIO")
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        self.assertEqual(h.dev_wallet, "VENDIO")
        self.assertEqual(h.dev_pct, 0.0)

    async def test_busca_el_creador_pagina_a_pagina(self):
        firmas = [f"S{i}" for i in range(SIGNATURES_PAGE * 2 + 5)]
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, firmas=firmas)
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        self.assertEqual(cadena.llamadas.count("getSignaturesForAddress"), 3)
        self.assertEqual(h.dev_wallet, "DEV")

    async def test_pagina_justa_de_mil_firmas(self):
        # La última página llena no dice si hay más: hace falta otra, vacía.
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, firmas=[f"S{i}" for i in range(SIGNATURES_PAGE)])
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        self.assertEqual(cadena.llamadas.count("getSignaturesForAddress"), 2)
        self.assertEqual(h.dev_wallet, "DEV")

    async def test_token_demasiado_viejo_deja_el_dev_vacio_y_no_lo_vuelve_a_buscar(self):
        firmas = [f"S{i}" for i in range(SIGNATURES_PAGE * 3 + 1)]
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, firmas=firmas)
        holders = _holders(cadena, paginas=2)
        h = (await holders.holdings(["MINT"]))["MINT"]
        self.assertIsNone(h.dev_wallet)
        self.assertIsNone(h.dev_pct)
        self.assertAlmostEqual(h.top10_pct, 28.0)
        self.assertNotIn("getTransaction", cadena.llamadas)
        holders._cache.clear()  # pasado el TTL de la medida
        cadena.llamadas.clear()
        await holders.holdings(["MINT"])
        self.assertNotIn("getSignaturesForAddress", cadena.llamadas)

    async def test_sin_paginas_no_busca_el_creador(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS)
        h = (await _holders(cadena, paginas=0).holdings(["MINT"]))["MINT"]
        self.assertIsNone(h.dev_pct)
        self.assertNotIn("getSignaturesForAddress", cadena.llamadas)

    async def test_reutiliza_la_medida_y_el_creador(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS)
        holders = _holders(cadena)
        await holders.holdings(["MINT", "MINT"])
        llamadas = len(cadena.llamadas)
        await holders.holdings(["MINT"])
        self.assertEqual(len(cadena.llamadas), llamadas)
        holders._cache.clear()
        cadena.llamadas.clear()
        await holders.holdings(["MINT"])
        self.assertNotIn("getTransaction", cadena.llamadas)

    async def test_rpc_caido_no_rompe_nada(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, fallar={"getTokenLargestAccounts", "getSignaturesForAddress"})
        holders = _holders(cadena)
        h = (await holders.holdings(["MINT"]))["MINT"]
        self.assertEqual(h, Holdings())
        # Un fallo no se recuerda como "sin creador": se vuelve a buscar.
        self.assertNotIn("MINT", holders._creators)

    async def test_falla_solo_el_creador(self):
        cadena = _Cadena(_CUENTAS, _PROGRAMAS, fallar={"getSignaturesForAddress"})
        h = (await _holders(cadena).holdings(["MINT"]))["MINT"]
        self.assertAlmostEqual(h.top10_pct, 28.0)
        self.assertIsNone(h.dev_pct)


class TestFormatHoldings(unittest.TestCase):
    def test_texto_del_aviso(self):
        self.assertEqual(format_holdings(Holdings(35.2, 9.1, "DEV", 12.4)), "dev 12% · top 10 35% (mayor 9%)")
        self.assertEqual(format_holdings(Holdings(35.2, 9.1)), "dev ? · top 10 35% (mayor 9%)")
        self.assertEqual(format_holdings(Holdings(dev_wallet="DEV", dev_pct=3.0)), "dev 3%")
        self.assertEqual(format_holdings(Holdings()), "")
        self.assertEqual(format_holdings(None), "")
