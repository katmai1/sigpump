"""
sigpump/radar.py

Orquesta el ciclo del radar: descubre candidatos, aplica los filtros duros,
los puntúa (score_pair), verifica contra GeckoTerminal los que superan el
umbral y dispara alertas de Telegram, respetando un cooldown por dirección.
"""

import time
import asyncio
import logging
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config, score_pair
from sigpump.screener import DexScreenerClient
from sigpump.signals import RECENT_HIGH_MINUTES, CandleStats, EarlySignal, candle_stats, txns_m5
from sigpump.solana import TokenAuthorities
from sigpump.telegram import TelegramAlerter
from sigpump.tracker import AlertTracker
from sigpump.util import normalize_address, to_float
from sigpump.wallets import WalletBuy, WalletSignal, WalletWatcher
from sigpump.watch import EarlyThresholds, PoolHistory

log = logging.getLogger()

# Velas de 1 minuto que se revisan buscando desplomes bruscos.
CANDLE_LOOKBACK_MINUTES = 60
# Tope de tokens a verificar contra GeckoTerminal por pasada: cada uno cuesta
# un request de velas y el tier gratuito corta en ~10 por minuto. Los que no
# entran se reintentan en la próxima pasada (no se les marca cooldown).
MAX_VERIFICATIONS_PER_PASS = 5
# Tope de prealertas por vuelta: si arrancan muchos a la vez suele ser el
# mercado entero moviéndose, no señales individuales.
MAX_EARLY_PER_PASS = 3
# Espera máxima a GeckoTerminal para verificar una prealerta. Si está ocupado
# (p. ej. esperando la ventana de un 429) la prealerta ya no llegaría a
# tiempo: se reintenta en la próxima vuelta si el arranque sigue.
EARLY_VERIFY_TIMEOUT_SECONDS = 20.0
# Medición (no filtra): si el arranque de una prealerta sigue cumpliéndose con
# los datos de ~30 y ~60 s después. Mide si exigirlo quitaría los mini pumps
# de 2-3 minutos que llegaban al final. (segundos, columna del registro).
SUSTAIN_CHECKS = ((30, "sostenido_30s"), (60, "sostenido_60s"))
# DexScreener refresca cada ~30 s: la foto "de los 30 s" puede llegar unos segundos antes.
SUSTAIN_CHECK_SLACK_SECONDS = 5
# Sin datos nuevos en este tiempo se abandona la medición (queda en NULL).
FOLLOWUP_MAX_SECONDS = 180
# Rasgos de velas de las señales que no las pidieron al avisar (prealertas y
# alertas omitidas): se completan después, mientras las velas de antes de la
# señal sigan dentro de lo que devuelve GeckoTerminal.
CANDLE_FEATURES_MAX_AGE_MINUTES = 45
# Tope de espera por esas velas: es solo medición y no debe frenar la vigilancia.
CANDLE_FEATURES_TIMEOUT_SECONDS = 10.0
# dexId de DexScreener de los pools que son bonding curves de un launchpad
# (el token todavía no se graduó a un AMM): las compras de wallets ahí se ignoran.
BONDING_CURVE_DEX_IDS = {"pumpfun", "meteoradbc", "launchlab", "moonshot"}


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


def _data_key(pair: dict) -> tuple:
    """Identifica un refresco de DexScreener: dos consultas dentro del mismo
    refresco (~30 s) devuelven exactamente estos valores."""
    return (
        pair.get("priceUsd"),
        (pair.get("volume") or {}).get("m5"),
        str((pair.get("txns") or {}).get("m5")),
    )


def _max_candle_drop_pct(candles: list[tuple[float, ...]]) -> float:
    """
    Mayor caída dentro de una vela de 1 minuto, en %. Por vela mide:
      - de la apertura (o el cierre anterior, si abrió más arriba) al mínimo:
        dumps que se ven como mecha aunque se recompren en segundos;
      - del máximo al cierre: subidas en vertical que se desploman en la
        misma vela.
    Una vela alcista normal (abre abajo y cierra arriba) da 0.
    """
    worst = 0.0
    prev_close = 0.0
    for candle in candles:
        # Las velas pueden traer el volumen como sexto campo.
        open_, high, low, close = candle[1:5]
        ref = max(open_, prev_close)
        if ref > 0:
            worst = max(worst, (ref - low) / ref * 100)
        if high > 0:
            worst = max(worst, (high - close) / high * 100)
        prev_close = close
    return worst


