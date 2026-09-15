"""Tests de carga/validación de config.toml y de la función de scoring."""

import logging
import tempfile
import unittest
from pathlib import Path

from sigpump.config import Config, ScoringWeights, score_pair


def _pair(
    volume_h1=0, change_h1=0, change_h6=0, liquidity=0, address="TOK",
    volume_m5=0, buys_m5=0, sells_m5=0, change_m5=1,
):
    return {
        "baseToken": {"address": address},
        "volume": {"h1": volume_h1, "m5": volume_m5},
        "txns": {"m5": {"buys": buys_m5, "sells": sells_m5}},
        "priceChange": {"m5": change_m5, "h1": change_h1, "h6": change_h6},
        "liquidity": {"usd": liquidity},
    }


class TestScorePair(unittest.TestCase):
    def setUp(self):
        self.weights = ScoringWeights()

    def test_todo_al_tope_da_100(self):
        pair = _pair(
            volume_h1=50_000, change_h1=50, change_h6=100, liquidity=100_000,
            volume_m5=12_500, buys_m5=30, sells_m5=10,
        )
        self.assertEqual(score_pair(pair, self.weights, {"TOK"}), 100.0)

    def test_aceleracion_de_volumen(self):
        # x1 (mismo ritmo que la hora) no suma; x2 da la mitad; x3 o más, el tope.
        for volume_m5, esperado in ((1_000, 0.0), (2_000, 12.5), (3_000, 25.0), (12_000, 25.0)):
            with self.subTest(volume_m5=volume_m5):
                pair = _pair(volume_h1=12_000, volume_m5=volume_m5)
                # volume_h1 12k -> 24/100 * 0.20 = 4.8 fijos
                self.assertEqual(score_pair(pair, self.weights, set()), round(4.8 + esperado, 1))

    def test_aceleracion_con_precio_bajando_no_suma(self):
        """Visto en datos reales: EMBERCAT sumaba x2.7 de aceleración con el
        precio cayendo 4.75% en 5 min. El volumen también se acelera en un dump."""
        for change_m5 in (-4.75, 0):
            with self.subTest(change_m5=change_m5):
                pair = _pair(volume_h1=12_000, volume_m5=3_000, change_m5=change_m5)
                self.assertEqual(score_pair(pair, self.weights, set()), 4.8)

    def test_presion_compradora(self):
        for buys, sells, esperado in ((20, 20, 0.0), (25, 15, 5.0), (30, 10, 10.0), (10, 30, 0.0)):
            with self.subTest(buys=buys, sells=sells):
                pair = _pair(buys_m5=buys, sells_m5=sells)
                self.assertEqual(score_pair(pair, self.weights, set()), esperado)

    def test_presion_compradora_con_pocas_txns_no_suma(self):
        self.assertEqual(score_pair(_pair(buys_m5=4, sells_m5=0), self.weights, set()), 0.0)

    def test_penalizacion_por_subida_ya_hecha(self):
        """Un +150% en 1h con todo al tope daba 100: la señal salía cuando la
        subida ya se había hecho y poco después caía."""
        base = dict(
            volume_h1=50_000, change_h6=100, liquidity=100_000,
            volume_m5=12_500, buys_m5=30, sells_m5=10,
        )
        casos = ((60, 100.0), (155, 50.0), (250, 0.0), (400, 0.0))
        for change_h1, esperado in casos:
            with self.subTest(change_h1=change_h1):
                pair = _pair(change_h1=change_h1, **base)
                self.assertEqual(score_pair(pair, self.weights, {"TOK"}, 60.0, 250.0), esperado)

    def test_penalizacion_desactivada_por_defecto(self):
        pair = _pair(change_h1=400)
        self.assertEqual(
            score_pair(pair, self.weights, set()), score_pair(pair, self.weights, set(), 0.0, 250.0)
        )
        self.assertEqual(score_pair(pair, self.weights, set()), 20.0)

    def test_pair_vacio_no_rompe(self):
        self.assertIsInstance(score_pair({}, self.weights, set()), float)

    def test_campos_null_se_tratan_como_cero(self):
        pair = {"volume": {"h1": None}, "liquidity": None, "priceChange": None}
        self.assertEqual(score_pair(pair, self.weights, set()), 0.0)

    def test_campos_numericos_como_string(self):
        # DexScreener devuelve varios de estos campos como string.
        pair = _pair(volume_h1="50000", change_h1="50", liquidity="100000")
        texto = score_pair(pair, self.weights, set())
        numero = score_pair(_pair(50_000, 50, 0, 100_000), self.weights, set())
        self.assertEqual(texto, numero)

    def test_boost_solo_suma_si_la_direccion_esta_en_el_set(self):
        pair = _pair(address="TOK")
        self.assertEqual(
            score_pair(pair, self.weights, {"TOK"}) - score_pair(pair, self.weights, set()),
            10.0,
        )

    def test_momentum_neutro_no_aporta_puntos(self):
        """Regresión: 0% de cambio puntuaba 50/100 en ambos sub-scores de
        momentum (20 puntos regalados), y un token cayendo 20% con buen
        volumen, liquidez y boost llegaba a 73.5 y disparaba alerta."""
        self.assertEqual(score_pair(_pair(), self.weights, set()), 0.0)

        cayendo = _pair(volume_h1=50_000, change_h1=-20, change_h6=-20, liquidity=100_000)
        score = score_pair(cayendo, self.weights, {"TOK"})
        self.assertEqual(score, 40.0)
        self.assertLess(score, Config().score_alert_threshold)

    def test_momentum_es_lineal_hasta_el_tope(self):
        # +25% h1 -> 50/100 * 0.20 = 10; +50% h6 -> 50/100 * 0.05 = 2.5
        self.assertEqual(score_pair(_pair(change_h1=25), self.weights, set()), 10.0)
        self.assertEqual(score_pair(_pair(change_h6=50), self.weights, set()), 2.5)

    def test_subida_vieja_sin_aceleracion_no_llega_al_umbral(self):
        """Un token con buen volumen que subió +45% en la hora pero ya no se
        mueve daba 97.5 y alertaba justo cuando la subida había terminado."""
        quieto = _pair(
            volume_h1=80_000, change_h1=45, change_h6=100, liquidity=100_000,
            volume_m5=3_000, buys_m5=20, sells_m5=20,
        )
        self.assertLess(score_pair(quieto, self.weights, {"TOK"}), 65.0)


