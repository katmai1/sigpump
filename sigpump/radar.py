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

log = logging.getLogger()


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
        combinando tokens con boost activo (pago) y perfiles nuevos/actualizados.
        Devuelve (direcciones recortadas a top_n_candidates, set de boosteadas)
        — el segundo valor se reutiliza luego en score_pair() para el bonus de boost.
        """
        # Las tres fuentes se piden en paralelo porque son independientes entre sí.
        latest_boosted, top_boosted, profiles = await asyncio.gather(
            client.get_latest_boosted(),
            client.get_top_boosted(),
            client.get_latest_profiles(),
        )

        chain = self._config.chain_id
        boosted_addresses = {
            item["tokenAddress"]
            for item in (latest_boosted + top_boosted)
            if item.get("chainId") == chain and item.get("tokenAddress")
        }
        profile_addresses = {
            item["tokenAddress"]
            for item in profiles
            if item.get("chainId") == chain and item.get("tokenAddress")
        }

        # boosted primero (señal de interés/marketing más fuerte), preservando
        # orden de llegada en vez de depender del orden arbitrario de un set.
        candidates = list(dict.fromkeys([*boosted_addresses, *profile_addresses]))
        return candidates[: self._config.top_n_candidates], boosted_addresses

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
        pairs = await client.get_pairs_for_tokens(self._config.chain_id, addresses)
        log.info("Analizando %d pares de %d candidatos", len(pairs), len(addresses))

        for pair in pairs:
            liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            volume_h1 = float((pair.get("volume") or {}).get("h1") or 0.0)
            # marketCap suele venir ausente en tokens nuevos (sin supply circulante
            # conocido); fdv (fully diluted valuation) es el fallback de DexScreener.
            market_cap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0.0)

            # Filtros duros antes de puntuar: descartan pares demasiado
            # ilíquidos, sin actividad real o de capitalización muy baja
            # (mayor riesgo de rug/manipulación), sin gastar cómputo de scoring en ellos.
            if liquidity_usd < self._config.min_liquidity_usd:
                continue
            if volume_h1 < self._config.min_volume_h1_usd:
                continue
            if market_cap_usd < self._config.min_market_cap_usd:
                continue

            score = score_pair(pair, self._config.weights, boosted_addresses)
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
        )
        async with aiohttp.ClientSession() as session:
            client = DexScreenerClient(session)
            while True:
                try:
                    await self._scan_once(client, alerter)
                except Exception:
                    # Errores inesperados (red, parsing, etc.) no deben matar
                    # el proceso: se loguean y se reintenta en la próxima pasada.
                    log.exception("Error durante el escaneo")
                await asyncio.sleep(self._config.poll_interval_seconds)
