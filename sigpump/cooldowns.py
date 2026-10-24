"""
sigpump/cooldowns.py

Lo que el radar recuerda de las compras recientes para decidir si avisa de
una nueva: el cooldown de cada wallet en cada token, la confluencia entre
wallets, los tokens que compró cada wallet en la última hora (tope
[wallets].max_tokens_per_hour) y el último aviso de cada token. Todo se
recupera del registro al reiniciar.
"""

import time
from typing import Callable

from sigpump.config import Config
from sigpump.wallets import WalletBuy

# Ventana del tope [wallets].max_tokens_per_hour.
WALLET_ACTIVITY_SECONDS = 3600


class Cooldowns:
    def __init__(self, config: Config):
        self._config = config
        # Último aviso por (wallet, token), para no repetirlo si compra en varias tx.
        self._wallet_alerted: dict[tuple[str, str], float] = {}
        # Compras recientes por token (avisadas o no), para la confluencia.
        self._buys: dict[str, list[WalletBuy]] = {}
        # Tokens comprados por cada wallet en la última hora (wallet -> token
        # -> última compra), para el tope [wallets].max_tokens_per_hour.
        self._activity: dict[str, dict[str, float]] = {}
        # Último aviso enviado por token y cuántas wallets llevaba, para no
        # repetirlo dentro de [wallets].token_cooldown_minutes.
        self._token_alerted: dict[str, tuple[float, int]] = {}

    def _windows(self, now: float) -> tuple[float, float, float, float]:
        """Desde cuándo cuenta cada cosa: confluencia, aviso por token,
        actividad de la wallet y cooldown de la wallet en el token."""
        cfg = self._config
        return (
            now - cfg.wallets_confluence_minutes * 60,
            now - cfg.wallets_token_cooldown_minutes * 60,
            now - WALLET_ACTIVITY_SECONDS,
            now - cfg.wallets_cooldown_minutes * 60,
        )

    def restore_since(self, now: float) -> float:
        """Desde cuándo hay que leer el registro para restore()."""
        return min(self._windows(now))

    def restore(self, rows: list[dict], now: float) -> None:
        """Recupera las compras de wallets del registro (de la más vieja a la
        más nueva, como las da AlertTracker.recent_wallet_buys)."""
        # Sin las compras recientes, un reinicio entre la compra de una wallet
        # y la de otra perdía la confluencia (y con min_wallets > 1, el aviso).
        # Lo mismo con la actividad de cada wallet, los tokens ya avisados y
        # el cooldown de cada wallet en cada token: con uno de 24 h, cada
        # reinicio volvía a avisar de las recompras de una wallet que
        # promedia en el mismo token.
        confluence_since, token_since, activity_since, wallet_since = self._windows(now)
        for row in rows:
            if row["timestamp"] > confluence_since:
                buy = WalletBuy(
                    wallet=row["wallet"], label=row["wallet_etiqueta"] or row["wallet"],
                    mint=row["token"], sol_spent=0.0, stable_spent=0.0, new_position=False,
                    signature="", ts=row["timestamp"],
                )
                self._buys.setdefault(buy.mint, []).append(buy)
            if row["timestamp"] > activity_since:
                self._activity.setdefault(row["wallet"], {})[row["token"]] = row["timestamp"]
            # Solo se registran las compras fuera del cooldown, así que la
            # última de cada (wallet, token) es la que lo abrió.
            if row["timestamp"] > wallet_since:
                self._wallet_alerted[(row["wallet"], row["token"])] = row["timestamp"]
            if row["enviada"] and row["timestamp"] > token_since:
                wallets = 1 + (row["wallets_confluencia"] or 0)
                self._token_alerted[row["token"]] = (row["timestamp"], wallets)

    def prune(self, now: float | None = None) -> None:
        """Olvida lo que ya salió de su ventana, para que la memoria no crezca
        indefinidamente en ejecuciones largas."""
        now = time.time() if now is None else now
        confluence_since, token_since, activity_since, wallet_since = self._windows(now)
        self._wallet_alerted = {k: ts for k, ts in self._wallet_alerted.items() if ts >= wallet_since}
        self._buys = {
            mint: recent for mint, buys in self._buys.items()
            if (recent := [b for b in buys if b.ts >= confluence_since])
        }
        self._activity = {
            wallet: recent for wallet, mints in self._activity.items()
            if (recent := {m: ts for m, ts in mints.items() if ts >= activity_since})
        }
        self._token_alerted = {
            mint: last for mint, last in self._token_alerted.items() if last[0] >= token_since
        }

    # --- cooldown de cada wallet en cada token ---

    def wallet_cooldown_active(self, buy: WalletBuy) -> bool:
        """True si ya se avisó de esta wallet en este token hace menos de
        [wallets].cooldown_minutes."""
        last = self._wallet_alerted.get((buy.wallet, buy.mint))
        return last is not None and (time.time() - last) / 60 < self._config.wallets_cooldown_minutes

    def start_wallet_cooldown(self, buy: WalletBuy) -> None:
        self._wallet_alerted[(buy.wallet, buy.mint)] = time.time()

    def cancel_wallet_cooldown(self, buy: WalletBuy) -> None:
        self._wallet_alerted.pop((buy.wallet, buy.mint), None)

    # --- actividad de cada wallet ---

    def add_activity(self, buy: WalletBuy) -> None:
        self._activity.setdefault(buy.wallet, {})[buy.mint] = buy.ts

    def tokens_last_hour(self, wallet: str) -> int:
        """Tokens distintos que compró `wallet` en la última hora."""
        since = time.time() - WALLET_ACTIVITY_SECONDS
        return sum(ts >= since for ts in self._activity.get(wallet, {}).values())

    def hyperactive(self, wallet: str) -> bool:
        """True si `wallet` pasó de [wallets].max_tokens_per_hour: compra de
        todo (un bot o un degen) y sus compras no dicen nada."""
        limit = self._config.wallets_max_tokens_per_hour
        return bool(limit) and self.tokens_last_hour(wallet) > limit

    # --- confluencia ---

    def confluence(self, buy: WalletBuy, trusted: Callable[[WalletBuy], bool]) -> tuple[str, ...]:
        """Anota la compra y devuelve las etiquetas de las otras wallets
        seguidas que entraron en el mismo token dentro de confluence_minutes.
        Las hiperactivas no cuentan; las de confianza (según `trusted`)
        llevan ⭐."""
        since = buy.ts - self._config.wallets_confluence_minutes * 60
        recent = [b for b in self._buys.get(buy.mint, []) if b.ts >= since]
        # Una por wallet: dos wallets con la misma etiqueta cuentan como dos.
        others = tuple({
            b.wallet: f"⭐ {b.label}" if trusted(b) else b.label for b in recent
            if b.wallet != buy.wallet and not self.hyperactive(b.wallet)
        }.values())
        self._buys[buy.mint] = recent + [buy]
        return others

    # --- último aviso de cada token ---

    def last_token_alert(self, mint: str) -> int | None:
        """Wallets que llevaba el último aviso de `mint`, si fue hace menos
        de [wallets].token_cooldown_minutes; None si no lo hubo."""
        last = self._token_alerted.get(mint)
        if last is None or (time.time() - last[0]) / 60 >= self._config.wallets_token_cooldown_minutes:
            return None
        return last[1]

    def mark_token_alerted(self, mint: str, wallets: int) -> None:
        self._token_alerted[mint] = (time.time(), wallets)
