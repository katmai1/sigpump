"""
memecoin_radar.py

Radar de solo-lectura para memecoins trending en DexScreener.
No ejecuta trades: descubre candidatos, los puntúa y avisa por Telegram
cuando cruzan el umbral configurado.

Fuentes de datos (API pública de DexScreener, sin API key):
  - GET /token-boosts/latest/v1   -> tokens con promoción paga reciente
  - GET /token-boosts/top/v1      -> tokens con más boosts activos
  - GET /token-profiles/latest/v1 -> perfiles de token nuevos/actualizados
  - GET /latest/dex/tokens/{addrs}-> datos de mercado (volumen, liquidez,
                                      cambios de precio) para hasta 30
                                      direcciones por llamada

DexScreener no expone públicamente el ranking exacto de su página
"trending" (ese cálculo es interno). Este radar arma su propia señal
combinando boosts + perfiles recientes como candidatos, y los puntúa
con métricas de mercado reales (volumen, momentum de precio, liquidez).

Requisitos:
  pip install aiohttp python-telegram-bot

Uso:
  python memecoin_radar.py --config config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import tomllib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode

API_BASE = "https://api.dexscreener.com"
MAX_ADDRESSES_PER_CALL = 30

log = logging.getLogger("memecoin_radar")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class ScoringWeights:
    volume_h1: float = 0.35
    price_change_h1: float = 0.25
    price_change_h6: float = 0.15
    liquidity: float = 0.15
    boosted: float = 0.10


@dataclass
class Config:
    chain_id: str = "solana"
    poll_interval_seconds: int = 90
    alert_cooldown_minutes: int = 60
    min_liquidity_usd: float = 5_000.0
    min_volume_h1_usd: float = 2_000.0
    score_alert_threshold: float = 70.0
    top_n_candidates: int = 40
    verbose: bool = False
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int | None = None

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        radar = raw.get("radar", {})
        dexscreener = raw.get("dexscreener", {})
        telegram = raw.get("telegram", {})
        weights_raw = raw.get("scoring_weights", {})

        return cls(
            chain_id=dexscreener.get("chain_id", "solana"),
            poll_interval_seconds=radar.get("poll_interval_seconds", 90),
            alert_cooldown_minutes=radar.get("alert_cooldown_minutes", 60),
            min_liquidity_usd=dexscreener.get("min_liquidity_usd", 5_000.0),
            min_volume_h1_usd=dexscreener.get("min_volume_h1_usd", 2_000.0),
            score_alert_threshold=radar.get("score_alert_threshold", 70.0),
            top_n_candidates=radar.get("top_n_candidates", 40),
            verbose=radar.get("verbose", False),
            weights=ScoringWeights(**weights_raw) if weights_raw else ScoringWeights(),
            telegram_bot_token=telegram.get("bot_token", ""),
            telegram_chat_id=telegram.get("chat_id", ""),
            telegram_message_thread_id=telegram.get("message_thread_id"),
        )


# --------------------------------------------------------------------------
# DexScreener client
# --------------------------------------------------------------------------

class DexScreenerClient:
    """Thin async client for the public DexScreener REST API."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, path: str) -> Any:
        url = f"{API_BASE}{path}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                log.warning("DexScreener rate limit hit on %s", path)
                return None
            resp.raise_for_status()
            return await resp.json()

    async def get_latest_boosted(self) -> list[dict]:
        data = await self._get("/token-boosts/latest/v1")
        return data or []

    async def get_top_boosted(self) -> list[dict]:
        data = await self._get("/token-boosts/top/v1")
        return data or []

    async def get_latest_profiles(self) -> list[dict]:
        data = await self._get("/token-profiles/latest/v1")
        return data or []

    async def get_pairs_for_tokens(self, chain_id: str, addresses: list[str]) -> list[dict]:
        """Fetch full pair/market data for up to 30 token addresses at once."""
        pairs: list[dict] = []
        for i in range(0, len(addresses), MAX_ADDRESSES_PER_CALL):
            batch = addresses[i : i + MAX_ADDRESSES_PER_CALL]
            joined = ",".join(batch)
            data = await self._get(f"/latest/dex/tokens/{joined}")
            if not data:
                continue
            # Endpoint returns either {"pairs": [...]} or a bare list depending on version
            batch_pairs = data.get("pairs") if isinstance(data, dict) else data
            for pair in batch_pairs or []:
                if pair.get("chainId") == chain_id:
                    pairs.append(pair)
        return pairs


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def score_pair(pair: dict, weights: ScoringWeights, boosted_addresses: set[str]) -> float:
    """
    Puntaje 0-100 combinando:
      - volumen 1h (normalizado log-scale contra un techo razonable)
      - momentum de precio 1h y 6h
      - liquidez (más liquidez = menos riesgo de rug/slippage)
      - si el token tiene boost activo (señal de marketing/interés)
    """
    volume_h1 = float((pair.get("volume") or {}).get("h1") or 0.0)
    price_change_h1 = float((pair.get("priceChange") or {}).get("h1") or 0.0)
    price_change_h6 = float((pair.get("priceChange") or {}).get("h6") or 0.0)
    liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
    token_address = (pair.get("baseToken") or {}).get("address", "")
    is_boosted = token_address in boosted_addresses

    # Normalizaciones simples, ajustar techos según lo que observes en la práctica
    volume_score = _clamp((volume_h1 / 50_000.0) * 100)
    price_h1_score = _clamp(50 + price_change_h1)  # +50% h1 -> tope
    price_h6_score = _clamp(50 + price_change_h6 / 2)
    liquidity_score = _clamp((liquidity_usd / 100_000.0) * 100)
    boost_score = 100.0 if is_boosted else 0.0

    total = (
        volume_score * weights.volume_h1
        + price_h1_score * weights.price_change_h1
        + price_h6_score * weights.price_change_h6
        + liquidity_score * weights.liquidity
        + boost_score * weights.boosted
    )
    return round(_clamp(total), 1)


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str, message_thread_id: int | None = None):
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id

    async def send(self, pair: dict, score: float) -> None:
        base = pair.get("baseToken", {})
        symbol = base.get("symbol", "?")
        name = base.get("name", "?")
        address = base.get("address", "?")
        price_usd = pair.get("priceUsd", "?")
        liquidity_usd = (pair.get("liquidity") or {}).get("usd", 0)
        volume_h1 = (pair.get("volume") or {}).get("h1", 0)
        change_h1 = (pair.get("priceChange") or {}).get("h1", 0)
        url = pair.get("url", f"https://dexscreener.com/solana/{address}")

        text = (
            f"🎯 <b>{name} ({symbol})</b>\n"
            f"Score: <b>{score}</b>/100\n"
            f"Precio: ${price_usd}\n"
            f"Cambio 1h: {change_h1}%\n"
            f"Volumen 1h: ${volume_h1:,.0f}\n"
            f"Liquidez: ${liquidity_usd:,.0f}\n"
            f"<a href=\"{url}\">Ver en DexScreener</a>\n"
            f"<code>{address}</code>"
        )
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
            message_thread_id=self._message_thread_id,
        )


# --------------------------------------------------------------------------
# Radar loop
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Radar de memecoins trending (DexScreener)")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    config = Config.from_toml(args.config)
    _setup_logging(config.verbose)

    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise SystemExit("Falta telegram.bot_token o telegram.chat_id en config.toml")

    radar = MemecoinRadar(config)
    asyncio.run(radar.run())


if __name__ == "__main__":
    main()
