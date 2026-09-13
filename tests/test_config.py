"""Tests de carga/validación de config.toml y de la función de scoring."""

import logging
import tempfile
import unittest
from pathlib import Path

from sigpump.config import Config, ScoringWeights, score_pair


def _pair(volume_h1=0, change_h1=0, change_h6=0, liquidity=0, address="TOK"):
    return {
        "baseToken": {"address": address},
        "volume": {"h1": volume_h1},
        "priceChange": {"h1": change_h1, "h6": change_h6},
        "liquidity": {"usd": liquidity},
    }


class TestScorePair(unittest.TestCase):
    def setUp(self):
        self.weights = ScoringWeights()

    def test_todo_al_tope_da_100(self):
        pair = _pair(volume_h1=50_000, change_h1=50, change_h6=100, liquidity=100_000)
        self.assertEqual(score_pair(pair, self.weights, {"TOK"}), 100.0)

    def test_pair_vacio_no_rompe(self):
        self.assertIsInstance(score_pair({}, self.weights, set()), float)

    def test_campos_null_se_tratan_como_cero(self):
        pair = {"volume": {"h1": None}, "liquidity": None, "priceChange": None}
        self.assertEqual(score_pair(pair, self.weights, set()), 20.0)

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

    def test_momentum_neutro_aporta_20_puntos_de_base(self):
        """BUG CONOCIDO (sin corregir): 0% de cambio puntúa 50/100 en ambos
        sub-scores de momentum, o sea 20 puntos regalados sobre 100.

        Consecuencia: un token *cayendo* 20% en 1h y 6h, con buen volumen,
        liquidez y boost, llega a 73.5 y dispara alerta con el umbral por
        defecto de 70. Si se cambia la curva de momentum, este test falla:
        es intencional, hay que actualizarlo con la nueva semántica."""
        self.assertEqual(score_pair(_pair(), self.weights, set()), 20.0)

        cayendo = _pair(volume_h1=50_000, change_h1=-20, change_h6=-20, liquidity=100_000)
        score = score_pair(cayendo, self.weights, {"TOK"})
        self.assertEqual(score, 73.5)
        self.assertGreaterEqual(score, Config().score_alert_threshold)


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

    def test_ejemplo_del_repo_es_valido(self):
        # config.example.toml es lo que copia el usuario: tiene que parsear.
        config = Config.from_toml(Path("config.example.toml"))
        self.assertEqual(config.chain_id, "solana")
        self.assertAlmostEqual(config.weights.total, 1.0)


if __name__ == "__main__":
    unittest.main()
