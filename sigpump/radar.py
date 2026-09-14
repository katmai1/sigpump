"""
sigpump/radar.py

Orquesta el ciclo del radar: descubre candidatos, aplica los filtros duros,
los puntúa (score_pair), verifica contra GeckoTerminal los que superan el
umbral y dispara alertas de Telegram, respetando un cooldown por dirección.
"""

import time
import asyncio
import logging
import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config, score_pair
from sigpump.screener import DexScreenerClient
from sigpump.telegram import TelegramAlerter
from sigpump.util import normalize_address, to_float

log = logging.getLogger()

# Velas de 1 minuto que se revisan buscando desplomes bruscos.
CANDLE_LOOKBACK_MINUTES = 60
# Tope de tokens a verificar contra GeckoTerminal por pasada: cada uno cuesta
# un request de velas y el tier gratuito corta en ~10 por minuto. Los que no
# entran se reintentan en la próxima pasada (no se les marca cooldown).
MAX_VERIFICATIONS_PER_PASS = 5


def _liquidity_usd(pair: dict) -> float:
    """Liquidez en USD del par, 0.0 si la API no la informa."""
    return to_float((pair.get("liquidity") or {}).get("usd"))


def _symbol(pair: dict) -> object:
    return (pair.get("baseToken") or {}).get("symbol")


