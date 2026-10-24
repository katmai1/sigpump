"""Tests del fichero de wallets: etiquetas, confianza, duplicadas y las de la lista negra."""

import logging
import tempfile
import unittest
from pathlib import Path

from sigpump.wallet_list import FollowedWallet, load_wallets
from tests.helpers import WALLET, OTRA


class TestLoadWallets(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "wallets.txt"

    def test_etiquetas_comentarios_y_lineas_invalidas(self):
        self.path.write_text(
            f"# mis wallets\n\n{WALLET}  # ballena 1\n{OTRA}\nesto-no-es-una-wallet\n",
            encoding="utf-8",
        )
        wallets = load_wallets(self.path)
        self.assertEqual(list(wallets), [WALLET, OTRA])
        self.assertEqual(wallets[WALLET], FollowedWallet("ballena 1"))
        # Sin etiqueta, la dirección abreviada.
        self.assertEqual(wallets[OTRA].label, "7Q54…e4j9")

    def test_duplicadas_se_eliminan_del_fichero(self):
        self.path.write_text(
            f"# mis wallets\n{WALLET}  # ballena 1\n{OTRA}\n{WALLET} # repetida\n{OTRA}\n",
            encoding="utf-8",
        )
        wallets = load_wallets(self.path)
        self.assertEqual(list(wallets), [WALLET, OTRA])
        # Se queda la primera aparición, con su etiqueta.
        self.assertEqual(wallets[WALLET].label, "ballena 1")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            f"# mis wallets\n{WALLET}  # ballena 1\n{OTRA}\n",
        )

    def test_sin_duplicadas_no_toca_el_fichero(self):
        self.path.write_text(f"{WALLET}\n{OTRA}\n", encoding="utf-8")
        mtime = self.path.stat().st_mtime_ns
        load_wallets(self.path)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_asterisco_marca_wallet_de_confianza(self):
        self.path.write_text(f"{WALLET} *  # ballena\n{OTRA}*\n", encoding="utf-8")
        wallets = load_wallets(self.path)
        self.assertEqual(wallets[WALLET], FollowedWallet("ballena", trusted=True))
        self.assertTrue(wallets[OTRA].trusted)

    def test_las_de_la_lista_negra_se_eliminan_del_fichero(self):
        self.path.write_text(f"{WALLET}  # ballena\n{OTRA}  # rug\n", encoding="utf-8")
        wallets = load_wallets(self.path, {OTRA})
        self.assertEqual(list(wallets), [WALLET])
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{WALLET}  # ballena\n")


if __name__ == "__main__":
    unittest.main()
