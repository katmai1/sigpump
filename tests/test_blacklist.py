"""Tests de la lista negra en la base y del paso del fichero de versiones anteriores."""

import tempfile
import time
import unittest
from pathlib import Path

from sigpump.blacklist import import_blacklist_file
from tests.helpers import WALLET, OTRA, _abrir_negra, _motivos


class TestBlacklist(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.negra = _abrir_negra(self, self.dir / "alertas.db")
        self.addCleanup(self.negra.close)

    def test_vacia(self):
        self.assertEqual(self.negra.entries(), {})

    def test_permanente_y_temporal(self):
        hasta = time.time() + 3600
        self.negra.add(WALLET, "rug NGU -96%")
        self.negra.add(OTRA, "abandonada", until=hasta)
        self.assertEqual(self.negra.entries(), {WALLET: None, OTRA: hasta})
        self.assertEqual(self.negra.reason(WALLET), "rug NGU -96%")

    def test_las_temporales_vencidas_se_ignoran_y_se_limpian(self):
        self.negra.add(OTRA, "bot", until=time.time() - 1)
        self.assertEqual(self.negra.entries(), {})
        self.negra.add(WALLET, "rug")
        self.assertEqual(_motivos(self.negra), f"{WALLET}  # rug\n")

    def test_la_permanente_gana_a_la_temporal(self):
        self.negra.add(OTRA, "bot", until=time.time() + 3600)
        self.negra.add(OTRA, "rug")
        self.negra.add(OTRA, "abandonada", until=time.time() + 3600)
        self.assertEqual(self.negra.entries(), {OTRA: None})
        self.assertEqual(self.negra.reason(OTRA), "rug")

    def test_importa_el_fichero_de_antes(self):
        wallets_txt, fichero = self.dir / "wallets.txt", self.dir / "negra.txt"
        wallets_txt.write_text(f"{WALLET}  # ballena", encoding="utf-8")
        tercera = "8Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j8"
        fichero.write_text(
            f"{WALLET}  # ballena: rug NGU -96%\n"
            f"{OTRA}  # ab12…cd34: abandonada, 9 días sin actividad\n"
            f"{tercera}  # hasta 2000-01-01T00:00:00+00:00 · vencida\n",
            encoding="utf-8",
        )
        import_blacklist_file(self.negra, fichero, wallets_txt)
        self.assertFalse(fichero.exists())
        self.assertTrue(fichero.with_name("negra.txt.importado").exists())
        self.assertEqual(self.negra.reason(WALLET), "ballena: rug NGU -96%")
        self.assertIsNone(self.negra.reason(tercera))
        # La abandonada vuelve al fichero de wallets, para comprobarla ya.
        self.assertEqual(self.negra.entries(), {WALLET: None})
        self.assertIn("abandonada", self.negra.reason(OTRA))
        self.assertEqual(
            wallets_txt.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n{OTRA}  # ab12…cd34\n"
        )
        # Ya importado, no hace nada.
        import_blacklist_file(self.negra, fichero, wallets_txt)


if __name__ == "__main__":
    unittest.main()
