"""
sigpump/screener.py

Cliente HTTP async de solo lectura contra la API pública de DexScreener
(sin API key). No hay lógica de negocio acá: solo llamadas a los endpoints
y el manejo de rate limit/paginación de direcciones.
"""

import asyncio
import aiohttp  # type: ignore[import-not-found]
from typing import Any
import logging

log = logging.getLogger()

API_BASE = "https://api.dexscreener.com"
MAX_ADDRESSES_PER_CALL = 30  # límite de la API para /latest/dex/tokens/{addrs}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

class DexScreenerClient:
    """Thin async client for the public DexScreener REST API."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, path: str) -> Any:
        """GET genérico con reintentos y backoff exponencial ante 429
        (rate limit). Devuelve None si se agotan los reintentos, para que
        el llamador pueda tratarlo como "sin datos esta vez" sin romper el scan."""
        url = f"{API_BASE}{path}"
        for attempt in range(MAX_RETRIES + 1):
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    if attempt == MAX_RETRIES:
                        log.warning("DexScreener rate limit hit on %s, sin más reintentos", path)
                        return None
                    wait = RETRY_BACKOFF_SECONDS * (2 ** attempt)
                    log.warning("DexScreener rate limit hit on %s, reintentando en %.1fs", path, wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return await resp.json()

    async def get_latest_boosted(self) -> list[dict]:
        """Tokens con boost (promoción paga) reciente, cualquier chain."""
        data = await self._get("/token-boosts/latest/v1")
        return data or []

    async def get_top_boosted(self) -> list[dict]:
        """Tokens con más boosts activos acumulados, cualquier chain."""
        data = await self._get("/token-boosts/top/v1")
        return data or []

    async def get_latest_profiles(self) -> list[dict]:
        """Perfiles de token nuevos o actualizados recientemente, cualquier chain."""
        data = await self._get("/token-profiles/latest/v1")
        return data or []

    async def get_pairs_for_tokens(self, chain_id: str, addresses: list[str]) -> list[dict]:
        """Fetch full pair/market data for up to 30 token addresses at once."""
        pairs: list[dict] = []
        # La API acepta como máximo MAX_ADDRESSES_PER_CALL direcciones por
        # llamada, así que se parte la lista en lotes.
        for i in range(0, len(addresses), MAX_ADDRESSES_PER_CALL):
            batch = addresses[i : i + MAX_ADDRESSES_PER_CALL]
            joined = ",".join(batch)
            data = await self._get(f"/latest/dex/tokens/{joined}")
            if not data:
                continue
            # Endpoint returns either {"pairs": [...]} or a bare list depending on version
            batch_pairs = data.get("pairs") if isinstance(data, dict) else data
            for pair in batch_pairs or []:
                # Filtro extra por las dudas: la API a veces devuelve pares
                # de otras chains aunque se haya pedido por dirección específica.
                if pair.get("chainId") == chain_id:
                    pairs.append(pair)
        return pairs


