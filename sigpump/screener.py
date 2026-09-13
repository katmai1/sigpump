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
# GeckoTerminal (API pública, sin key) expone el ranking de pools trending por
# chain, algo que DexScreener no publica. El tier gratuito sirve hasta la
# página 10 (20 pools por página) y limita a ~30 requests/minuto.
GECKO_API_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_MAX_PAGES = 10
GECKO_PAGE_DELAY_SECONDS = 1.0
# chainId de DexScreener -> network id de GeckoTerminal, solo donde difieren.
GECKO_NETWORKS = {"ethereum": "eth", "polygon": "polygon_pos", "avalanche": "avax"}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 15
# Fallos transitorios del lado del servidor: se reintentan igual que el 429.
# Un 4xx distinto de 429 es un error nuestro (URL mal armada) y no se reintenta.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _gecko_token_address(relationship: Any) -> str:
    """Dirección de un token a partir de una relación de GeckoTerminal, cuyo
    id tiene la forma "{network}_{address}". "" si la forma no es la esperada."""
    data = relationship.get("data") if isinstance(relationship, dict) else None
    token_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(token_id, str) or "_" not in token_id:
        return ""
    return token_id.split("_", 1)[1]


class DexScreenerClient:
    """Thin async client for the public DexScreener REST API."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, path: str, base: str = API_BASE) -> Any:
        """GET genérico con reintentos y backoff exponencial ante rate limit,
        errores 5xx y fallos de red/timeout. Devuelve None si se agotan los
        reintentos, para que el llamador pueda tratarlo como "sin datos esta
        vez" sin romper el scan."""
        url = f"{base}{path}"
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

    async def get_latest_takeovers(self) -> list[dict]:
        """Community takeovers recientes (la comunidad retomó un token abandonado)."""
        return await self._get_list("/community-takeovers/latest/v1")

    async def get_latest_ads(self) -> list[dict]:
        """Tokens con anuncios pagos recientes en DexScreener."""
        return await self._get_list("/ads/latest/v1")

    async def get_trending_tokens(self, chain_id: str, pages: int) -> list[str]:
        """
        Direcciones de los base tokens de los pools trending de GeckoTerminal,
        en orden de ranking y sin repetidos. A diferencia de boosts/perfiles
        (que dependen de que alguien pague o edite), refleja actividad real
        de mercado, así que rota mucho más entre pasadas.
        """
        network = GECKO_NETWORKS.get(chain_id, chain_id)
        bases: list[str] = []
        quotes: set[str] = set()
        for page in range(1, min(pages, GECKO_MAX_PAGES) + 1):
            if page > 1:
                # Páginas en serie y espaciadas: en ráfaga el tier gratuito
                # responde 429 enseguida.
                await asyncio.sleep(GECKO_PAGE_DELAY_SECONDS)
            data = await self._get(
                f"/networks/{network}/trending_pools?page={page}", base=GECKO_API_BASE
            )
            pools = data.get("data") if isinstance(data, dict) else None
            if not isinstance(pools, list) or not pools:
                break
            for pool in pools:
                relationships = pool.get("relationships") if isinstance(pool, dict) else None
                if not isinstance(relationships, dict):
                    continue
                base_address = _gecko_token_address(relationships.get("base_token"))
                quote_address = _gecko_token_address(relationships.get("quote_token"))
                if base_address:
                    bases.append(base_address)
                if quote_address:
                    quotes.add(quote_address)
        # Un token que aparece como quote (SOL, USDC...) no es un memecoin
        # trending aunque en algún pool figure como base.
        return [a for a in dict.fromkeys(bases) if a not in quotes]

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
