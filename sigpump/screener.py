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
REQUEST_TIMEOUT_SECONDS = 15
# Fallos transitorios del lado del servidor: se reintentan igual que el 429.
# Un 4xx distinto de 429 es un error nuestro (URL mal armada) y no se reintenta.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class DexScreenerClient:
    """Thin async client for the public DexScreener REST API."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, path: str) -> Any:
        """GET genérico con reintentos y backoff exponencial ante rate limit,
        errores 5xx y fallos de red/timeout. Devuelve None si se agotan los
        reintentos, para que el llamador pueda tratarlo como "sin datos esta
        vez" sin romper el scan."""
        url = f"{API_BASE}{path}"
        for attempt in range(MAX_RETRIES + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                async with self._session.get(url, timeout=timeout) as resp:
                    if resp.status not in RETRYABLE_STATUSES:
                        resp.raise_for_status()
                        # content_type=None: la API a veces responde con un
                        # Content-Type que aiohttp no reconoce como JSON.
                        return await resp.json(content_type=None)
                    reason = f"HTTP {resp.status}"
            except aiohttp.ClientResponseError:
                # Viene de raise_for_status(): es un 4xx real, no se reintenta.
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Corte de red, DNS, timeout: transitorios, se reintentan.
                reason = f"{type(exc).__name__}: {exc}"

            if attempt == MAX_RETRIES:
                log.warning("DexScreener %s en %s, sin más reintentos", reason, path)
                return None
            wait = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            log.warning("DexScreener %s en %s, reintentando en %.1fs", reason, path, wait)
            await asyncio.sleep(wait)
        return None

    async def _get_list(self, path: str) -> list[dict]:
        """GET que espera una lista de objetos JSON. Filtra cualquier cosa que
        no sea un dict para que un cambio de forma en la API no se propague
        como AttributeError al resto del código."""
        data = await self._get(path)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def get_latest_boosted(self) -> list[dict]:
        """Tokens con boost (promoción paga) reciente, cualquier chain."""
        return await self._get_list("/token-boosts/latest/v1")

    async def get_top_boosted(self) -> list[dict]:
        """Tokens con más boosts activos acumulados, cualquier chain."""
        return await self._get_list("/token-boosts/top/v1")

    async def get_latest_profiles(self) -> list[dict]:
        """Perfiles de token nuevos o actualizados recientemente, cualquier chain."""
        return await self._get_list("/token-profiles/latest/v1")

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
                if isinstance(pair, dict) and pair.get("chainId") == chain_id:
                    pairs.append(pair)
        return pairs
