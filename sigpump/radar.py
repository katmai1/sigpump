"""
sigpump/radar.py

Orquesta el seguimiento de wallets: busca las compras nuevas de las wallets
seguidas, trae de DexScreener el par del token comprado, aplica los filtros
y avisa por Telegram, registrando cada compra para medir su resultado.
"""

import time
import asyncio
import dataclasses
import logging
import statistics
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config
from sigpump.screener import DexScreenerClient
from sigpump.solana import TokenAuthorities
from sigpump.telegram import TelegramAlerter
from sigpump.tracker import AlertTracker
from sigpump.util import normalize_address, to_float
from sigpump.wallets import WalletBuy, WalletSignal, WalletWatcher, ws_url

log = logging.getLogger()

# dexId de DexScreener de los pools que son bonding curves de un launchpad
# (el token todavía no se graduó a un AMM): las compras de wallets ahí se ignoran.
BONDING_CURVE_DEX_IDS = {"pumpfun", "meteoradbc", "launchlab", "moonshot"}
# Ventana del tope [wallets].max_tokens_per_hour.
WALLET_ACTIVITY_SECONDS = 3600


def _liquidity_usd(pair: dict) -> float:
    """Liquidez en USD del par, 0.0 si la API no la informa."""
    return to_float((pair.get("liquidity") or {}).get("usd"))


def _symbol(pair: dict) -> object:
    return (pair.get("baseToken") or {}).get("symbol")


def _matches_quote(pair: dict, wanted: list[str]) -> bool:
    """True si el par cotiza contra una de las monedas pedidas, comparando por
    símbolo o por dirección del quote."""
    quote = pair.get("quoteToken") or {}
    symbol = str(quote.get("symbol") or "").upper()
    address = str(normalize_address(quote.get("address") or ""))
    return any(str(w).upper() == symbol or str(normalize_address(w)) == address for w in wanted)


