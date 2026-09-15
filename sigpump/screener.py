"""
sigpump/screener.py

Cliente HTTP async de solo lectura contra la API pública de DexScreener
(sin API key). No hay lógica de negocio acá: solo llamadas a los endpoints
y el manejo de rate limit/paginación de direcciones.
"""

import asyncio
import time
import aiohttp  # type: ignore[import-not-found]
from typing import Any
import logging

from sigpump.util import normalize_address, to_float

log = logging.getLogger()

API_BASE = "https://api.dexscreener.com"
MAX_ADDRESSES_PER_CALL = 30  # límite de la API para /latest/dex/tokens/{addrs}
# GeckoTerminal (API pública, sin key) expone el ranking de pools trending por
# chain, algo que DexScreener no publica, y un precio/liquidez propios con los
# que contrastar los de DexScreener. El tier gratuito sirve hasta la página 10
# del ranking (20 pools por página). Documenta ~30 requests/minuto, pero en la
# práctica corta bastante antes.
GECKO_API_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_MAX_PAGES = 10
GECKO_MAX_POOLS_PER_CALL = 30  # límite de /pools/multi/{addrs}
# Separación mínima entre requests a GeckoTerminal: en ráfaga responde 429 enseguida.
# 6s (~10/min): a 3s (~20/min) daba 429 tras 6-7 requests y la pasada entera
# se quedaba sin velas para verificar.
GECKO_REQUEST_INTERVAL_SECONDS = 6.0
# Ante un 429 de GeckoTerminal se espera a que se renueve la ventana del
# minuto. El backoff de 2s/4s/8s caía dentro de la misma ventana: los
# reintentos también daban 429 y encima seguían gastando cupo.
GECKO_RATE_LIMIT_WAIT_SECONDS = 60.0
GECKO_MAX_RETRIES = 2
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
        # time.monotonic() del último request a GeckoTerminal, para espaciarlos.
        self._gecko_last_request = float("-inf")
        # El escaneo y la vigilancia piden a GeckoTerminal a la vez: sin el
        # lock los dos calculaban la misma espera y disparaban juntos.
        self._gecko_lock = asyncio.Lock()

    async def _gecko_get(self, path: str) -> Any:
        """GET a GeckoTerminal separado al menos GECKO_REQUEST_INTERVAL_SECONDS
        del anterior. Centralizado porque en una pasada se piden trending,
        pools y velas: espaciar solo las páginas del ranking no alcanzaba."""
        async with self._gecko_lock:
            wait = self._gecko_last_request + GECKO_REQUEST_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._get(
                    path,
                    base=GECKO_API_BASE,
                    max_retries=GECKO_MAX_RETRIES,
                    rate_limit_wait=GECKO_RATE_LIMIT_WAIT_SECONDS,
                )
            finally:
                self._gecko_last_request = time.monotonic()

    async def _get(
        self,
        path: str,
        base: str = API_BASE,
        max_retries: int = MAX_RETRIES,
        rate_limit_wait: float | None = None,
    ) -> Any:
        """GET genérico con reintentos y backoff exponencial ante rate limit,
        errores 5xx y fallos de red/timeout. Con `rate_limit_wait`, un 429
        espera eso fijo en vez del backoff. Devuelve None si se agotan los
        reintentos, para que el llamador pueda tratarlo como "sin datos esta
        vez" sin romper el scan."""
        url = f"{base}{path}"
        source = "GeckoTerminal" if base == GECKO_API_BASE else "DexScreener"
        for attempt in range(max_retries + 1):
            status = None
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                async with self._session.get(url, timeout=timeout) as resp:
                    if resp.status not in RETRYABLE_STATUSES:
                        resp.raise_for_status()
                        # content_type=None: la API a veces responde con un
                        # Content-Type que aiohttp no reconoce como JSON.
                        return await resp.json(content_type=None)
                    status = resp.status
                    reason = f"HTTP {status}"
            except aiohttp.ClientResponseError:
                # Viene de raise_for_status(): es un 4xx real, no se reintenta.
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Corte de red, DNS, timeout: transitorios, se reintentan.
                reason = f"{type(exc).__name__}: {exc}"

            if attempt == max_retries:
                log.warning("%s %s en %s, sin más reintentos", source, reason, path)
                return None
            if status == 429 and rate_limit_wait is not None:
                wait = rate_limit_wait
            else:
                wait = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            # DEBUG: un reintento que después funciona es ruido; el WARNING
            # queda para cuando se agotan los reintentos.
            log.debug("%s %s en %s, reintentando en %.1fs", source, reason, path, wait)
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
            data = await self._gecko_get(f"/networks/{network}/trending_pools?page={page}")
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
        """Datos de mercado (pares) de las direcciones pedidas, en lotes de 30."""
        pairs: list[dict] = []
        # La API acepta como máximo MAX_ADDRESSES_PER_CALL direcciones por
        # llamada, así que se parte la lista en lotes.
        for i in range(0, len(addresses), MAX_ADDRESSES_PER_CALL):
            batch = addresses[i : i + MAX_ADDRESSES_PER_CALL]
            joined = ",".join(batch)
            # /tokens/v1 y no /latest/dex/tokens: el legacy corta en 30 pares
            # por respuesta en total, y los tokens con muchos pools dejaban
            # sin datos (y sin alerta) a varios del lote.
            try:
                data = await self._get(f"/tokens/v1/{chain_id}/{joined}")
            except (aiohttp.ClientResponseError, ValueError) as exc:
                # 4xx (p. ej. un 403 de Cloudflare) o un cuerpo que no es JSON:
                # se pierde solo este lote, no la pasada entera.
                log.warning("DexScreener falló para un lote de %d direcciones: %s", len(batch), exc)
                continue
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

    async def get_gecko_pools(self, chain_id: str, pool_addresses: list[str]) -> dict[str, dict]:
        """
        Datos de GeckoTerminal de los pools pedidos, indexados por dirección
        de pool: {"reserve_usd": float, "token_prices": {token: precio_usd}}.
        Los precios van por dirección de token porque GeckoTerminal puede
        orientar el par al revés que DexScreener (base <-> quote). Los pools
        sin datos, o de un lote que falló, simplemente no figuran.
        """
        network = GECKO_NETWORKS.get(chain_id, chain_id)
        result: dict[str, dict] = {}
        for i in range(0, len(pool_addresses), GECKO_MAX_POOLS_PER_CALL):
            batch = pool_addresses[i : i + GECKO_MAX_POOLS_PER_CALL]
            try:
                data = await self._gecko_get(f"/networks/{network}/pools/multi/{','.join(batch)}")
            except (aiohttp.ClientResponseError, ValueError) as exc:
                log.warning("GeckoTerminal falló para un lote de %d pools: %s", len(batch), exc)
                continue
            pools = data.get("data") if isinstance(data, dict) else None
            for pool in pools if isinstance(pools, list) else []:
                attributes = pool.get("attributes") if isinstance(pool, dict) else None
                relationships = pool.get("relationships") if isinstance(pool, dict) else None
                if not isinstance(attributes, dict) or not isinstance(relationships, dict):
                    continue
                address = attributes.get("address")
                if not isinstance(address, str) or not address:
                    continue
                token_prices: dict[str, float] = {}
                for side in ("base", "quote"):
                    token = _gecko_token_address(relationships.get(f"{side}_token"))
                    if token:
                        token_prices[normalize_address(token)] = to_float(
                            attributes.get(f"{side}_token_price_usd")
                        )
                result[normalize_address(address)] = {
                    "reserve_usd": to_float(attributes.get("reserve_in_usd")),
                    "token_prices": token_prices,
                }
        return result

    async def get_pool_candles(
        self, chain_id: str, pool_address: str, token_address: str, limit: int
    ) -> list[tuple[float, float, float, float, float, float]]:
        """
        Últimas `limit` velas de 1 minuto del pool, con el precio en USD de
        `token_address`, como (timestamp, open, high, low, close, volume) de
        la más vieja a la más nueva. GeckoTerminal omite los minutos sin
        trades. [] si la API falla o no hay datos.
        """
        network = GECKO_NETWORKS.get(chain_id, chain_id)
        # token=<dirección>: sin esto las velas son del base según GeckoTerminal,
        # que puede ser el quote (SOL) si orienta el par al revés.
        path = (
            f"/networks/{network}/pools/{pool_address}/ohlcv/minute"
            f"?aggregate=1&limit={limit}&currency=usd&token={token_address}"
        )
        try:
            data = await self._gecko_get(path)
        except (aiohttp.ClientResponseError, ValueError) as exc:
            log.warning("GeckoTerminal falló al pedir velas de %s: %s", pool_address, exc)
            return []
        body = data.get("data") if isinstance(data, dict) else None
        attributes = body.get("attributes") if isinstance(body, dict) else None
        rows = attributes.get("ohlcv_list") if isinstance(attributes, dict) else None
        candles = [
            (
                to_float(row[0]), to_float(row[1]), to_float(row[2]), to_float(row[3]), to_float(row[4]),
                # El volumen se usa para medir si venía sostenido o explotó de golpe.
                to_float(row[5]) if len(row) > 5 else 0.0,
            )
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, list) and len(row) >= 5
        ]
        # La API las devuelve de la más nueva a la más vieja.
        return sorted(candles)
