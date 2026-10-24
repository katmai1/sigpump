"""Tests de la tabla de filtros del par: qué se aplica en bonding curve y a
las wallets de confianza, y que la documentación diga lo mismo."""

import re
import unittest
from pathlib import Path

from sigpump.config import Config
from sigpump.filters import PAIR_FILTERS, pair_rejection

_CLAVES = {f.key for f in PAIR_FILTERS}
_CURVA = {f.key for f in PAIR_FILTERS if f.curve}
_SIN_CONFIANZA = {f.key for f in PAIR_FILTERS if not f.trusted}
_CON_CONFIANZA = _CLAVES - _SIN_CONFIANZA
# La documentación nombra estas dos con palabras y no por su clave.
_ALIAS = {"edad": "min_pair_age_minutes", "dex": "dex_ids"}


def _claves_entre(texto: str, inicio: str, fin: str) -> set[str]:
    """Claves de filtros nombradas entre `inicio` y `fin` en `texto`."""
    match = re.search(f"{inicio}(.*?){fin}", texto, re.S | re.I)
    assert match, f"no se encontró {inicio!r}"
    palabras = (_ALIAS.get(p, p) for p in re.findall(r"\w+", match.group(1)))
    return {palabra for palabra in palabras if palabra in _CLAVES}


class TestLaDocumentacionCoincideConLaTabla(unittest.TestCase):
    """README, config.example.toml y config.py describen estos subconjuntos a
    mano: min_volume_h1_usd se aplicaba a las de confianza sin que lo dijeran."""

    def test_readme(self):
        texto = Path("README.md").read_text(encoding="utf-8")
        self.assertEqual(_claves_entre(texto, "A esas solo se les aplican", r"\("), _CURVA)
        self.assertEqual(_claves_entre(texto, "a sus compras no se les aplican", r"\(sí"), _SIN_CONFIANZA)
        self.assertEqual(_claves_entre(texto, r"\(sí ", r"\)"), _CON_CONFIANZA)

    def test_config_de_ejemplo(self):
        texto = Path("config.example.toml").read_text(encoding="utf-8")
        self.assertEqual(_claves_entre(texto, "Solo se les aplican", "y no cuentan"), _CURVA)
        self.assertEqual(_claves_entre(texto, "a sus compras no se les aplican", r"\(sí"), _SIN_CONFIANZA)
        self.assertEqual(_claves_entre(texto, r"\(sí ", r"\)"), _CON_CONFIANZA)

    def test_comentario_de_config(self):
        texto = Path("sigpump/config.py").read_text(encoding="utf-8")
        self.assertEqual(_claves_entre(texto, "A esas solo se les aplican", ":"), _CURVA)


def _par_malo() -> dict:
    """Un par que incumple todos los filtros de _config_estricta()."""
    return {
        "dexId": "orca",
        "quoteToken": {"symbol": "USDC"},
        "pairCreatedAt": 1,  # con now=0 tiene edad negativa: siempre "recién creado"
        "priceChange": {"m5": 50, "h1": 500},
        "marketCap": 10_000,
        "txns": {"m5": {"buys": 1, "sells": 0}},
        "volume": {"h1": 4_000},  # < 5000 y un 40% del market cap
    }


def _config_estricta(**kwargs) -> Config:
    base = dict(
        alert_log_path="", dex_ids=["pumpswap"], quote_tokens=["SOL"],
        wallets_min_pair_age_minutes=60, wallets_max_price_change_h1_pct=50,
        wallets_max_price_change_m5_pct=20, wallets_min_market_cap_usd=100_000,
        wallets_min_txns_m5=5, wallets_min_volume_h1_usd=5_000, wallets_max_turnover_h1=0.1,
    )
    return Config(**{**base, **kwargs})


def _campo(key: str) -> str:
    return key if key in ("dex_ids", "quote_tokens") else f"wallets_{key}"


class TestPairRejection(unittest.TestCase):
    def test_el_motivo_es_el_del_primer_filtro_que_falla(self):
        self.assertIn("par en orca", pair_rejection(_config_estricta(), _par_malo(), now=0.0))
        self.assertIn("par contra USDC", pair_rejection(_config_estricta(), _par_malo(), now=0.0, curve=True))

    def test_cada_filtro_se_aplica_solo_donde_dice_la_tabla(self):
        estricta = _config_estricta()
        for f in PAIR_FILTERS:
            solo = Config(alert_log_path="", **{_campo(f.key): getattr(estricta, _campo(f.key))})
            for curve, trusted in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(filtro=f.key, curve=curve, trusted=trusted):
                    motivo = pair_rejection(solo, _par_malo(), now=0.0, curve=curve, trusted=trusted)
                    aplica = (f.curve or not curve) and (f.trusted or not trusted)
                    self.assertEqual(motivo is not None, aplica, motivo)

    def test_par_bueno_pasa(self):
        par = {
            "dexId": "pumpswap", "quoteToken": {"symbol": "SOL"}, "pairCreatedAt": 1,
            "priceChange": {"m5": 1, "h1": 5}, "marketCap": 1_000_000,
            "txns": {"m5": {"buys": 10, "sells": 5}}, "volume": {"h1": 10_000},
        }
        self.assertIsNone(pair_rejection(_config_estricta(), par))


if __name__ == "__main__":
    unittest.main()
