"""Tests de la memoria de compras recientes: cooldowns, confluencia,
actividad por wallet y recuperación desde el registro, sin radar ni red."""

import time
import unittest

from sigpump.config import Config
from sigpump.cooldowns import Cooldowns
from sigpump.wallets import WalletBuy


def _buy(wallet="W1", mint="TOK", ts=None, label=None):
    return WalletBuy(
        wallet=wallet, label=label or wallet, mint=mint, sol_spent=1.0, stable_spent=0.0,
        new_position=True, signature="SIG", ts=time.time() if ts is None else ts,
    )


def _cooldowns(**kwargs):
    return Cooldowns(Config(alert_log_path="", **kwargs))


def _sin_confianza(_buy):
    return False


class TestCooldowns(unittest.TestCase):
    def test_cooldown_de_wallet_en_token(self):
        c = _cooldowns(wallets_cooldown_minutes=60)
        buy = _buy()
        self.assertFalse(c.wallet_cooldown_active(buy))
        c.start_wallet_cooldown(buy)
        self.assertTrue(c.wallet_cooldown_active(buy))
        self.assertFalse(c.wallet_cooldown_active(_buy(mint="OTRO")))
        c.cancel_wallet_cooldown(buy)
        self.assertFalse(c.wallet_cooldown_active(buy))

    def test_confluencia_una_por_wallet_y_sin_la_propia(self):
        c = _cooldowns(wallets_confluence_minutes=60)
        self.assertEqual(c.confluence(_buy("W1"), _sin_confianza), ())
        c.confluence(_buy("W1"), _sin_confianza)
        self.assertEqual(c.confluence(_buy("W2"), _sin_confianza), ("W1",))
        self.assertEqual(c.confluence(_buy("W1"), _sin_confianza), ("W2",))

    def test_confluencia_marca_las_de_confianza(self):
        c = _cooldowns()
        c.confluence(_buy("W1", label="ballena"), _sin_confianza)
        others = c.confluence(_buy("W2"), lambda b: b.wallet == "W1")
        self.assertEqual(others, ("⭐ ballena",))

    def test_hiperactiva_no_cuenta_para_la_confluencia(self):
        c = _cooldowns(wallets_max_tokens_per_hour=2)
        for mint in ("A", "B", "C"):
            c.add_activity(_buy("BOT", mint=mint))
        self.assertTrue(c.hyperactive("BOT"))
        self.assertEqual(c.tokens_last_hour("BOT"), 3)
        c.confluence(_buy("BOT"), _sin_confianza)
        self.assertEqual(c.confluence(_buy("W2"), _sin_confianza), ())

    def test_aviso_por_token(self):
        c = _cooldowns(wallets_token_cooldown_minutes=60)
        self.assertIsNone(c.last_token_alert("TOK"))
        c.mark_token_alerted("TOK", 2)
        self.assertEqual(c.last_token_alert("TOK"), 2)
        # 0 = sin límite por token: nunca hay un aviso previo vigente.
        sin_limite = _cooldowns(wallets_token_cooldown_minutes=0)
        sin_limite.mark_token_alerted("TOK", 2)
        self.assertIsNone(sin_limite.last_token_alert("TOK"))

    def test_prune_olvida_lo_que_salio_de_su_ventana(self):
        c = _cooldowns(wallets_confluence_minutes=60, wallets_cooldown_minutes=60)
        viejo = time.time() - 2 * 3600
        c.confluence(_buy("W1", ts=viejo), _sin_confianza)
        c.add_activity(_buy("W1", ts=viejo))
        c.start_wallet_cooldown(_buy("W1"))
        c.prune(now=time.time() + 2 * 3600)
        self.assertEqual(c.confluence(_buy("W2"), _sin_confianza), ())
        self.assertEqual(c.tokens_last_hour("W1"), 0)
        self.assertFalse(c.wallet_cooldown_active(_buy("W1")))

    def test_restore_desde_el_registro(self):
        c = _cooldowns(
            wallets_confluence_minutes=60, wallets_cooldown_minutes=1440, wallets_token_cooldown_minutes=60,
        )
        now = time.time()
        filas = [
            # Hace 3 h: fuera de la confluencia y del aviso por token, dentro del cooldown.
            {"wallet": "W1", "wallet_etiqueta": "uno", "token": "TOK", "timestamp": now - 3 * 3600,
             "enviada": 1, "wallets_confluencia": 0},
            {"wallet": "W2", "wallet_etiqueta": None, "token": "TOK", "timestamp": now - 600,
             "enviada": 1, "wallets_confluencia": 1},
        ]
        self.assertEqual(c.restore_since(now), now - 1440 * 60)
        c.restore(filas, now)
        self.assertTrue(c.wallet_cooldown_active(_buy("W1")))
        self.assertEqual(c.confluence(_buy("W3"), _sin_confianza), ("W2",))
        self.assertEqual(c.last_token_alert("TOK"), 2)
        self.assertEqual(c.tokens_last_hour("W1"), 0)
        self.assertEqual(c.tokens_last_hour("W2"), 1)


if __name__ == "__main__":
    unittest.main()
