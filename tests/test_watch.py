"""Tests de la memoria entre pasadas y la detección de arranques contra la
historia propia de cada pool."""

import unittest

from sigpump.watch import (
    HISTORY_MINUTES,
    MIN_BASELINE_VOLUME_M5_USD,
    EarlyThresholds,
    PoolHistory,
)

UMBRALES = EarlyThresholds(
    min_price_move_pct=4.0,
    max_price_move_pct=30.0,
    min_volume_ratio=2.5,
    min_txns_ratio=2.0,
    min_buy_ratio=0.55,
)
AHORA = 1_000_000.0


def _pair(price=1.0, volume_m5=500, buys=5, sells=5, change_m5=0.5, pool="POOL"):
    """Pool tranquilo por defecto: precio plano, $500 y 10 txns en 5 min."""
    return {
        "pairAddress": pool,
        "priceUsd": str(price),
        "volume": {"m5": volume_m5},
        "txns": {"m5": {"buys": buys, "sells": sells}},
        "priceChange": {"m5": change_m5},
    }


def _arranque(**overrides):
    """+10%, volumen x5, txns x3.3 y 76% de compras frente a la base tranquila."""
    base = dict(price=1.1, volume_m5=2_500, buys=25, sells=8, change_m5=8)
    base.update(overrides)
    return _pair(**base)


def _historia(*minutos_atras, **kwargs):
    history = PoolHistory()
    for minutos in minutos_atras:
        history.observe(_pair(**kwargs), AHORA - minutos * 60)
    return history


class TestEarlySignal(unittest.TestCase):
    def _signal(self, history, pair):
        history.observe(pair, AHORA)
        return history.early_signal(pair, AHORA, UMBRALES)

    def test_detecta_el_arranque(self):
        signal = self._signal(_historia(12, 9, 6), _arranque())
        self.assertAlmostEqual(signal.price_move_pct, 10.0)
        self.assertAlmostEqual(signal.volume_ratio, 5.0)
        self.assertAlmostEqual(signal.txns_ratio, 3.3)
        self.assertAlmostEqual(signal.buy_ratio, 25 / 33)
        self.assertAlmostEqual(signal.baseline_minutes, 12.0)

    def test_sin_base_suficiente(self):
        """Las fotos de menos de 5 min comparten ventana de 5m con la actual,
        y con una sola foto un valor raro decidiría todo."""
        for minutos in ((6,), (4, 3, 2)):
            with self.subTest(minutos=minutos):
                self.assertIsNone(self._signal(_historia(*minutos), _arranque()))

    def test_sin_historia(self):
        self.assertIsNone(PoolHistory().early_signal(_arranque(), AHORA, UMBRALES))

    def test_movimiento_fuera_de_rango(self):
        # +2%: todavía no arrancó; +50%: ya subió, llega tarde.
        for price in (1.02, 1.5):
            with self.subTest(price=price):
                self.assertIsNone(self._signal(_historia(12, 9, 6), _arranque(price=price)))

    def test_volumen_txns_o_compras_insuficientes(self):
        casos = {
            "volumen x2": dict(volume_m5=1_000),
            "txns x1.8": dict(buys=12, sells=6),
            "47% compras": dict(buys=14, sells=16),
        }
        for nombre, overrides in casos.items():
            with self.subTest(nombre):
                self.assertIsNone(self._signal(_historia(12, 9, 6), _arranque(**overrides)))

    def test_sin_tope_de_subida_para_ver_si_se_sostiene(self):
        history = _historia(12, 9, 6)
        pair = _arranque(price=1.5)
        history.observe(pair, AHORA)
        self.assertIsNone(history.early_signal(pair, AHORA, UMBRALES))
        self.assertIsNotNone(history.early_signal(pair, AHORA, UMBRALES, check_max_move=False))

    def test_precio_de_5m_bajando_no_es_arranque(self):
        # Subió respecto de la base pero ya está cayendo: el arranque fue antes.
        self.assertIsNone(self._signal(_historia(12, 9, 6), _arranque(change_m5=-1)))

    def test_piso_de_volumen_en_la_base(self):
        # Base con $5 en 5 min: sin piso, $500 serían x100.
        signal = self._signal(_historia(12, 9, 6, volume_m5=5), _arranque(volume_m5=500))
        self.assertAlmostEqual(signal.volume_ratio, 500 / MIN_BASELINE_VOLUME_M5_USD)

    def test_la_historia_es_por_pool(self):
        history = _historia(12, 9, 6, pool="OTRO_POOL")
        self.assertIsNone(self._signal(history, _arranque()))

    def test_mediana_ignora_una_foto_rara(self):
        # Un pico aislado de precio en la base no esconde el arranque.
        history = PoolHistory()
        for minutos, precio in ((12, 1.0), (10, 2.0), (9, 1.0), (6, 1.0)):
            history.observe(_pair(price=precio), AHORA - minutos * 60)
        self.assertIsNotNone(self._signal(history, _arranque()))


class TestPoolHistory(unittest.TestCase):
    def test_descarta_fotos_viejas(self):
        history = _historia(HISTORY_MINUTES + 5, 12, 9)
        history.observe(_arranque(), AHORA)
        signal = history.early_signal(_arranque(), AHORA, UMBRALES)
        self.assertAlmostEqual(signal.baseline_minutes, 12.0)

    def test_prune_elimina_pools_sin_fotos_recientes(self):
        history = _historia(12)
        self.assertEqual(len(history), 1)
        history.prune(AHORA + HISTORY_MINUTES * 60)
        self.assertEqual(len(history), 0)

    def test_ignora_pares_sin_precio_o_sin_pool(self):
        history = PoolHistory()
        history.observe(_pair(price=0), AHORA)
        history.observe(_pair(pool=""), AHORA)
        self.assertEqual(len(history), 0)


if __name__ == "__main__":
    unittest.main()
