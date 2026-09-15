"""Tests de las métricas derivadas: aceleración de volumen, presión
compradora de 5 min y posición del precio en la ventana de velas."""

import unittest

from sigpump.signals import (
    MIN_TXNS_M5,
    RECENT_HIGH_MINUTES,
    buy_ratio_m5,
    candle_stats,
    volume_acceleration,
)


def _vela(t, open_, high, low, close):
    return (float(t), float(open_), float(high), float(low), float(close))


class TestVolumeAcceleration(unittest.TestCase):
    def test_mismo_ritmo_que_la_hora_da_uno(self):
        self.assertAlmostEqual(volume_acceleration({"volume": {"m5": 1_000, "h1": 12_000}}), 1.0)

    def test_triple_de_ritmo(self):
        self.assertAlmostEqual(volume_acceleration({"volume": {"m5": 3_000, "h1": 12_000}}), 3.0)

    def test_sin_volumen_1h_da_cero(self):
        for pair in ({}, {"volume": None}, {"volume": {"m5": 500, "h1": 0}}):
            with self.subTest(pair=pair):
                self.assertEqual(volume_acceleration(pair), 0.0)

    def test_numeros_como_string(self):
        self.assertAlmostEqual(volume_acceleration({"volume": {"m5": "2000", "h1": "12000"}}), 2.0)


class TestBuyRatioM5(unittest.TestCase):
    def test_fraccion_de_compras(self):
        self.assertAlmostEqual(buy_ratio_m5({"txns": {"m5": {"buys": 30, "sells": 10}}}), 0.75)

    def test_pocas_txns_no_dicen_nada(self):
        pocas = {"txns": {"m5": {"buys": MIN_TXNS_M5 - 1, "sells": 0}}}
        self.assertIsNone(buy_ratio_m5(pocas))

    def test_sin_datos_de_5m(self):
        for pair in ({}, {"txns": None}, {"txns": {"h1": {"buys": 10}}}, {"txns": {"m5": "x"}}):
            with self.subTest(pair=pair):
                self.assertIsNone(buy_ratio_m5(pair))


class TestCandleStats(unittest.TestCase):
    def test_sin_velas(self):
        self.assertIsNone(candle_stats([]))

    def test_subida_sobre_el_minimo_de_toda_la_ventana(self):
        velas = [_vela(0, 100, 100, 50, 60), _vela(60, 60, 150, 60, 150)]
        stats = candle_stats(velas)
        self.assertAlmostEqual(stats.rise_from_low_pct, 200.0)
        self.assertEqual(stats.drop_from_recent_high_pct, 0.0)

    def test_caida_desde_el_maximo_reciente(self):
        velas = [_vela(0, 100, 200, 100, 200), _vela(60, 200, 200, 150, 160)]
        self.assertAlmostEqual(candle_stats(velas).drop_from_recent_high_pct, 20.0)

    def test_maximo_viejo_no_cuenta_como_reciente(self):
        """El pico de hace 40 min y la consolidación posterior no significan
        que la subida de ahora esté terminando."""
        velas = [
            _vela(0, 100, 300, 100, 150),
            _vela((RECENT_HIGH_MINUTES + 25) * 60, 150, 160, 140, 155),
        ]
        stats = candle_stats(velas)
        self.assertAlmostEqual(stats.drop_from_recent_high_pct, (1 - 155 / 160) * 100)

    def test_ventana_reciente_por_timestamp_y_no_por_cantidad(self):
        # GeckoTerminal omite los minutos sin trades: 2 velas pueden cubrir 30 min.
        velas = [_vela(0, 100, 200, 100, 200), _vela(30 * 60, 120, 130, 110, 120)]
        self.assertAlmostEqual(candle_stats(velas).drop_from_recent_high_pct, (1 - 120 / 130) * 100)

    def test_rasgos_de_como_venia_la_subida(self):
        planas = [(i * 60, 1.0, 1.01, 0.99, 1.0, 100.0) for i in range(20)]
        verdes = [
            ((20 + i) * 60, 1.0 + i * 0.02, 1.03 + i * 0.02, 1.0 + i * 0.02, 1.02 + i * 0.02, 300.0)
            for i in range(3)
        ]
        stats = candle_stats(planas + verdes)
        self.assertEqual(stats.green_streak, 3)
        # 3 velas con 300 frente a 3 con 100: el volumen explotó.
        self.assertAlmostEqual(stats.volume_trend, 3.0)
        self.assertAlmostEqual(stats.upper_wick, 1 / 3)
        self.assertAlmostEqual(stats.rise_15m_pct, (1.06 / 0.99 - 1) * 100)

    def test_sin_volumen_no_hay_tendencia(self):
        velas = [_vela(i * 60, 1, 1, 1, 1) for i in range(10)]
        self.assertIsNone(candle_stats(velas).volume_trend)

    def test_until_ignora_velas_sin_cerrar_al_dar_la_senal(self):
        velas = [_vela(0, 1, 1, 1, 1), _vela(60, 1, 2, 1, 2), _vela(120, 2, 5, 2, 5)]
        # Señal a los 120 s: la vela de los 60 ya cerró, la de los 120 no.
        self.assertAlmostEqual(candle_stats(velas, until=120).rise_from_low_pct, 100.0)

    def test_precios_invalidos(self):
        self.assertIsNone(candle_stats([_vela(0, 0, 0, 0, 0)]))
