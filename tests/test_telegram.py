"""Tests del formateo del mensaje de Telegram: escapado de HTML y tolerancia
a campos numéricos ausentes/null/string. No envía nada: format_message() es
pura y el Bot nunca se inicializa."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter

from sigpump.signals import CandleStats, EarlySignal
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


class TestMomentum(unittest.TestCase):
    def test_aceleracion_y_compras_de_5m(self):
        pair = _pair(
            volume={"h1": 12_000, "m5": 3_000},
            txns={"m5": {"buys": 30, "sells": 10}},
            priceChange={"m5": 4.5, "h1": 12.3},
        )
        texto = _alerter().format_message(pair, 80.0)
        self.assertIn("Cambio 5m: +4.5%", texto)
        self.assertIn("Aceleración vol. 5m: x3.0", texto)
        self.assertIn("Compras 5m: 75%", texto)
        self.assertNotIn("Sobre mínimo", texto)

    def test_sin_txns_de_5m_no_muestra_compras(self):
        self.assertNotIn("Compras 5m", _alerter().format_message(_pair(), 80.0))

    def test_con_velas_muestra_subida_y_caida_desde_el_pico(self):
        texto = _alerter().format_message(_pair(), 80.0, CandleStats(42.4, 6.6))
        self.assertIn("Sobre mínimo 1h: +42%", texto)
        self.assertIn("Bajo máximo 15m: -7%", texto)

    def test_send_pasa_las_velas_al_mensaje(self):
        alerter = _alerter()
        alerter._bot = AsyncMock()
        asyncio.run(alerter.send(_pair(), 80.0, CandleStats(10.0, 1.0)))
        self.assertIn("Sobre mínimo 1h: +10%", alerter._bot.send_message.await_args.kwargs["text"])


class TestPrealerta(unittest.TestCase):
    def test_destaca_el_arranque(self):
        early = EarlySignal(
            price_move_pct=9.6, volume_ratio=4.2, txns_ratio=3.1, buy_ratio=0.7, baseline_minutes=12.4
        )
        texto = _alerter().format_message(_pair(), 55.0, early=early)
        self.assertTrue(texto.startswith("⚡ <b>PREALERTA Pepe Coin (PEPE)</b>\n"))
        self.assertIn("Arranque: <b>+9.6%</b> sobre la base de 12 min", texto)
        self.assertIn("Vol. 5m x4.2 y txns 5m x3.1 sobre la base", texto)
        self.assertIn("Score: <b>55.0</b>/100", texto)
        self.assertNotIn("🎯", texto)

    def test_alerta_normal_mantiene_el_encabezado(self):
        texto = _alerter().format_message(_pair(), 80.0)
        self.assertTrue(texto.startswith("🎯 <b>Pepe Coin (PEPE)</b>\n"))
        self.assertNotIn("PREALERTA", texto)

    def test_send_pasa_la_prealerta_al_mensaje(self):
        alerter = _alerter()
        alerter._bot = AsyncMock()
        early = EarlySignal(5.0, 3.0, 2.5, 0.6, 10.0)
        asyncio.run(alerter.send(_pair(), 50.0, early=early))
        self.assertIn("PREALERTA", alerter._bot.send_message.await_args.kwargs["text"])


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
        self.assertEqual(texto.count('<a href="'), 2)  # DexScreener + Photon

    def test_escapa_la_direccion_en_photon(self):
        texto = _alerter().format_message(
            _pair(baseToken={"symbol": "S", "name": "N", "address": 'P"><script>'}), 70.0
        )
        self.assertNotIn("<script>", texto)
        self.assertEqual(texto.count('<a href="'), 2)

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


class TestPhotonLink(unittest.TestCase):
    def test_link_de_photon_usa_la_direccion_del_token(self):
        texto = _alerter().format_message(_pair(pairAddress="Pool999"), 70.0)
        self.assertIn('href="https://photon-sol.tinyastro.io/en/lp/AbC123"', texto)
        self.assertNotIn("Pool999", texto)

    def test_sin_direccion_no_hay_link_de_photon(self):
        texto = _alerter().format_message(_pair(baseToken={"symbol": "S"}), 70.0)
        self.assertNotIn("photon", texto)

    def test_sin_photon_fuera_de_solana(self):
        texto = _alerter(chain_id="base").format_message(_pair(), 70.0)
        self.assertNotIn("photon", texto)

    def test_chain_del_par_tiene_prioridad(self):
        texto = _alerter().format_message(_pair(chainId="ethereum"), 70.0)
        self.assertNotIn("photon", texto)


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

    async def test_flood_control_espera_y_reintenta_una_vez(self):
        alerter = self._con_bot_falso()
        alerter._bot.send_message.side_effect = [RetryAfter(3), None]
        with patch("sigpump.telegram.asyncio.sleep", new=AsyncMock()) as sleep:
            await alerter.send(_pair(), 91.0)
        sleep.assert_awaited_once_with(3.0)
        self.assertEqual(alerter._bot.send_message.await_count, 2)

    async def test_flood_control_demasiado_largo_no_bloquea_el_radar(self):
        alerter = self._con_bot_falso()
        alerter._bot.send_message.side_effect = RetryAfter(3600)
        with patch("sigpump.telegram.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(RetryAfter):
                await alerter.send(_pair(), 91.0)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