class MemecoinRadar:
    def __init__(self, config: Config):
        self._config = config
        self._tracker = AlertTracker(Path(config.alert_log_path)) if config.alert_log_path else None
        # Se crean en run(), con la sesión HTTP.
        self._authorities: TokenAuthorities | None = None
        self._wallet_watcher: WalletWatcher | None = None
        # Último aviso por (wallet, token), para no repetirlo si compra en varias tx.
        self._wallet_alerted: dict[tuple[str, str], float] = {}
        # Compras recientes por token (avisadas o no), para la confluencia.
        self._wallet_buys: dict[str, list[WalletBuy]] = {}
        # Tokens comprados por cada wallet en la última hora (wallet -> token
        # -> última compra), para el tope [wallets].max_tokens_per_hour.
        self._wallet_activity: dict[str, dict[str, float]] = {}
        # Último aviso enviado por token y cuántas wallets llevaba, para no
        # repetirlo dentro de [wallets].token_cooldown_minutes.
        self._wallet_token_alerted: dict[str, tuple[float, int]] = {}

    def _restore_cooldowns(self) -> None:
        """Recupera del registro las compras recientes de wallets. Sin esto un
        reinicio olvidaba los cooldowns y la confluencia."""
        if not self._tracker:
            return
        now = time.time()
        # Sin las compras recientes, un reinicio entre la compra de una wallet
        # y la de otra perdía la confluencia (y con min_wallets > 1, el aviso).
        # Lo mismo con la actividad de cada wallet y los tokens ya avisados.
        confluence_since = now - self._config.wallets_confluence_minutes * 60
        token_since = now - self._config.wallets_token_cooldown_minutes * 60
        activity_since = now - WALLET_ACTIVITY_SECONDS
        for row in self._tracker.recent_wallet_buys(min(confluence_since, token_since, activity_since)):
            if row["timestamp"] > confluence_since:
                buy = WalletBuy(
                    wallet=row["wallet"], label=row["wallet_etiqueta"] or row["wallet"],
                    mint=row["token"], sol_spent=0.0, stable_spent=0.0, new_position=False,
                    signature="", ts=row["timestamp"],
                )
                self._wallet_buys.setdefault(buy.mint, []).append(buy)
            if row["timestamp"] > activity_since:
                self._wallet_activity.setdefault(row["wallet"], {})[row["token"]] = row["timestamp"]
            if row["enviada"] and row["timestamp"] > token_since:
                wallets = 1 + (row["wallets_confluencia"] or 0)
                self._wallet_token_alerted[row["token"]] = (row["timestamp"], wallets)

    def _prune_alerted(self) -> None:
        """Elimina los cooldowns expirados, para que la memoria no crezca
        indefinidamente en ejecuciones largas."""
        now = time.time()
        wallet_cooldown = self._config.wallets_cooldown_minutes * 60
        for key in [k for k, ts in self._wallet_alerted.items() if ts < now - wallet_cooldown]:
            del self._wallet_alerted[key]
        confluence_since = now - self._config.wallets_confluence_minutes * 60
        self._wallet_buys = {
            mint: recent for mint, buys in self._wallet_buys.items()
            if (recent := [b for b in buys if b.ts >= confluence_since])
        }
        activity_since = now - WALLET_ACTIVITY_SECONDS
        self._wallet_activity = {
            wallet: recent for wallet, mints in self._wallet_activity.items()
            if (recent := {m: ts for m, ts in mints.items() if ts >= activity_since})
        }
        token_since = now - self._config.wallets_token_cooldown_minutes * 60
        self._wallet_token_alerted = {
            mint: last for mint, last in self._wallet_token_alerted.items() if last[0] >= token_since
        }

    def _best_pair_per_token(self, pairs: list[dict], addresses: list[str]) -> list[dict]:
        """
        Un token suele cotizar en varios pools. Se queda con el par de mayor
        liquidez por token, que es el que mejor representa su mercado real.

        Además descarta los pares donde el token pedido es el quote (p. ej.
        SOL en un par SOL/TOKEN): ahí el baseToken es otro token y avisar
        de él sería avisar del token equivocado.

        Prefiere los pares que cumplen [dexscreener].dex_ids y quote_tokens:
        un token con su pool más profundo en otro dex o contra USDC se evalúa
        con su pool contra SOL en pumpswap, si lo tiene. Si no tiene ninguno
        se queda el de más liquidez, que luego se descarta por eso.
        """
        def rank(pair: dict) -> tuple[bool, float]:
            return self._venue_reason(pair) is None, _liquidity_usd(pair)

        wanted = set(addresses)
        best: dict[str, dict] = {}
        for pair in pairs:
            address = normalize_address((pair.get("baseToken") or {}).get("address") or "")
            if address not in wanted:
                continue
            current = best.get(address)
            if current is None or rank(pair) > rank(current):
                best[address] = pair
        return list(best.values())

    def _venue_reason(self, pair: dict) -> str | None:
        """Motivo para descartar un par por dónde cotiza ([dexscreener].dex_ids
        y quote_tokens), o None si pasa."""
        cfg = self._config
        if cfg.dex_ids:
            dex_id = str(pair.get("dexId") or "")
            if dex_id.lower() not in {d.lower() for d in cfg.dex_ids}:
                return f"par en {dex_id or '?'}, no en {' o '.join(cfg.dex_ids)}"
        if cfg.quote_tokens and not _matches_quote(pair, cfg.quote_tokens):
            quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
            return f"par contra {quote}, no contra {' o '.join(cfg.quote_tokens)}"
        return None

    def _venue_reason(self, pair: dict) -> str | None:
        """Motivo para descartar un par por dónde cotiza ([dexscreener].dex_ids
        y quote_tokens), o None si pasa."""
        cfg = self._config
        if cfg.dex_ids:
            dex_id = str(pair.get("dexId") or "")
            if dex_id.lower() not in {d.lower() for d in cfg.dex_ids}:
                return f"par en {dex_id or '?'}, no en {' o '.join(cfg.dex_ids)}"
        if cfg.quote_tokens and not _matches_quote(pair, cfg.quote_tokens):
            quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
            return f"par contra {quote}, no contra {' o '.join(cfg.quote_tokens)}"
        return None

    async def _unsafe_reasons(self, pairs: list[dict]) -> dict[str, str]:
        """
        Motivo por token de los que se pueden acuñar o congelar. {} si la
        comprobación está apagada o el RPC no responde: es preferible dejar
        pasar un aviso a perderlos todos por un RPC caído.
        """
        if self._authorities is None:
            return {}
        addresses = [
            str(normalize_address((p.get("baseToken") or {}).get("address", ""))) for p in pairs
        ]
        return await self._authorities.unsafe_reasons(addresses)

    def _wallet_cooldown_active(self, buy: WalletBuy) -> bool:
        """True si ya se avisó de esta wallet en este token hace menos de
        [wallets].cooldown_minutes."""
        last = self._wallet_alerted.get((buy.wallet, buy.mint))
        return last is not None and (time.time() - last) / 60 < self._config.wallets_cooldown_minutes

    def _tokens_last_hour(self, wallet: str) -> int:
        """Tokens distintos que compró `wallet` en la última hora."""
        since = time.time() - WALLET_ACTIVITY_SECONDS
        return sum(ts >= since for ts in self._wallet_activity.get(wallet, {}).values())

    def _hyperactive(self, wallet: str) -> bool:
        """True si `wallet` pasó de [wallets].max_tokens_per_hour: compra de
        todo (un bot o un degen) y sus compras no dicen nada."""
        limit = self._config.wallets_max_tokens_per_hour
        return bool(limit) and self._tokens_last_hour(wallet) > limit

    def _last_token_alert(self, mint: str) -> int | None:
        """Wallets que llevaba el último aviso de `mint`, si fue hace menos
        de [wallets].token_cooldown_minutes; None si no lo hubo."""
        last = self._wallet_token_alerted.get(mint)
        if last is None or (time.time() - last[0]) / 60 >= self._config.wallets_token_cooldown_minutes:
            return None
        return last[1]

    def _blacklist_ruggers(self) -> None:
        """Mete en la lista negra las wallets seguidas que compraron un token
        que luego hizo rug (cayó [wallets].rug_drop_pct en el seguimiento)."""
        watcher = self._wallet_watcher
        drop = self._config.wallets_rug_drop_pct
        if not (self._tracker and watcher and drop and self._config.wallets_blacklist_file):
            return
        for wallet, reason in self._tracker.rug_wallets(drop).items():
            info = watcher.wallets.get(wallet)
            if info is None:
                continue
            try:
                watcher.blacklist(wallet, f"{info.label}: {reason}")
            except OSError as exc:
                log.warning("No se pudo añadir %s a la lista negra: %s", info.label, exc)
                continue
            log.warning("Wallet %s a la lista negra: %s", info.label, reason)

    def _blacklist_losers(self) -> None:
        """Mete en la lista negra las wallets seguidas cuyas compras, con al
        menos [wallets].loser_min_signals tokens medidos, tienen una mediana a
        30 min de loser_max_median_ret_pct o peor: copiarlas hace perder."""
        watcher = self._wallet_watcher
        cfg = self._config
        if not (self._tracker and watcher and cfg.wallets_loser_min_signals and cfg.wallets_blacklist_file):
            return
        for wallet, returns in self._tracker.wallet_returns().items():
            info = watcher.wallets.get(wallet)
            if info is None or len(returns) < cfg.wallets_loser_min_signals:
                continue
            median = statistics.median(returns)
            if median > cfg.wallets_loser_max_median_ret_pct:
                continue
            reason = f"mediana {median:+.0f}% a 30 min en {len(returns)} tokens"
            try:
                watcher.blacklist(wallet, f"{info.label}: {reason}")
            except OSError as exc:
                log.warning("No se pudo añadir %s a la lista negra: %s", info.label, exc)
                continue
            log.warning("Wallet %s a la lista negra: %s", info.label, reason)

    def _wallet_pair_rejection(self, pair: dict) -> str | None:
        """Motivo para no avisar de una compra de wallet por cómo está el par
        ([dexscreener].dex_ids y quote_tokens; [wallets].min_pair_age_minutes,
        max_price_change_h1_pct y min_market_cap_usd), o None si pasa."""
        cfg = self._config
        venue_reason = self._venue_reason(pair)
        if venue_reason:
            return venue_reason
        created_at = to_float(pair.get("pairCreatedAt"))
        # Sin pairCreatedAt no se puede evaluar la edad: no se descarta por ella.
        if cfg.wallets_min_pair_age_minutes and created_at > 0:
            age_minutes = (time.time() - created_at / 1000) / 60
            if age_minutes < cfg.wallets_min_pair_age_minutes:
                return f"par creado hace {age_minutes:.0f} min"
        change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))
        if cfg.wallets_max_price_change_h1_pct and change_h1 > cfg.wallets_max_price_change_h1_pct:
            return f"cambio 1h {change_h1:+,.0f}% > {cfg.wallets_max_price_change_h1_pct:,.0f}%"
        market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
        if market_cap_usd < cfg.wallets_min_market_cap_usd:
            return f"market cap ${market_cap_usd:,.0f} < ${cfg.wallets_min_market_cap_usd:,.0f}"
        return None

    def _blacklist_hyperactive(self, buy: WalletBuy) -> None:
        """Mete en la lista negra la wallet de `buy` si pasó de
        [wallets].max_tokens_per_hour: compra de todo y no sirve copiarla."""
        watcher = self._wallet_watcher
        if not (watcher and self._config.wallets_blacklist_file and self._hyperactive(buy.wallet)):
            return
        if buy.wallet not in watcher.wallets:
            return
        reason = f"hiperactiva, {self._tokens_last_hour(buy.wallet)} tokens en 1h"
        try:
            watcher.blacklist(buy.wallet, f"{buy.label}: {reason}")
        except OSError as exc:
            log.warning("No se pudo añadir %s a la lista negra: %s", buy.label, exc)
            return
        log.warning("Wallet %s a la lista negra: %s", buy.label, reason)

    def _confluence(self, buy: WalletBuy) -> tuple[str, ...]:
        """Anota la compra y devuelve las etiquetas de las otras wallets
        seguidas que entraron en el mismo token dentro de confluence_minutes.
        Las hiperactivas no cuentan."""
        since = buy.ts - self._config.wallets_confluence_minutes * 60
        recent = [b for b in self._wallet_buys.get(buy.mint, []) if b.ts >= since]
        # Una por wallet: dos wallets con la misma etiqueta cuentan como dos.
        others = tuple({
            b.wallet: b.label for b in recent
            if b.wallet != buy.wallet and not self._hyperactive(b.wallet)
        }.values())
        self._wallet_buys[buy.mint] = recent + [buy]
        return others

    async def _wallets_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """
        Vuelta del seguimiento de wallets: busca sus compras nuevas y avisa
        de cada una, salvo tokens en bonding curve (sin pool en un AMM),
        tokens que se pueden acuñar o congelar, compras repetidas de la
        misma wallet en el mismo token dentro de su cooldown, wallets
        hiperactivas, tokens ya avisados sin wallets nuevas suficientes y,
        con [wallets].min_wallets > 1, compras sin suficientes wallets en el
        token (salvo de wallets de confianza).
        """
        if self._wallet_watcher is None:
            return
        self._prune_alerted()
        # Muestrea el precio de las compras registradas para medir su resultado.
        if self._tracker:
            try:
                await self._tracker.update(client, self._config.chain_id)
            except Exception:
                log.exception("Error actualizando el registro de alertas")
        self._blacklist_ruggers()
        self._blacklist_losers()
        buys = await self._wallet_watcher.poll()
        if not buys:
            return
        for buy in buys:
            self._wallet_activity.setdefault(buy.wallet, {})[buy.mint] = buy.ts
        for buy in buys:
            self._blacklist_hyperactive(buy)
        signals = [
            WalletSignal(buy, self._confluence(buy)) for buy in buys
            if not self._wallet_cooldown_active(buy)
        ]
        if not signals:
            return

        mints = list(dict.fromkeys(s.buy.mint for s in signals))
        all_pairs = await client.get_pairs_for_tokens(self._config.chain_id, mints)
        amm_pairs = [p for p in all_pairs if p.get("dexId") not in BONDING_CURVE_DEX_IDS]
        pairs = {
            normalize_address((p.get("baseToken") or {}).get("address", "")): p
            for p in self._best_pair_per_token(amm_pairs, mints)
        }
        unsafe = await self._unsafe_reasons(list(pairs.values()))

        for signal in signals:
            buy = signal.buy
            pair = pairs.get(buy.mint)
            if pair is None:
                log.info("Compra de %s en %s ignorada: sin pool fuera de bonding curve", buy.label, buy.mint)
                continue
            # Una wallet que compra en varias tx seguidas da una señal por tx
            # en la misma vuelta: solo cuenta la primera.
            if self._wallet_cooldown_active(buy):
                continue
            key = (buy.wallet, buy.mint)
            # Se marca también al descartar, para no registrar una fila por
            # cada compra repetida del mismo token peligroso.
            self._wallet_alerted[key] = time.time()
            reason = unsafe.get(buy.mint) or self._wallet_pair_rejection(pair)
            wallets = 1 + len(signal.others)
            if not reason and self._hyperactive(buy.wallet):
                reason = f"wallet hiperactiva ({self._tokens_last_hour(buy.wallet)} tokens en 1h)"
            if not reason and wallets < self._config.wallets_min_wallets and not buy.trusted:
                reason = f"{wallets} de {self._config.wallets_min_wallets} wallets"
            previous = self._last_token_alert(buy.mint)
            if not reason and previous is not None and (
                wallets < previous + self._config.wallets_realert_new_wallets
            ):
                reason = f"token ya avisado con {previous} wallets"
            if reason:
                log.info("Compra de %s en %s descartada: %s", buy.label, _symbol(pair), reason)
                self._record_wallet(pair, signal, sent=False, reason=reason)
                continue
            if previous is not None:
                signal = dataclasses.replace(signal, update=True)
            log.info(
                "Wallet%s: %s compró %s (%.2f SOL, %s)%s",
                " (actualización)" if signal.update else "",
                buy.label,
                _symbol(pair),
                buy.sol_spent,
                "entrada nueva" if buy.new_position else "amplía",
                f" | también {', '.join(signal.others)}" if signal.others else "",
            )
            try:
                await alerter.send(pair, signal)
            except Exception:
                log.exception("Error enviando aviso de wallet para %s", buy.mint)
                self._wallet_alerted.pop(key, None)
                continue
            self._wallet_token_alerted[buy.mint] = (time.time(), wallets)
            self._record_wallet(pair, signal, sent=True)

    def _record_wallet(
        self, pair: dict, signal: WalletSignal, sent: bool, reason: str = ""
    ) -> None:
        if not self._tracker:
            return
        row_id = self._tracker.record(pair, sent=sent, reason=reason)
        if row_id is None:
            return
        buy = signal.buy
        self._tracker.update_row(row_id, {
            "wallet": buy.wallet,
            "wallet_etiqueta": buy.label,
            "sol_gastado": buy.sol_spent,
            "stable_gastado": buy.stable_spent,
            "entrada_nueva": int(buy.new_position),
            "wallets_confluencia": len(signal.others),
            "tx": buy.signature,
        })

    async def run(self) -> None:
        """Loop principal: crea la sesión HTTP y el bot de Telegram una sola
        vez y sigue las wallets indefinidamente: una vuelta cada
        [wallets].interval_seconds y, con websocket, los avisos del RPC."""
        if self._tracker:
            # Abre la base al arrancar (y no en el primer aviso) para que un
            # problema con el archivo se vea enseguida en el log.
            self._tracker.open()
            self._restore_cooldowns()
        alerter = TelegramAlerter(
            self._config.telegram_bot_token,
            self._config.telegram_chat_id,
            self._config.telegram_message_thread_id,
            self._config.chain_id,
        )
        # `async with alerter` valida el token al arrancar y cierra el cliente
        # HTTP de Telegram al salir.
        async with aiohttp.ClientSession() as session, alerter:
            client = DexScreenerClient(session)
            if self._config.check_token_authorities:
                self._authorities = TokenAuthorities(session, self._config.solana_rpc_url)
            self._wallet_watcher = WalletWatcher(
                session,
                self._config.solana_rpc_url,
                Path(self._config.wallets_file),
                self._config.wallets_min_sol,
                Path(self._config.wallets_blacklist_file) if self._config.wallets_blacklist_file else None,
                self._config.wallets_full_poll_minutes * 60,
                self._config.wallets_max_txs_per_hour,
            )
            loops = [
                self._loop(
                    self._wallets_once, client, alerter,
                    self._config.wallets_interval_seconds, "Error siguiendo las wallets",
                )
            ]
            if self._config.wallets_websocket:
                loops.append(self._wallet_watcher.listen(
                    self._config.wallets_ws_url or ws_url(self._config.solana_rpc_url)
                ))
            await asyncio.gather(*loops)

    @staticmethod
    async def _loop(step, client, alerter, interval: float, error_message: str) -> None:
        """Repite `step(client, alerter)` cada `interval` segundos. Errores
        inesperados (red, parsing, etc.) no deben matar el proceso: se loguean
        y se reintenta en la próxima vuelta."""
        while True:
            try:
                await step(client, alerter)
            except Exception:
                log.exception(error_message)
            await asyncio.sleep(interval)