class TestScoringWeights(unittest.TestCase):
    def test_clave_desconocida_da_error_explicativo(self):
        with self.assertRaises(ValueError) as ctx:
            ScoringWeights.from_raw({"volumen_h1": 0.5})
        self.assertIn("volumen_h1", str(ctx.exception))
        self.assertIn("scoring_weights", str(ctx.exception))

    def test_valor_no_numerico_da_error(self):
        with self.assertRaises(ValueError):
            ScoringWeights.from_raw({"volume_h1": "mucho"})

    def test_peso_negativo_da_error(self):
        with self.assertRaises(ValueError):
            ScoringWeights.from_raw({"volume_h1": -0.5})

    def test_claves_ausentes_toman_el_default(self):
        w = ScoringWeights.from_raw({"volume_h1": 0.4, "liquidity": 0.1})
        self.assertEqual(w.volume_h1, 0.4)
        self.assertEqual(w.price_change_h1, ScoringWeights().price_change_h1)

    def test_suma_distinta_de_uno_avisa(self):
        # Suman 0.5 -> el score máximo es 50 y un umbral de 70 no dispara nunca.
        with self.assertLogs(level=logging.WARNING) as logs:
            ScoringWeights.from_raw(
                {
                    "volume_h1": 0.2,
                    "volume_acceleration": 0.0,
                    "buy_pressure": 0.0,
                    "price_change_h1": 0.1,
                    "price_change_h6": 0.1,
                    "liquidity": 0.05,
                    "boosted": 0.05,
                }
            )
        self.assertIn("0.50", "".join(logs.output))

    def test_suma_uno_no_avisa(self):
        with self.assertNoLogs(level=logging.WARNING):
            ScoringWeights.from_raw({})


class TestConfigValidation(unittest.TestCase):
    def test_poll_interval_cero_da_error(self):
        with self.assertRaises(ValueError):
            Config(poll_interval_seconds=0)

    def test_top_n_cero_da_error(self):
        with self.assertRaises(ValueError):
            Config(top_n_candidates=0)

    def test_paginas_de_geckoterminal_fuera_de_rango_da_error(self):
        for pages in (-1, 11):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                Config(geckoterminal_pages=pages)

    def test_umbral_fuera_de_rango_da_error(self):
        with self.assertRaises(ValueError):
            Config(score_alert_threshold=150.0)

    def test_minimo_negativo_da_error(self):
        with self.assertRaises(ValueError):
            Config(min_liquidity_usd=-1.0)

    def test_chain_vacia_da_error(self):
        with self.assertRaises(ValueError):
            Config(chain_id="")

    def test_tipo_invalido_da_error_de_config(self):
        """`poll_interval_seconds = "90"` tiraba un TypeError crudo que run.py
        no captura, en vez de un error de configuración."""
        casos = {
            "poll_interval_seconds": "90",
            "top_n_candidates": 1.5,
            "geckoterminal_pages": True,
            "verbose": "si",
            "telegram_message_thread_id": "123",
        }
        for campo, valor in casos.items():
            with self.subTest(campo=campo), self.assertRaises(ValueError):
                Config(**{campo: valor})

    def test_float_en_campos_numericos_es_valido(self):
        Config(poll_interval_seconds=30.5, alert_cooldown_minutes=0.5)

    def test_filtros_anti_manipulacion_fuera_de_rango_dan_error(self):
        casos = {
            "min_sell_ratio_h1": 1.5,
            "max_candle_drop_pct": 150.0,
            "max_avg_trade_usd": -1.0,
            "max_price_change_h1_pct": -1.0,
            "max_price_deviation_pct": -1.0,
            "min_txns_h1": -1,
            "verify_before_alert": "si",
            "max_rise_from_low_pct": -1.0,
            "max_drop_from_recent_high_pct": 120.0,
            "late_penalty_start_h1_pct": -5.0,
            "alert_log_path": None,
        }
        for campo, valor in casos.items():
            with self.subTest(campo=campo), self.assertRaises(ValueError):
                Config(**{campo: valor})

    def test_penalizacion_con_fin_antes_del_inicio_da_error(self):
        with self.assertRaises(ValueError):
            Config(late_penalty_start_h1_pct=100.0, late_penalty_end_h1_pct=50.0)

    def test_penalizacion_desactivada_no_valida_el_fin(self):
        Config(late_penalty_start_h1_pct=0.0, late_penalty_end_h1_pct=0.0)