def _max_candle_drop_pct(candles: list[tuple[float, float, float, float, float]]) -> float:
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
    for _, open_, high, low, close in candles:
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

    def _cooldown_active(self, address: str) -> bool:
        """True si `address` fue alertado hace menos de alert_cooldown_minutes."""
        last = self._alerted.get(address)
        if last is None:
            return False
        elapsed_minutes = (time.time() - last) / 60
        return elapsed_minutes < self._config.alert_cooldown_minutes

    def _prune_alerted(self) -> None:
        """Elimina de _alerted las direcciones cuyo cooldown ya expiró, para
        que el diccionario no crezca indefinidamente en ejecuciones largas."""
        cutoff = time.time() - self._config.alert_cooldown_minutes * 60
        expired = [addr for addr, ts in self._alerted.items() if ts < cutoff]
        for addr in expired:
            del self._alerted[addr]

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

    def _rejection_reason(self, pair: dict) -> str | None:
        """
        Filtros duros antes de puntuar, con los datos de DexScreener. Devuelve
        el motivo del descarte o None si el par pasa. Descartan pares
        ilíquidos, sin actividad real, de capitalización muy baja o con
        señales de manipulación, sin gastar cómputo de scoring en ellos.
        """
        cfg = self._config
        liquidity_usd = _liquidity_usd(pair)
        volume_h1 = to_float((pair.get("volume") or {}).get("h1"))
        # marketCap suele venir ausente en tokens nuevos (sin supply circulante
        # conocido); fdv (fully diluted valuation) es el fallback de DexScreener.
        market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
        # to_float: pairCreatedAt llega como epoch en ms, pero si la API lo
        # manda como string un cast directo tiraba TypeError y mataba la
        # pasada entera, no solo este par.
        pair_created_at = to_float(pair.get("pairCreatedAt"))
        change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))
        txns_h1 = (pair.get("txns") or {}).get("h1")
        if not isinstance(txns_h1, dict):
            txns_h1 = {}
        buys = to_float(txns_h1.get("buys"))
        sells = to_float(txns_h1.get("sells"))
        txns = buys + sells

        if liquidity_usd < cfg.min_liquidity_usd:
            return f"liquidez ${liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}"
        if volume_h1 < cfg.min_volume_h1_usd:
            return f"volumen 1h ${volume_h1:,.0f} < ${cfg.min_volume_h1_usd:,.0f}"
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
        if txns < cfg.min_txns_h1:
            return f"{txns:.0f} txns en 1h < {cfg.min_txns_h1}"
        # Mucho volumen en pocas operaciones: wash trading, o un precio en USD
        # mal calculado que infla el volumen (y la liquidez y el market cap).
        # En memecoins reales el trade medio ronda los cientos de dólares.
        if cfg.max_avg_trade_usd and txns > 0 and volume_h1 / txns > cfg.max_avg_trade_usd:
            return f"trade medio ${volume_h1 / txns:,.0f} > ${cfg.max_avg_trade_usd:,.0f}"
        # Subidas de miles de % en una hora en un par que ya tiene cierta edad
        # son casi siempre precio roto o manipulado, no momentum real.
        if cfg.max_price_change_h1_pct and change_h1 > cfg.max_price_change_h1_pct:
            return f"cambio 1h {change_h1:+,.0f}% > {cfg.max_price_change_h1_pct:,.0f}%"
        # Casi nadie vende: honeypot (no se puede vender) o pump coordinado.
        if txns > 0 and sells / txns < cfg.min_sell_ratio_h1:
            return f"solo {sells:.0f} ventas de {txns:.0f} txns en 1h"
        return None

    async def _verification_reason(
        self, client: DexScreenerClient, pair: dict, pools: dict[str, dict]
    ) -> str | None:
        """
        Contrasta un par que está por alertar con GeckoTerminal. Devuelve el
        motivo del descarte o None si pasa. Sin datos para verificar también
        se descarta: es preferible perder una alerta a mandar un token
        manipulado (se reintenta en la próxima pasada).
        """
        cfg = self._config
        pair_address = str(pair.get("pairAddress") or "")
        token_address = normalize_address((pair.get("baseToken") or {}).get("address") or "")
        pool = pools.get(normalize_address(pair_address))
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

        if cfg.max_candle_drop_pct:
            candles = await client.get_pool_candles(
                cfg.chain_id, pair_address, str(token_address), CANDLE_LOOKBACK_MINUTES
            )
            if not candles:
                return "sin velas de GeckoTerminal para revisar la volatilidad"
            drop = _max_candle_drop_pct(candles)
            if drop > cfg.max_candle_drop_pct:
                return f"caída de {drop:.0f}% dentro de una vela de 1 min"
        return None

    async def _verify(
        self, client: DexScreenerClient, candidates: list[tuple[dict, float]]
    ) -> list[tuple[dict, float]]:
        """Devuelve los (par, score) de `candidates` que pasan la verificación
        contra GeckoTerminal, de mayor a menor score."""
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
            reason = await self._verification_reason(client, pair, pools)
            if reason:
                log.info("Descartado %s (score=%s) al verificar: %s", _symbol(pair), score, reason)
                continue
            verified.append((pair, score))
        return verified

    async def _scan_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """Ejecuta una pasada completa: descubrir -> traer datos de mercado ->
        filtrar -> puntuar -> verificar -> alertar. Se invoca en loop desde run()."""
        self._prune_alerted()
        addresses, boosted_addresses = await self._discover_candidates(client)
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

        to_alert: list[tuple[dict, float]] = []
        for pair in pairs:
            reason = self._rejection_reason(pair)
            if reason:
                log.debug("Descartado %s: %s", _symbol(pair), reason)
                continue

            score = score_pair(pair, self._config.weights, boosted_addresses)
            # _best_pair_per_token ya garantizó que address es una de las
            # direcciones pedidas, así que nunca es "".
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))

            if score < self._config.score_alert_threshold:
                continue
            if self._cooldown_active(address):
                continue
            to_alert.append((pair, score))

        if self._config.verify_before_alert:
            to_alert = await self._verify(client, to_alert)

        for pair, score in to_alert:
            address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
            log.info(
                "Alerta: %s score=%s liq=$%.0f vol1h=$%.0f",
                _symbol(pair),
                score,
                _liquidity_usd(pair),
                to_float((pair.get("volume") or {}).get("h1")),
            )
            try:
                await alerter.send(pair, score)
            except Exception:
                # Un fallo puntual de envío (p. ej. error de red o de Telegram)
                # no debe cortar la evaluación del resto de los candidatos.
                log.exception("Error enviando alerta para %s", address)
                continue
            self._alerted[address] = time.time()

    async def run(self) -> None:
        """Loop principal: crea la sesión HTTP y el bot de Telegram una sola
        vez, y repite _scan_once cada poll_interval_seconds indefinidamente."""
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
            while True:
                try:
                    await self._scan_once(client, alerter)
                except Exception:
                    # Errores inesperados (red, parsing, etc.) no deben matar
                    # el proceso: se loguean y se reintenta en la próxima pasada.
                    log.exception("Error durante el escaneo")
                await asyncio.sleep(self._config.poll_interval_seconds)
