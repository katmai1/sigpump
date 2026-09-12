import aiohttp  # type: ignore[import-not-found]
from typing import Any
import logging

log = logging.getLogger()

API_BASE = "https://api.dexscreener.com"
MAX_ADDRESSES_PER_CALL = 30

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