class TestFromToml(unittest.TestCase):
    def _write(self, contenido: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tmp.write(contenido)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_archivo_ausente(self):
        with self.assertRaises(FileNotFoundError):
            Config.from_toml(Path("/no/existe.toml"))

    def test_toml_vacio_usa_defaults(self):
        config = Config.from_toml(self._write(""))
        self.assertEqual(config, Config())

    def test_secciones_parciales(self):
        config = Config.from_toml(
            self._write('[telegram]\nbot_token = "t"\nchat_id = "c"\n[radar]\nverbose = true\n')
        )
        self.assertEqual(config.telegram_bot_token, "t")
        self.assertTrue(config.verbose)
        self.assertIsNone(config.telegram_message_thread_id)
        self.assertEqual(config.poll_interval_seconds, 90)

    def test_clave_desconocida_avisa(self):
        # Un typo se ignoraba en silencio y se usaba el default.
        with self.assertLogs(level=logging.WARNING) as logs:
            config = Config.from_toml(self._write("[dexscreener]\nmin_liquidty_usd = 1\n"))
        self.assertIn("min_liquidty_usd", "".join(logs.output))
        self.assertEqual(config.min_liquidity_usd, Config().min_liquidity_usd)

    def test_seccion_desconocida_avisa(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            Config.from_toml(self._write("[telegrm]\nbot_token = 't'\n"))
        self.assertIn("telegrm", "".join(logs.output))

    def test_seccion_que_no_es_tabla_da_error(self):
        with self.assertRaises(ValueError):
            Config.from_toml(self._write("radar = 5\n"))

    def test_lee_los_filtros_anti_manipulacion(self):
        config = Config.from_toml(
            self._write(
                "[dexscreener]\nmin_txns_h1 = 100\nmax_avg_trade_usd = 0\n"
                "[geckoterminal]\nverify_before_alert = false\nmax_candle_drop_pct = 20.0\n"
            )
        )
        self.assertEqual(config.min_txns_h1, 100)
        self.assertEqual(config.max_avg_trade_usd, 0)
        self.assertFalse(config.verify_before_alert)
        self.assertEqual(config.max_candle_drop_pct, 20.0)
        self.assertEqual(config.max_price_deviation_pct, Config().max_price_deviation_pct)

    def test_lee_los_parametros_de_llegar_tarde_y_el_registro(self):
        config = Config.from_toml(
            self._write(
                "[geckoterminal]\nmax_rise_from_low_pct = 90\nmax_drop_from_recent_high_pct = 10\n"
                "[scoring]\nlate_penalty_start_h1_pct = 40\nlate_penalty_end_h1_pct = 120\n"
                '[radar]\nalert_log_path = ""\n'
            )
        )
        self.assertEqual(config.max_rise_from_low_pct, 90)
        self.assertEqual(config.max_drop_from_recent_high_pct, 10)
        self.assertEqual(config.late_penalty_start_h1_pct, 40)
        self.assertEqual(config.late_penalty_end_h1_pct, 120)
        self.assertEqual(config.alert_log_path, "")

    def test_clave_desconocida_en_scoring_avisa(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            Config.from_toml(self._write("[scoring]\nlate_penalty_h1 = 1\n"))
        self.assertIn("late_penalty_h1", "".join(logs.output))

    def test_chat_id_numerico_se_acepta_como_str(self):
        config = Config.from_toml(self._write("[telegram]\nchat_id = -100123\n"))
        self.assertEqual(config.telegram_chat_id, "-100123")

    def test_ejemplo_del_repo_es_valido(self):
        # config.example.toml es lo que copia el usuario: tiene que parsear.
        config = Config.from_toml(Path("config.example.toml"))
        self.assertEqual(config.chain_id, "solana")
        self.assertAlmostEqual(config.weights.total, 1.0)


if __name__ == "__main__":
    unittest.main()
