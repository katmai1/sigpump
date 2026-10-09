"""Tests de la configuración: validación de rangos y tipos, y lectura del TOML."""

import logging
import tempfile
import unittest
from pathlib import Path

from sigpump.config import Config


class TestConfigValidation(unittest.TestCase):
    def test_chain_vacia_da_error(self):
        with self.assertRaises(ValueError):
            Config(chain_id="")

    def test_tipo_invalido_da_error_de_config(self):
        """`interval_seconds = "15"` tiraba un TypeError crudo que run.py no
        captura, en vez de un error de configuración."""
        casos = {
            "wallets_interval_seconds": "15",
            "wallets_min_wallets": 1.5,
            "wallets_max_tokens_per_hour": True,
            "verbose": "si",
            "telegram_message_thread_id": "123",
            "alert_log_path": None,
            "check_token_authorities": "si",
        }
        for campo, valor in casos.items():
            with self.subTest(campo=campo), self.assertRaises(ValueError):
                Config(**{campo: valor})

    def test_float_en_campos_numericos_es_valido(self):
        Config(wallets_interval_seconds=7.5, wallets_cooldown_minutes=0.5)

    def test_filtros_de_par_fuera_de_rango_dan_error(self):
        casos = [
            dict(quote_tokens="SOL"),          # tiene que ser una lista
            dict(quote_tokens=["SOL", ""]),    # ni textos vacíos
            dict(quote_tokens=["SOL", 3]),
            dict(dex_ids="pumpswap"),
            dict(dex_ids=["pumpswap", ""]),
            dict(solana_rpc_url=""),
        ]
        for caso in casos:
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                Config(**caso)

    def test_wallets_fuera_de_rango_dan_error(self):
        casos = [
            dict(wallets_enabled=False),       # sin wallets no hay nada que avisar
            dict(wallets_enabled="si"),
            dict(wallets_file=""),
            dict(wallets_interval_seconds=0),
            dict(wallets_full_poll_minutes=0),
            dict(wallets_min_sol=-1),
            dict(wallets_min_wallets=0),
            dict(wallets_min_wallets=1.5),
            dict(wallets_realert_new_wallets=0),
            dict(wallets_max_tokens_per_hour=-1),
            dict(wallets_max_txs_per_hour=-1),
            dict(wallets_rug_drop_pct=150),
            dict(wallets_loser_min_signals=-1),
            dict(wallets_token_cooldown_minutes=-1),
            dict(wallets_min_pair_age_minutes=-1),
            dict(wallets_max_price_change_h1_pct=-1),
            dict(wallets_min_market_cap_usd=-1),
        ]
        for caso in casos:
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                Config(**caso)


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
        self.assertTrue(config.wallets_enabled)

    def test_secciones_parciales(self):
        config = Config.from_toml(
            self._write('[telegram]\nbot_token = "t"\nchat_id = "c"\n[radar]\nverbose = true\n')
        )
        self.assertEqual(config.telegram_bot_token, "t")
        self.assertTrue(config.verbose)
        self.assertIsNone(config.telegram_message_thread_id)
        self.assertEqual(config.wallets_interval_seconds, 15)

    def test_clave_desconocida_avisa(self):
        # Un typo se ignoraba en silencio y se usaba el default.
        with self.assertLogs(level=logging.WARNING) as logs:
            config = Config.from_toml(self._write("[wallets]\nmin_sool = 1\n"))
        self.assertIn("min_sool", "".join(logs.output))
        self.assertEqual(config.wallets_min_sol, Config().wallets_min_sol)

    def test_seccion_desconocida_avisa(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            Config.from_toml(self._write("[telegrm]\nbot_token = 't'\n"))
        self.assertIn("telegrm", "".join(logs.output))

    def test_secciones_del_radar_viejo_avisan_sin_romper(self):
        """Un config.toml de antes de quitar el radar de trending sigue
        arrancando: lo que ya no existe solo se avisa en el log."""
        with self.assertLogs(level=logging.WARNING) as logs:
            config = Config.from_toml(
                self._write(
                    "[radar]\nalerts_enabled = false\nalert_log_path = 'x.db'\n"
                    "[dexscreener]\nmin_liquidity_usd = 5000\n[watch]\nenabled = false\n"
                )
            )
        salida = "".join(logs.output)
        for clave in ("alerts_enabled", "min_liquidity_usd", "watch"):
            self.assertIn(clave, salida)
        self.assertEqual(config.alert_log_path, "x.db")

    def test_seccion_que_no_es_tabla_da_error(self):
        with self.assertRaises(ValueError):
            Config.from_toml(self._write("radar = 5\n"))

    def test_lee_los_filtros_de_par_y_la_seccion_solana(self):
        config = Config.from_toml(
            self._write(
                '[dexscreener]\nquote_tokens = ["SOL", "USDC"]\n'
                'dex_ids = ["pumpswap", "raydium"]\n'
                '[solana]\ncheck_token_authorities = false\nrpc_url = "https://rpc.ejemplo"\n'
                '[radar]\nalert_log_path = ""\n'
            )
        )
        self.assertEqual(config.quote_tokens, ["SOL", "USDC"])
        self.assertEqual(config.dex_ids, ["pumpswap", "raydium"])
        self.assertFalse(config.check_token_authorities)
        self.assertEqual(config.solana_rpc_url, "https://rpc.ejemplo")
        self.assertEqual(config.alert_log_path, "")

    def test_lee_las_wallets(self):
        config = Config.from_toml(
            self._write(
                '[wallets]\nfile = "w.txt"\ninterval_seconds = 30\nmin_sol = 0.5\n'
                "min_wallets = 2\nmax_txs_per_hour = 100\nwebsocket = false\n"
                "min_market_cap_usd = 50000\n"
            )
        )
        self.assertEqual(config.wallets_file, "w.txt")
        self.assertEqual(config.wallets_interval_seconds, 30)
        self.assertEqual(config.wallets_min_sol, 0.5)
        self.assertEqual(config.wallets_min_wallets, 2)
        self.assertEqual(config.wallets_max_txs_per_hour, 100)
        self.assertFalse(config.wallets_websocket)
        self.assertEqual(config.wallets_min_market_cap_usd, 50_000)

    def test_clave_desconocida_en_solana_avisa(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            Config.from_toml(self._write("[solana]\nrcp_url = 'x'\n"))
        self.assertIn("rcp_url", "".join(logs.output))

    def test_chat_id_numerico_se_acepta_como_str(self):
        config = Config.from_toml(self._write("[telegram]\nchat_id = -100123\n"))
        self.assertEqual(config.telegram_chat_id, "-100123")

    def test_ejemplo_del_repo_es_valido_y_sin_claves_desconocidas(self):
        # config.example.toml es lo que copia el usuario: tiene que parsear limpio.
        with self.assertNoLogs(level=logging.WARNING):
            config = Config.from_toml(Path("config.example.toml"))
        self.assertEqual(config.chain_id, "solana")


if __name__ == "__main__":
    unittest.main()
