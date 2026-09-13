"""Tests del formateo del mensaje de Telegram: escapado de HTML y tolerancia
a campos numéricos ausentes/null/string. No envía nada: format_message() es
pura y el Bot nunca se inicializa."""

import unittest
from unittest.mock import AsyncMock, patch

from sigpump.telegram import TelegramAlerter

TOKEN_FALSO = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _alerter(chain_id="solana"):
    return TelegramAlerter(TOKEN_FALSO, "-100123", chain_id=chain_id)


def _pair(**overrides):
    pair = {
        "baseToken": {"symbol": "PEPE", "name": "Pepe Coin", "address": "AbC123"},
        "priceUsd": "0.00042",
        "liquidity": {"usd": 120_000.0},
        "volume": {"h1": 45_000.0},
        "priceChange": {"h1": 12.3},
        "marketCap": 2_500_000,
        "dexId": "raydium",
        "url": "https://dexscreener.com/solana/AbC123",
    }
    pair.update(overrides)
    return pair


class TestFormatMessage(unittest.TestCase):
    def test_mensaje_completo(self):
        texto = _alerter().format_message(_pair(), 87.5)
        self.assertIn("<b>Pepe Coin (PEPE)</b>", texto)
        self.assertIn("Score: <b>87.5</b>/100", texto)
        self.assertIn("Cambio 1h: +12.3%", texto)
        self.assertIn("Volumen 1h: $45,000", texto)
        self.assertIn("Liquidez: $120,000", texto)
        self.assertIn("Cap. mercado: $2,500,000", texto)
        self.assertIn("<code>AbC123</code>", texto)

    def test_campos_null_no_rompen_el_formato(self):
        """Con `"usd": null` el `.get("usd", 0)` devolvía None y el formato
        `:,.0f` tiraba TypeError, perdiendo la alerta en silencio."""
        texto = _alerter().format_message(
            _pair(liquidity={"usd": None}, volume={"h1": None}, priceChange={}), 70.0
        )
        self.assertIn("Liquidez: $0", texto)
        self.assertIn("Volumen 1h: $0", texto)
        self.assertIn("Cambio 1h: +0.0%", texto)

    def test_secciones_ausentes_o_null_no_rompen(self):
        for pair in ({}, {"liquidity": None, "volume": None, "priceChange": None}):
            with self.subTest(pair=pair):
                self.assertIn("Score:", _alerter().format_message(pair, 70.0))

    def test_numeros_como_string(self):
        texto = _alerter().format_message(_pair(volume={"h1": "45000"}), 70.0)
        self.assertIn("Volumen 1h: $45,000", texto)

    def test_cambio_negativo_lleva_signo(self):
        texto = _alerter().format_message(_pair(priceChange={"h1": -8.25}), 70.0)
        self.assertIn("Cambio 1h: -8.2%", texto)

    def test_fdv_como_fallback_de_market_cap(self):
        pair = _pair(fdv=999_000)
        del pair["marketCap"]
        self.assertIn("Cap. mercado: $999,000", _alerter().format_message(pair, 70.0))

    def test_market_cap_null_cae_en_fdv(self):
        self.assertIn(
            "Cap. mercado: $999,000",
            _alerter().format_message(_pair(marketCap=None, fdv=999_000), 70.0),
        )


class TestEscaping(unittest.TestCase):
    def test_escapa_symbol_y_name(self):
        # symbol/name los elige quien crea el token: son entrada hostil.
        texto = _alerter().format_message(
            _pair(baseToken={"symbol": "<b>X", "name": "A & B", "address": "AbC"}), 70.0
        )
        self.assertIn("&lt;b&gt;X", texto)
        self.assertIn("A &amp; B", texto)
        self.assertNotIn("<b>X", texto)

    def test_escapa_la_url_dentro_del_href(self):
        """Una comilla sin escapar rompe el tag y Telegram rechaza el mensaje
        entero con 400, no solo el link."""
        texto = _alerter().format_message(
            _pair(url='https://x.com/a"><script>alert(1)</script>'), 70.0
        )
        self.assertNotIn("<script>", texto)
        self.assertIn("&quot;", texto)
        self.assertEqual(texto.count('<a href="'), 1)

    def test_escapa_la_direccion(self):
        texto = _alerter().format_message(
            _pair(baseToken={"symbol": "S", "name": "N", "address": "<i>x"}), 70.0
        )
        self.assertIn("<code>&lt;i&gt;x</code>", texto)

    def test_escapa_el_precio(self):
        texto = _alerter().format_message(_pair(priceUsd="<b>0.1"), 70.0)
        self.assertIn("&lt;b&gt;0.1", texto)


class TestUrlFallback(unittest.TestCase):
    def test_usa_la_url_de_la_api_si_existe(self):
        texto = _alerter().format_message(_pair(url="https://dexscreener.com/x/y"), 70.0)
        self.assertIn('href="https://dexscreener.com/x/y"', texto)

    def test_fallback_respeta_la_chain_configurada(self):
        """El fallback hardcodeaba /solana/ aunque chain_id fuera otra."""
        pair = _pair()
        del pair["url"]
        texto = _alerter(chain_id="base").format_message(pair, 70.0)
        self.assertIn('href="https://dexscreener.com/base/AbC123"', texto)
        self.assertNotIn("/solana/", texto)

    def test_url_null_cae_en_el_fallback(self):
        texto = _alerter(chain_id="bsc").format_message(_pair(url=None), 70.0)
        self.assertIn("https://dexscreener.com/bsc/AbC123", texto)


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    def _con_bot_falso(self):
        """python-telegram-bot bloquea setattr sobre Bot, así que se reemplaza
        el objeto entero por un doble."""
        alerter = _alerter()
        alerter._bot = AsyncMock()
        return alerter

    async def test_el_context_manager_inicializa_y_cierra_el_bot(self):
        """initialize() valida el token con getMe al arrancar; sin esto un
        token inválido recién fallaba en la primera alerta. shutdown() cierra
        el cliente HTTP de python-telegram-bot al salir."""
        alerter = self._con_bot_falso()
        async with alerter as entrado:
            self.assertIs(entrado, alerter)
            alerter._bot.initialize.assert_awaited_once()
            alerter._bot.shutdown.assert_not_awaited()
        alerter._bot.shutdown.assert_awaited_once()

    async def test_cierra_el_bot_aunque_el_cuerpo_falle(self):
        alerter = self._con_bot_falso()
        with self.assertRaises(RuntimeError):
            async with alerter:
                raise RuntimeError("scan roto")
        alerter._bot.shutdown.assert_awaited_once()

    async def test_send_usa_el_mensaje_formateado(self):
        alerter = self._con_bot_falso()
        await alerter.send(_pair(), 91.0)
        kwargs = alerter._bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "-100123")
        self.assertIn("Score: <b>91.0</b>/100", kwargs["text"])
        self.assertTrue(kwargs["disable_web_page_preview"])


if __name__ == "__main__":
    unittest.main()
