"""
sigpump/radar.py

Orquesta el ciclo del radar: descubre candidatos, filtra por liquidez/volumen
mínimos, los puntúa (score_pair) y dispara alertas de Telegram para los que
superan el umbral configurado, respetando un cooldown por dirección.
"""

import time
import asyncio
import logging
import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config, score_pair
from sigpump.screener import DexScreenerClient
from sigpump.telegram import TelegramAlerter
from sigpump.util import to_float

log = logging.getLogger()


def _liquidity_usd(pair: dict) -> float:
    """Liquidez en USD del par, 0.0 si la API no la informa."""
    return to_float((pair.get("liquidity") or {}).get("usd"))


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
                item["tokenAddress"]
                for item in items
                if item.get("chainId") == chain and item.get("tokenAddress")
            ]

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
            address = (pair.get("baseToken") or {}).get("address") or ""
            if address not in wanted:
                continue
            current = best.get(address)
            if current is None or _liquidity_usd(pair) > _liquidity_usd(current):
                best[address] = pair
        return list(best.values())

    async def _scan_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """Ejecuta una pasada completa: descubrir -> traer datos de mercado ->
        filtrar -> puntuar -> alertar. Se invoca en loop desde run()."""
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

        for pair in pairs:
            liquidity_usd = _liquidity_usd(pair)
            volume_h1 = to_float((pair.get("volume") or {}).get("h1"))
            # marketCap suele venir ausente en tokens nuevos (sin supply circulante
            # conocido); fdv (fully diluted valuation) es el fallback de DexScreener.
            market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
            # to_float: pairCreatedAt llega como epoch en ms, pero si la API lo
            # manda como string un cast directo tiraba TypeError y mataba la
            # pasada entera, no solo este par.
            pair_created_at = to_float(pair.get("pairCreatedAt"))

            # Filtros duros antes de puntuar: descartan pares demasiado
            # ilíquidos, sin actividad real o de capitalización muy baja
            # (mayor riesgo de rug/manipulación), sin gastar cómputo de scoring en ellos.
            if liquidity_usd < self._config.min_liquidity_usd:
                continue
            if volume_h1 < self._config.min_volume_h1_usd:
                continue
            if market_cap_usd < self._config.min_market_cap_usd:
                continue
            # Pares recién creados son los más propensos a rug pulls; se
            # exige que tengan al menos min_pair_age_minutes de vida.
            # Si la API no informa pairCreatedAt no se puede evaluar la edad,
            # así que no se descarta por este filtro.
            if pair_created_at > 0:
                age_minutes = (time.time() - pair_created_at / 1000) / 60
                if age_minutes < self._config.min_pair_age_minutes:
                    continue

            score = score_pair(pair, self._config.weights, boosted_addresses)
            # _best_pair_per_token ya garantizó que address es una de las
            # direcciones pedidas, así que nunca es "".
            address = (pair.get("baseToken") or {}).get("address", "")

            if score < self._config.score_alert_threshold:
                continue
            if self._cooldown_active(address):
                continue

            log.info(
                "Alerta: %s score=%s liq=$%.0f vol1h=$%.0f",
                (pair.get("baseToken") or {}).get("symbol"),
                score,
                liquidity_usd,
                volume_h1,
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