class MemecoinRadar:
    def __init__(self, config: Config):
        self._config = config
        # Recuerda cuándo se alertó cada dirección por última vez, para no
        # spamear el mismo token en cada pasada mientras siga por encima del umbral.
        self._alerted: dict[str, float] = {}  # address -> last alert timestamp
        self._tracker = AlertTracker(Path(config.alert_log_path)) if config.alert_log_path else None
        # Prealertas: cooldown propio, para que una prealerta no bloquee la
        # alerta completa que confirma el movimiento después.
        self._early_alerted: dict[str, float] = {}
        self._history = PoolHistory()
        # Tokens que consulta el bucle de vigilancia; los elige cada pasada completa.
        self._watchlist: list[str] = []
        # Boosteados de la última pasada, para puntuar las prealertas.
        self._boosted: set[str] = set()
        # Arranques detectados que esperan confirmación (require_sustained_seconds):
        # address -> {"ts", "key" (refresco en que se detectó)}.
        self._pending_early: dict[str, dict] = {}
        # Se crean en run(), con la sesión HTTP.
        self._authorities: TokenAuthorities | None = None
        self._wallet_watcher: WalletWatcher | None = None
        # Último aviso por (wallet, token), para no repetirlo si compra en varias tx.
        self._wallet_alerted: dict[tuple[str, str], float] = {}
        # Compras recientes por token (avisadas o no), para la confluencia.
        self._wallet_buys: dict[str, list[WalletBuy]] = {}
        # Prealertas cuyo arranque se está midiendo si se sostiene:
        # address -> {"row_id", "ts", "key" (último refresco visto), "done"}.
        self._followups: dict[str, dict] = {}
        self._early_thresholds = EarlyThresholds(
            min_price_move_pct=config.early_min_price_move_pct,
            max_price_move_pct=config.early_max_price_move_pct,
            min_volume_ratio=config.early_min_volume_ratio,
            min_txns_ratio=config.early_min_txns_ratio,
            min_buy_ratio=config.early_min_buy_ratio,
        )

    @staticmethod
    def _recent(registry: dict[str, float], address: str, minutes: float) -> bool:
        last = registry.get(address)
        return last is not None and (time.time() - last) / 60 < minutes

    def _cooldown_active(self, address: str) -> bool:
        """True si `address` fue alertado hace menos de alert_cooldown_minutes."""
        return self._recent(self._alerted, address, self._config.alert_cooldown_minutes)

    def _early_cooldown_active(self, address: str) -> bool:
        """True si `address` tuvo prealerta hace menos de [watch].cooldown_minutes."""
        return self._recent(self._early_alerted, address, self._config.early_cooldown_minutes)

    def _prealert_suppresses(self, address: str) -> bool:
        """True si `address` tuvo prealerta hace menos de
        [watch].suppress_alert_minutes: la alerta completa llegaría con la
        subida ya hecha (las que llegaron así cayeron todas a 15 min)."""
        minutes = self._config.early_suppress_alert_minutes
        return bool(minutes) and self._recent(self._early_alerted, address, minutes)

    def _early_memory_minutes(self) -> float:
        """Cuánto hay que recordar una prealerta: lo que dure su cooldown o
        el silencio de la alerta completa, lo que sea más largo."""
        return max(self._config.early_cooldown_minutes, self._config.early_suppress_alert_minutes)

    def _restore_cooldowns(self) -> None:
        """Recupera del registro las alertas y prealertas recientes. Sin esto
        un reinicio olvidaba los cooldowns y volvía a avisar de tokens ya
        avisados."""
        if not self._tracker:
            return
        now = time.time()
        self._alerted.update(
            self._tracker.last_sent("alerta", now - self._config.alert_cooldown_minutes * 60)
        )
        self._early_alerted.update(
            self._tracker.last_sent("prealerta", now - self._early_memory_minutes() * 60)
        )

    def _prune_alerted(self) -> None:
        """Elimina los cooldowns expirados y las fotos viejas, para que la
        memoria no crezca indefinidamente en ejecuciones largas."""
        now = time.time()
        for registry, minutes in (
            (self._alerted, self._config.alert_cooldown_minutes),
            (self._early_alerted, self._early_memory_minutes()),
        ):
            expired = [addr for addr, ts in registry.items() if ts < now - minutes * 60]
            for addr in expired:
                del registry[addr]
        wallet_cooldown = self._config.wallets_cooldown_minutes * 60
        for key in [k for k, ts in self._wallet_alerted.items() if ts < now - wallet_cooldown]:
            del self._wallet_alerted[key]
        confluence_since = now - self._config.wallets_confluence_minutes * 60
        self._wallet_buys = {
            mint: recent for mint, buys in self._wallet_buys.items()
            if (recent := [b for b in buys if b.ts >= confluence_since])
        }
        stale = [
            address for address, pending in self._pending_early.items()
            if now - pending["ts"] > FOLLOWUP_MAX_SECONDS
        ]
        for address in stale:
            del self._pending_early[address]
        self._history.prune(now)

    async def _discover_candidates(self, client: DexScreenerClient) -> tuple[list[str], set[str]]:
        """
        Arma la lista de direcciones candidatas a evaluar en esta pasada,
        combinando tokens con boost activo (pago), pools trending de
        GeckoTerminal, perfiles nuevos/actualizados, community takeovers y ads.
        Devuelve (direcciones recortadas a top_n_candidates, set de boosteadas)
        — el segundo valor se reutiliza luego en score_pair() para el bonus de boost.
        """
        chain = self._config.chain_id
        # Las fuentes se piden en paralelo porque son independientes entre sí.
        # return_exceptions: si una falla, se sigue con las demás en vez de
        # perder la pasada completa.
        names = ("latest_boosted", "top_boosted", "profiles", "takeovers", "ads", "trending")
        results = await asyncio.gather(
            client.get_latest_boosted(),
            client.get_top_boosted(),
            client.get_latest_profiles(),
            client.get_latest_takeovers(),
            client.get_latest_ads(),
            client.get_trending_tokens(chain, self._config.geckoterminal_pages),
            return_exceptions=True,
        )
        sources: list[list] = []
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                log.warning("Fuente %s falló: %s", name, result)
                sources.append([])
            else:
                sources.append(result)
        latest_boosted, top_boosted, profiles, takeovers, ads, trending = sources

        def _addresses(items: list[dict]) -> list[str]:
            """Direcciones de la chain configurada, en el orden que las devolvió
            la API (ese orden ya es el ranking de DexScreener)."""
            return [
                normalize_address(item["tokenAddress"])
                for item in items
                if item.get("chainId") == chain and item.get("tokenAddress")
            ]

        trending = [normalize_address(a) for a in trending]
        boosted_ordered = _addresses(latest_boosted) + _addresses(top_boosted)
        boosted_addresses = set(boosted_ordered)

        # boosted primero (señal de interés/marketing más fuerte), después
        # trending (actividad real de mercado) y al final el resto. Se trabaja
        # sobre listas y no sobre sets para que el recorte a top_n_candidates
        # sea determinista: con sets, el orden es arbitrario y cada pasada
        # descartaba tokens distintos sin criterio.
        candidates = list(
            dict.fromkeys(
                boosted_ordered
                + trending
                + _addresses(profiles)
                + _addresses(takeovers)
                + _addresses(ads)
            )
        )
        log.debug(
            "Candidatos: %d boosted, %d trending, %d únicos en total",
            len(boosted_addresses),
            len(trending),
            len(candidates),
        )
        return candidates[: self._config.top_n_candidates], boosted_addresses

    def _best_pair_per_token(self, pairs: list[dict], addresses: list[str]) -> list[dict]:
        """
        Un token suele cotizar en varios pools. Se queda con el par de mayor
        liquidez por token, que es el que mejor representa su mercado real;
        antes se evaluaba pool por pool y la alerta salía con los datos del
        primero que devolvía la API, no del más profundo.

        Además descarta los pares donde el token pedido es el quote (p. ej.
        SOL en un par SOL/TOKEN): ahí el baseToken es otro token y alertar
        sobre él sería alertar sobre el token equivocado.
        """
        wanted = set(addresses)
        best: dict[str, dict] = {}
        for pair in pairs:
            address = normalize_address((pair.get("baseToken") or {}).get("address") or "")
            if address not in wanted:
                continue
            current = best.get(address)
            if current is None or _liquidity_usd(pair) > _liquidity_usd(current):
                best[address] = pair
        return list(best.values())

    def _structural_rejection_reason(self, pair: dict) -> str | None:
        """
        Filtros duros que no dependen de la actividad del momento: liquidez,
        capitalización, edad y subida absurda. Deciden también qué tokens se
        vigilan, porque el que interesa vigilar es justo el que todavía está
        tranquilo y no pasaría los de actividad.
        """
        cfg = self._config
        if cfg.quote_tokens and not _matches_quote(pair, cfg.quote_tokens):
            quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
            return f"par contra {quote}, no contra {' o '.join(cfg.quote_tokens)}"
        liquidity_usd = _liquidity_usd(pair)
        # marketCap suele venir ausente en tokens nuevos (sin supply circulante
        # conocido); fdv (fully diluted valuation) es el fallback de DexScreener.
        market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
        # to_float: pairCreatedAt llega como epoch en ms, pero si la API lo
        # manda como string un cast directo tiraba TypeError y mataba la
        # pasada entera, no solo este par.
        pair_created_at = to_float(pair.get("pairCreatedAt"))
        change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))

        if liquidity_usd < cfg.min_liquidity_usd:
            return f"liquidez ${liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}"
        if market_cap_usd < cfg.min_market_cap_usd:
            return f"market cap ${market_cap_usd:,.0f} < ${cfg.min_market_cap_usd:,.0f}"
        # Pares recién creados son los más propensos a rug pulls; se
        # exige que tengan al menos min_pair_age_minutes de vida.
        # Si la API no informa pairCreatedAt no se puede evaluar la edad,
        # así que no se descarta por este filtro.
        if pair_created_at > 0:
            age_minutes = (time.time() - pair_created_at / 1000) / 60
            if age_minutes < cfg.min_pair_age_minutes:
                return f"par creado hace {age_minutes:.0f} min"
        # Subidas de miles de % en una hora en un par que ya tiene cierta edad
        # son casi siempre precio roto o manipulado, no momentum real.
        if cfg.max_price_change_h1_pct and change_h1 > cfg.max_price_change_h1_pct:
            return f"cambio 1h {change_h1:+,.0f}% > {cfg.max_price_change_h1_pct:,.0f}%"
        return None

    def _activity_rejection_reason(self, pair: dict) -> str | None:
        """Filtros duros sobre la actividad de la última hora: volumen y txns
        mínimos, wash trading y honeypots."""
        cfg = self._config
        volume_h1 = to_float((pair.get("volume") or {}).get("h1"))
        txns_h1 = (pair.get("txns") or {}).get("h1")
        if not isinstance(txns_h1, dict):
            txns_h1 = {}
        buys = to_float(txns_h1.get("buys"))
        sells = to_float(txns_h1.get("sells"))
        txns = buys + sells

        if volume_h1 < cfg.min_volume_h1_usd:
            return f"volumen 1h ${volume_h1:,.0f} < ${cfg.min_volume_h1_usd:,.0f}"
        if txns < cfg.min_txns_h1:
            return f"{txns:.0f} txns en 1h < {cfg.min_txns_h1}"
        # Sin actividad ahora mismo, el "arranque" son dos operaciones sueltas.
        if cfg.min_txns_m5 and txns_m5(pair) < cfg.min_txns_m5:
            return f"{txns_m5(pair):.0f} txns en 5m < {cfg.min_txns_m5:.0f}"
        volume_m5 = to_float((pair.get("volume") or {}).get("m5"))
        if cfg.min_volume_m5_usd and volume_m5 < cfg.min_volume_m5_usd:
            return f"volumen 5m ${volume_m5:,.0f} < ${cfg.min_volume_m5_usd:,.0f}"
        # Mucho volumen en pocas operaciones: wash trading, o un precio en USD
        # mal calculado que infla el volumen (y la liquidez y el market cap).
        # En memecoins reales el trade medio ronda los cientos de dólares.
        if cfg.max_avg_trade_usd and txns > 0 and volume_h1 / txns > cfg.max_avg_trade_usd:
            return f"trade medio ${volume_h1 / txns:,.0f} > ${cfg.max_avg_trade_usd:,.0f}"
        # Casi nadie vende: honeypot (no se puede vender) o pump coordinado.
        if txns > 0 and sells / txns < cfg.min_sell_ratio_h1:
            return f"solo {sells:.0f} ventas de {txns:.0f} txns en 1h"
        return None

    def _rejection_reason(self, pair: dict) -> str | None:
        """
        Filtros duros antes de puntuar, con los datos de DexScreener. Devuelve
        el motivo del descarte o None si el par pasa. Descartan pares
        ilíquidos, sin actividad real, de capitalización muy baja o con
        señales de manipulación, sin gastar cómputo de scoring en ellos.
        """
        return self._structural_rejection_reason(pair) or self._activity_rejection_reason(pair)

    def _late_reason(self, stats: CandleStats | None) -> str | None:
        """
        Motivo para descartar un par que pasó la verificación pero llega
        tarde: la subida ya se hizo o el precio ya cae desde el pico. None si
        no llega tarde o no hay velas con las que medirlo.
        """
        cfg = self._config
        if stats is None:
            return None
        if cfg.max_rise_from_low_pct and stats.rise_from_low_pct > cfg.max_rise_from_low_pct:
            return f"ya sube {stats.rise_from_low_pct:.0f}% sobre el mínimo de la última hora"
        if (
            cfg.max_drop_from_recent_high_pct
            and stats.drop_from_recent_high_pct > cfg.max_drop_from_recent_high_pct
        ):
            return (
                f"ya cae {stats.drop_from_recent_high_pct:.0f}% desde el máximo "
                f"de los últimos {RECENT_HIGH_MINUTES} min"
            )
        return None

    def _pool_reason(self, pair: dict, pools: dict[str, dict]) -> str | None:
        """
        Contrasta precio y liquidez del par con los datos de GeckoTerminal de
        su pool (de get_gecko_pools). Devuelve el motivo del descarte o None
        si pasa. Lo usan la verificación de alertas y la de prealertas.
        """
        cfg = self._config
        token_address = normalize_address((pair.get("baseToken") or {}).get("address") or "")
        pool = pools.get(normalize_address(str(pair.get("pairAddress") or "")))
        if pool is None:
            return "GeckoTerminal no tiene datos del pool"

        # DexScreener calcula el precio en USD a partir del precio del quote;
        # cuando ese cálculo está roto (quote poco líquido o manipulado) publica
        # precios miles de veces más altos, y con ellos volumen, liquidez y
        # cambios de precio absurdos que inflan el score.
        if cfg.max_price_deviation_pct:
            dex_price = to_float(pair.get("priceUsd"))
            gecko_price = pool["token_prices"].get(token_address, 0.0)
            if dex_price <= 0 or gecko_price <= 0:
                return "sin precio para comparar con GeckoTerminal"
            deviation = (max(dex_price, gecko_price) / min(dex_price, gecko_price) - 1) * 100
            if deviation > cfg.max_price_deviation_pct:
                return (
                    f"precio DexScreener ${dex_price:.6g} vs GeckoTerminal "
                    f"${gecko_price:.6g} (difieren {deviation:,.0f}%)"
                )

        if pool["reserve_usd"] < cfg.min_liquidity_usd:
            return (
                f"liquidez según GeckoTerminal ${pool['reserve_usd']:,.0f} "
                f"< ${cfg.min_liquidity_usd:,.0f}"
            )
        return None

    async def _verification_reason(
        self, client: DexScreenerClient, pair: dict, pools: dict[str, dict]
    ) -> tuple[str | None, CandleStats | None]:
        """
        Contrasta un par que está por alertar con GeckoTerminal. Devuelve
        (motivo del descarte o None si pasa, CandleStats si se pidieron
        velas). Sin datos para verificar también se descarta: es preferible
        perder una alerta a mandar un token manipulado (se reintenta en la
        próxima pasada).
        """
        cfg = self._config
        pair_address = str(pair.get("pairAddress") or "")
        token_address = normalize_address((pair.get("baseToken") or {}).get("address") or "")
        reason = self._pool_reason(pair, pools)
        if reason:
            return reason, None

        if not (
            cfg.max_candle_drop_pct
            or cfg.max_rise_from_low_pct
            or cfg.max_drop_from_recent_high_pct
        ):
            return None, None
        candles = await client.get_pool_candles(
            cfg.chain_id, pair_address, str(token_address), CANDLE_LOOKBACK_MINUTES
        )
        if not candles:
            return "sin velas de GeckoTerminal para revisar la volatilidad", None
        drop = _max_candle_drop_pct(candles)
        if cfg.max_candle_drop_pct and drop > cfg.max_candle_drop_pct:
            return f"caída de {drop:.0f}% dentro de una vela de 1 min", None
        return None, candle_stats(candles)

    async def _verify(
        self, client: DexScreenerClient, candidates: list[tuple[dict, float]]
    ) -> list[tuple[dict, float, CandleStats | None]]:
        """Devuelve los (par, score, CandleStats) de `candidates` que pasan la
        verificación contra GeckoTerminal y no llegan tarde, de mayor a menor
        score. Los que llegan tarde se registran en el tracker."""
        if not candidates:
            return []
        # Mayor score primero: si hay más de MAX_VERIFICATIONS_PER_PASS, los
        # que esperan a la próxima pasada son los más flojos.
        candidates = sorted(candidates, key=lambda c: c[1], reverse=True)
        if len(candidates) > MAX_VERIFICATIONS_PER_PASS:
            log.info(
                "%d candidatos quedan para verificar en la próxima pasada",
                len(candidates) - MAX_VERIFICATIONS_PER_PASS,
            )
            candidates = candidates[:MAX_VERIFICATIONS_PER_PASS]

        pool_addresses = [str(p["pairAddress"]) for p, _ in candidates if p.get("pairAddress")]
        pools = await client.get_gecko_pools(self._config.chain_id, pool_addresses)
        verified = []
        for pair, score in candidates:
            reason, stats = await self._verification_reason(client, pair, pools)
            if reason:
                log.info("Descartado %s (score=%s) al verificar: %s", _symbol(pair), score, reason)
                continue
            late = self._late_reason(stats)
            if late:
                log.info("Descartado %s (score=%s) por llegar tarde: %s", _symbol(pair), score, late)
                # Se registra para medir si este filtro tira señales buenas.
                address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
                if self._tracker and not self._tracker.is_tracking(address, sent=False):
                    self._tracker.record(pair, score, stats, sent=False, reason=late)
                continue
            verified.append((pair, score, stats))
        return verified

    async def _unsafe_reasons(self, pairs: list[dict]) -> dict[str, str]:
        """
        Motivo por token de los que se pueden acuñar o congelar. {} si la
        comprobación está apagada o el RPC no responde: es preferible dejar
        pasar una alerta a perderlas todas por un RPC caído.
        """
        if self._authorities is None:
            return {}
        addresses = [
            str(normalize_address((p.get("baseToken") or {}).get("address", ""))) for p in pairs
        ]
        return await self._authorities.unsafe_reasons(addresses)

    async def _drop_unsafe(
        self, candidates: list[tuple[dict, float]]
    ) -> list[tuple[dict, float]]:
        """Quita los candidatos cuyo token se puede acuñar o congelar, y los
        registra para poder revisar después qué se dejó fuera."""
        unsafe = await self._unsafe_reasons([p for p, _ in candidates])
        if not unsafe:
            return candidates
        safe = []
        for pair, score in candidates:
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
            reason = unsafe.get(str(address))
            if reason is None:
                safe.append((pair, score))
                continue
            log.info("Descartado %s (score=%s): %s", _symbol(pair), score, reason)
            if self._tracker and not self._tracker.is_tracking(address, sent=False):
                self._tracker.record(pair, score, None, sent=False, reason=reason)
        return safe

    def _score(self, pair: dict, boosted_addresses: set[str]) -> float:
        return score_pair(
            pair,
            self._config.weights,
            boosted_addresses,
            self._config.late_penalty_start_h1_pct,
            self._config.late_penalty_end_h1_pct,
        )

    def _update_watchlist(self, pairs: list[dict]) -> None:
        """
        Elige los tokens que consulta el bucle de vigilancia: los que pasan
        los filtros estructurales, de mayor a menor volumen 1h, hasta
        [watch].max_tokens. No se les exigen los de actividad: el token que
        interesa es el que todavía está tranquilo.
        """
        eligible = [p for p in pairs if self._structural_rejection_reason(p) is None]
        eligible.sort(key=lambda p: to_float((p.get("volume") or {}).get("h1")), reverse=True)
        self._watchlist = [
            normalize_address((p.get("baseToken") or {}).get("address", ""))
            for p in eligible[: self._config.watch_max_tokens]
        ]

    async def _watch_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """Vuelta del bucle de vigilancia: trae los datos de los tokens
        vigilados (un request a DexScreener cada 30) y busca arranques, sin
        esperar a la pasada completa. También muestrea el precio de las
        señales registradas y completa sus rasgos de velas."""
        if self._tracker:
            # Con la vigilancia activa el precio de las señales se muestrea
            # aquí (~30 s) y no en la pasada completa (2-3 min), que recortaba
            # mucho el mejor y el peor precio de los 30 minutos.
            try:
                await self._tracker.update(client, self._config.chain_id)
            except Exception:
                log.exception("Error actualizando el registro de alertas")
        addresses = [
            a for a in self._watchlist
            if not self._cooldown_active(a) and not self._early_cooldown_active(a)
        ]
        # Las prealertas recientes se siguen consultando aunque estén en
        # cooldown, para medir si su arranque se sostiene.
        addresses += [a for a in self._followups if a not in addresses]
        if addresses:
            all_pairs = await client.get_pairs_for_tokens(self._config.chain_id, addresses)
            await self._check_early(client, alerter, self._best_pair_per_token(all_pairs, addresses))
        try:
            await self._record_candle_features(client)
        except Exception:
            log.exception("Error registrando las velas de una señal")

    async def _check_early(
        self, client: DexScreenerClient, alerter: TelegramAlerter, pairs: list[dict]
    ) -> None:
        """
        Guarda la foto de cada par y manda prealerta a los que arrancan contra
        su propia historia. Pasan los mismos filtros duros que una alerta y,
        con verify_before_alert, el contraste de precio y liquidez con
        GeckoTerminal; no la revisión de velas, que cuesta un request por token
        y le quitaría a la prealerta el margen que la justifica.
        """
        now = time.time()
        signals: list[tuple[dict, EarlySignal]] = []
        for pair in pairs:
            self._history.observe(pair, now)
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
            if address in self._followups:
                self._check_followup(address, pair, now)
            if self._cooldown_active(address) or self._early_cooldown_active(address):
                continue
            if self._rejection_reason(pair):
                continue
            signal = self._history.early_signal(pair, now, self._early_thresholds)
            if self._confirmed(address, pair, signal, now):
                signals.append((pair, signal))
        if not signals:
            return
        signals.sort(key=lambda s: s[1].volume_ratio, reverse=True)
        signals = signals[:MAX_EARLY_PER_PASS]

        unsafe = await self._unsafe_reasons([p for p, _ in signals])
        if unsafe:
            for pair, _ in signals:
                reason = unsafe.get(str(normalize_address((pair.get("baseToken") or {}).get("address", ""))))
                if reason:
                    log.info("Prealerta de %s descartada: %s", _symbol(pair), reason)
            signals = [
                (p, s) for p, s in signals
                if str(normalize_address((p.get("baseToken") or {}).get("address", ""))) not in unsafe
            ]
            if not signals:
                return

        pools: dict[str, dict] = {}
        if self._config.verify_before_alert:
            pool_addresses = [str(p["pairAddress"]) for p, _ in signals if p.get("pairAddress")]
            try:
                pools = await asyncio.wait_for(
                    client.get_gecko_pools(self._config.chain_id, pool_addresses),
                    EARLY_VERIFY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                log.info("GeckoTerminal no respondió a tiempo para verificar prealertas")
                return

        for pair, signal in signals:
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
            if self._config.verify_before_alert:
                reason = self._pool_reason(pair, pools)
                if reason:
                    log.info("Prealerta de %s descartada al verificar: %s", _symbol(pair), reason)
                    continue
            # El escaneo y la vigilancia corren a la vez: durante la espera a
            # GeckoTerminal el otro bucle pudo haber avisado ya de este token.
            if self._cooldown_active(address) or self._early_cooldown_active(address):
                continue
            score = self._score(pair, self._boosted)
            log.info(
                "Prealerta: %s arranque %+.1f%% vol5m x%.1f txns5m x%.1f compras %.0f%%",
                _symbol(pair),
                signal.price_move_pct,
                signal.volume_ratio,
                signal.txns_ratio,
                signal.buy_ratio * 100,
            )
            # Se marca antes de enviar por lo mismo: el envío también cede el control.
            self._early_alerted[address] = time.time()
            try:
                await alerter.send(pair, score, early=signal)
            except Exception:
                log.exception("Error enviando prealerta para %s", address)
                self._early_alerted.pop(address, None)
                continue
            if self._tracker:
                row_id = self._tracker.record(
                    pair, score, None, sent=True, kind="prealerta", early=signal
                )
                if row_id is not None:
                    self._followups[address] = {
                        "row_id": row_id, "ts": now, "key": _data_key(pair), "done": set(),
                    }

    def _confirmed(self, address: str, pair: dict, signal: EarlySignal | None, now: float) -> bool:
        """
        True si hay que avisar del arranque. Con require_sustained_seconds > 0
        el primer arranque solo se anota: hace falta que siga cumpliéndose
        pasado ese tiempo y con datos nuevos de DexScreener. Los arranques que
        no se sostenían rendían bastante peor (-2,1% a 15 min frente a +4,3%).
        """
        wait = self._config.early_require_sustained_seconds
        if not wait:
            return signal is not None
        pending = self._pending_early.get(address)
        if signal is None:
            if pending is not None:
                log.debug("El arranque de %s no se sostuvo", _symbol(pair))
                del self._pending_early[address]
            return False
        if pending is None:
            self._pending_early[address] = {"ts": now, "key": _data_key(pair)}
            return False
        # Un refresco nuevo, no el mismo dato dos veces.
        if now - pending["ts"] < wait or _data_key(pair) == pending["key"]:
            return False
        del self._pending_early[address]
        return True

    def _check_followup(self, address: str, pair: dict, now: float) -> None:
        """
        Anota si el arranque de una prealerta sigue cumpliéndose ~30 y ~60 s
        después, con datos nuevos de DexScreener. Solo mide: sirve para decidir
        con datos si conviene exigir que el arranque se sostenga antes de avisar.
        """
        followup = self._followups[address]
        elapsed = now - followup["ts"]
        if elapsed > FOLLOWUP_MAX_SECONDS:
            del self._followups[address]
            return
        key = _data_key(pair)
        if key == followup["key"]:
            return  # mismo refresco de DexScreener: no hay nada nuevo que medir
        followup["key"] = key
        for seconds, column in SUSTAIN_CHECKS:
            if column in followup["done"]:
                continue
            if elapsed < seconds - SUSTAIN_CHECK_SLACK_SECONDS:
                break
            # Sin tope de subida: seguir subiendo es justo lo que se espera.
            sustained = self._history.early_signal(
                pair, now, self._early_thresholds, check_max_move=False
            ) is not None
            followup["done"].add(column)
            if self._tracker:
                self._tracker.update_row(followup["row_id"], {column: int(sustained)})
            break  # una comprobación por refresco nuevo
        if len(followup["done"]) == len(SUSTAIN_CHECKS):
            del self._followups[address]

    async def _record_candle_features(self, client: DexScreenerClient) -> None:
        """
        Completa en el registro los rasgos de velas (subida en 15 min, velas
        verdes seguidas, tendencia del volumen, mecha) de una señal que no los
        tiene. Una por vuelta y con tiempo máximo, para no acaparar
        GeckoTerminal. Solo mide: no filtra nada.
        """
        if not self._tracker:
            return
        row = self._tracker.next_without_candles(CANDLE_FEATURES_MAX_AGE_MINUTES * 60)
        if row is None:
            return
        # Suficientes velas para cubrir la hora anterior a la señal.
        elapsed_minutes = (time.time() - row["timestamp"]) / 60
        limit = CANDLE_LOOKBACK_MINUTES + int(elapsed_minutes) + 2
        try:
            candles = await asyncio.wait_for(
                client.get_pool_candles(self._config.chain_id, row["pool"], row["token"], limit),
                CANDLE_FEATURES_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return
        # Solo las velas ya cerradas al dar la señal: lo que se sabía entonces.
        stats = candle_stats(candles, until=row["timestamp"])
        if stats is not None:
            self._tracker.set_candle_stats(row["id"], stats)

    def _wallet_cooldown_active(self, buy: WalletBuy) -> bool:
        """True si ya se avisó de esta wallet en este token hace menos de
        [wallets].cooldown_minutes."""
        last = self._wallet_alerted.get((buy.wallet, buy.mint))
        return last is not None and (time.time() - last) / 60 < self._config.wallets_cooldown_minutes

    def _confluence(self, buy: WalletBuy) -> tuple[str, ...]:
        """Anota la compra y devuelve las etiquetas de las otras wallets
        seguidas que entraron en el mismo token dentro de confluence_minutes."""
        since = buy.ts - self._config.wallets_confluence_minutes * 60
        recent = [b for b in self._wallet_buys.get(buy.mint, []) if b.ts >= since]
        others = tuple(dict.fromkeys(b.label for b in recent if b.wallet != buy.wallet))
        self._wallet_buys[buy.mint] = recent + [buy]
        return others

    async def _wallets_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """
        Vuelta del seguimiento de wallets: busca sus compras nuevas y avisa
        de cada una, salvo tokens en bonding curve (sin pool en un AMM),
        tokens que se pueden acuñar o congelar, y compras repetidas de la
        misma wallet en el mismo token dentro de su cooldown.
        """
        if self._wallet_watcher is None:
            return
        buys = await self._wallet_watcher.poll()
        if not buys:
            return
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
            score = self._score(pair, self._boosted)
            reason = unsafe.get(buy.mint)
            if reason:
                log.info("Compra de %s en %s descartada: %s", buy.label, _symbol(pair), reason)
                self._record_wallet(pair, score, signal, sent=False, reason=reason)
                continue
            log.info(
                "Wallet: %s compró %s (%.2f SOL, %s)%s",
                buy.label,
                _symbol(pair),
                buy.sol_spent,
                "entrada nueva" if buy.new_position else "amplía",
                f" | también {', '.join(signal.others)}" if signal.others else "",
            )
            try:
                await alerter.send(pair, score, wallet=signal)
            except Exception:
                log.exception("Error enviando aviso de wallet para %s", buy.mint)
                self._wallet_alerted.pop(key, None)
                continue
            self._record_wallet(pair, score, signal, sent=True)

    def _record_wallet(
        self, pair: dict, score: float, signal: WalletSignal, sent: bool, reason: str = ""
    ) -> None:
        if not self._tracker:
            return
        row_id = self._tracker.record(pair, score, None, sent=sent, reason=reason, kind="wallet")
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

    async def _scan_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """Ejecuta una pasada completa: descubrir -> traer datos de mercado ->
        filtrar -> puntuar -> verificar -> alertar. Se invoca en loop desde run()."""
        self._prune_alerted()
        # Con la vigilancia activa el precio de las señales lo muestrea ella,
        # más seguido; hacerlo también aquí pisaría sus actualizaciones.
        if self._tracker and not self._config.watch_enabled:
            try:
                await self._tracker.update(client, self._config.chain_id)
            except Exception:
                # El registro es secundario: no debe costar las alertas de la pasada.
                log.exception("Error actualizando el registro de alertas")
        addresses, boosted_addresses = await self._discover_candidates(client)
        self._boosted = boosted_addresses
        if not addresses:
            log.info("Sin candidatos nuevos en esta pasada")
            return

        # Un solo llamado (paginado internamente) trae liquidez/volumen/precio
        # real de mercado para todos los candidatos descubiertos.
        all_pairs = await client.get_pairs_for_tokens(self._config.chain_id, addresses)
        pairs = self._best_pair_per_token(all_pairs, addresses)
        log.info(
            "Analizando %d tokens (%d pares) de %d candidatos",
            len(pairs),
            len(all_pairs),
            len(addresses),
        )
        if self._config.watch_enabled:
            self._update_watchlist(pairs)
            # Antes de verificar las alertas completas, que tarda: la
            # prealerta vale por llegar pronto.
            await self._check_early(client, alerter, pairs)

        to_alert: list[tuple[dict, float]] = []
        for pair in pairs:
            reason = self._rejection_reason(pair)
            if reason:
                log.debug("Descartado %s: %s", _symbol(pair), reason)
                continue

            score = self._score(pair, boosted_addresses)
            # _best_pair_per_token ya garantizó que address es una de las
            # direcciones pedidas, así que nunca es "".
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))

            if score < self._config.score_alert_threshold:
                continue
            if self._cooldown_active(address):
                continue
            if self._prealert_suppresses(address):
                minutes = (time.time() - self._early_alerted[address]) / 60
                reason = f"prealerta hace {minutes:.0f} min"
                # Se registra (una vez mientras siga por encima del umbral) para
                # comprobar con datos que omitirla fue acertado.
                if self._tracker and not self._tracker.is_tracking(address, sent=False):
                    log.info("Alerta de %s (score=%s) omitida: %s", _symbol(pair), score, reason)
                    self._tracker.record(pair, score, None, sent=False, reason=reason)
                else:
                    log.debug("Alerta de %s (score=%s) omitida: %s", _symbol(pair), score, reason)
                continue
            to_alert.append((pair, score))

        to_alert = await self._drop_unsafe(to_alert)

        # Sin verificación no se piden velas, así que no hay CandleStats.
        verified: list[tuple[dict, float, CandleStats | None]] = (
            await self._verify(client, to_alert)
            if self._config.verify_before_alert
            else [(pair, score, None) for pair, score in to_alert]
        )

        for pair, score, stats in verified:
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
            log.info(
                "Alerta: %s score=%s liq=$%.0f vol1h=$%.0f",
                _symbol(pair),
                score,
                _liquidity_usd(pair),
                to_float((pair.get("volume") or {}).get("h1")),
            )
            try:
                await alerter.send(pair, score, stats)
            except Exception:
                # Un fallo puntual de envío (p. ej. error de red o de Telegram)
                # no debe cortar la evaluación del resto de los candidatos.
                log.exception("Error enviando alerta para %s", address)
                continue
            self._alerted[address] = time.time()
            if self._tracker:
                self._tracker.record(pair, score, stats, sent=True)

    async def run(self) -> None:
        """Loop principal: crea la sesión HTTP y el bot de Telegram una sola
        vez, y repite _scan_once cada poll_interval_seconds indefinidamente."""
        if self._tracker:
            # Abre la base al arrancar (y no en la primera alerta) para que un
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
            if self._config.wallets_enabled:
                self._wallet_watcher = WalletWatcher(
                    session,
                    self._config.solana_rpc_url,
                    Path(self._config.wallets_file),
                    self._config.wallets_min_sol,
                )
            loops = [
                self._loop(
                    self._scan_once, client, alerter,
                    self._config.poll_interval_seconds, "Error durante el escaneo",
                )
            ]
            # La vigilancia corre en paralelo con su propio intervalo: esperar
            # a que termine la pasada completa (2-3 min) es justo lo que hace
            # llegar tarde.
            if self._config.watch_enabled:
                loops.append(
                    self._loop(
                        self._watch_once, client, alerter,
                        self._config.watch_interval_seconds, "Error durante la vigilancia",
                    )
                )
            if self._wallet_watcher is not None:
                loops.append(
                    self._loop(
                        self._wallets_once, client, alerter,
                        self._config.wallets_interval_seconds, "Error siguiendo las wallets",
                    )
                )
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
