"""Tests de los helpers compartidos: métricas del par de 5 min (aceleración
de volumen y presión compradora) que se muestran en el aviso y se registran."""

import unittest

from sigpump.util import MIN_TXNS_FOR_BUY_RATIO, bot_volume_m5, buy_ratio_m5, volume_acceleration


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
        pocas = {"txns": {"m5": {"buys": MIN_TXNS_FOR_BUY_RATIO - 1, "sells": 0}}}
        self.assertIsNone(buy_ratio_m5(pocas))

    def test_sin_datos_de_5m(self):
        for pair in ({}, {"txns": None}, {"txns": {"h1": {"buys": 10}}}, {"txns": {"m5": "x"}}):
            with self.subTest(pair=pair):
                self.assertIsNone(buy_ratio_m5(pair))


def _par_5m(buys, sells, volumen):
    return {"txns": {"m5": {"buys": buys, "sells": sells}}, "volume": {"m5": volumen}}


class TestBotVolumeM5(unittest.TestCase):
    def test_hell(self):
        """Los números del aviso de HELL del 2026-11-07."""
        txns, usd_tx, ratio = bot_volume_m5(_par_5m(126, 128, 275.15), 50, 5, 2)
        self.assertEqual(txns, 254)
        self.assertAlmostEqual(usd_tx, 275.15 / 254)
        self.assertAlmostEqual(ratio, 126 / 254)

    def test_txns_de_centimos_aunque_no_sea_mitad_y_mitad(self):
        self.assertIsNotNone(bot_volume_m5(_par_5m(80, 20, 300), 50, 5, 2))

    def test_mitad_y_mitad_aunque_sean_txns_grandes(self):
        self.assertIsNotNone(bot_volume_m5(_par_5m(51, 50, 20_000), 50, 5, 2))

    def test_volumen_normal_no_avisa(self):
        self.assertIsNone(bot_volume_m5(_par_5m(70, 30, 20_000), 50, 5, 2))

    def test_pocas_txns_o_apagado(self):
        self.assertIsNone(bot_volume_m5(_par_5m(20, 20, 10), 50, 5, 2))
        self.assertIsNone(bot_volume_m5(_par_5m(126, 128, 275), 0, 5, 2))
        self.assertIsNone(bot_volume_m5({}, 50, 5, 2))

    def test_pocas_txns_sin_porcentaje_de_compras(self):
        # Con min_txns por debajo de MIN_TXNS_FOR_BUY_RATIO no hay % de compras.
        self.assertEqual(bot_volume_m5(_par_5m(2, 1, 3), 1, 5, 2), (3, 1.0, None))



if __name__ == "__main__":
    unittest.main()
