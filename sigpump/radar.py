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
        self._alerted: dict[str, float] = {}  # address -> last alert timestamp

    def _cooldown_active(self, address: str) -> bool:
        last = self._alerted.get(address)
        if last is None:
            return False
        elapsed_minutes = (time.time() - last) / 60
        return elapsed_minutes < self._config.alert_cooldown_minutes

    async def _discover_candidates(self, client: DexScreenerClient) -> tuple[list[str], set[str]]:
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

        candidates = list(boosted_addresses | profile_addresses)
        return candidates[: self._config.top_n_candidates], boosted_addresses

    async def _scan_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        addresses, boosted_addresses = await self._discover_candidates(client)
        if not addresses:
            log.info("Sin candidatos nuevos en esta pasada")
            return

        pairs = await client.get_pairs_for_tokens(self._config.chain_id, addresses)
        log.info("Analizando %d pares de %d candidatos", len(pairs), len(addresses))

        for pair in pairs:
            liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            volume_h1 = float((pair.get("volume") or {}).get("h1") or 0.0)

            if liquidity_usd < self._config.min_liquidity_usd:
                continue
            if volume_h1 < self._config.min_volume_h1_usd:
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
            await alerter.send(pair, score)
            self._alerted[address] = time.time()

    async def run(self) -> None:
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
                    log.exception("Error durante el escaneo")
                await asyncio.sleep(self._config.poll_interval_seconds)
