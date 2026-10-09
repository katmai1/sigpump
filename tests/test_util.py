"""Tests de los helpers compartidos: métricas del par de 5 min (aceleración
de volumen y presión compradora) que se muestran en el aviso y se registran."""

import unittest

from sigpump.util import MIN_TXNS_M5, buy_ratio_m5, volume_acceleration


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



if __name__ == "__main__":
    unittest.main()
