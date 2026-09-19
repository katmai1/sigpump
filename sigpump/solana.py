"""
sigpump/solana.py

Comprueba en la blockchain (RPC de Solana) si el mint de un token conserva
autoridades peligrosas: mintAuthority (pueden acuñar más y diluir a los que
compraron) o freezeAuthority (pueden congelar tus tokens y dejarte sin poder
vender). Las autoridades solo se pueden renunciar, nunca volver a poner, así
que un token limpio se recuerda para siempre y uno con autoridades se vuelve
a mirar cada tanto por si las renunció.
"""

import asyncio
import logging
import time

import aiohttp  # type: ignore[import-not-found]

log = logging.getLogger()

MAINNET_RPC_URL = "https://api.mainnet-beta.solana.com"
# getMultipleAccounts acepta como máximo 100 cuentas por llamada.
MAX_ACCOUNTS_PER_CALL = 100
REQUEST_TIMEOUT_SECONDS = 15
# Un token con autoridades puede renunciarlas después: se vuelve a consultar
# pasado este tiempo. Uno limpio ya no puede volver atrás y no caduca.
UNSAFE_TTL_SECONDS = 30 * 60


class TokenAuthorities:
    """Autoridades del mint de cada token, cacheadas entre consultas."""

    def __init__(self, session: aiohttp.ClientSession, rpc_url: str = MAINNET_RPC_URL):
        self._session = session
        self._rpc_url = rpc_url
        # dirección -> (motivo, o None si el token está limpio; momento de la consulta)
        self._cache: dict[str, tuple[str | None, float]] = {}

    def _cached(self, address: str) -> tuple[str | None, float] | None:
        """Lo que se sabe del token, o None si hay que consultarlo."""
        entry = self._cache.get(address)
        if entry is None:
            return None
        reason, checked_at = entry
        if reason is not None and time.time() - checked_at > UNSAFE_TTL_SECONDS:
            return None
        return entry

    async def unsafe_reasons(self, addresses: list[str]) -> dict[str, str]:
        """
        {dirección: motivo} de los tokens que se pueden acuñar o congelar. Los
        que no se pudieron consultar no figuran: es preferible dejar pasar una
        alerta a perderlas todas porque el RPC esté caído.
        """
        wanted = list(dict.fromkeys(addresses))
        pending = [a for a in wanted if self._cached(a) is None]
        for i in range(0, len(pending), MAX_ACCOUNTS_PER_CALL):
            await self._fetch(pending[i : i + MAX_ACCOUNTS_PER_CALL])
        reasons = {}
        for address in wanted:
            entry = self._cached(address)
            if entry is not None and entry[0] is not None:
                reasons[address] = entry[0]
        return reasons

    async def _fetch(self, addresses: list[str]) -> None:
        """Consulta un lote de mints y guarda lo que devuelva. Ante cualquier
        fallo no cachea nada: se reintenta en la próxima señal."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [addresses, {"encoding": "jsonParsed"}],
        }
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with self._session.post(self._rpc_url, json=body, timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("No se pudieron consultar las autoridades en %s: %s", self._rpc_url, exc)
            return
        if not isinstance(data, dict) or data.get("error"):
            log.warning("El RPC de Solana devolvió un error: %s", (data or {}).get("error"))
            return
        accounts = (data.get("result") or {}).get("value")
        if not isinstance(accounts, list) or len(accounts) != len(addresses):
            log.warning("Respuesta inesperada del RPC de Solana para %d mints", len(addresses))
            return

        now = time.time()
        for address, account in zip(addresses, accounts):
            # Con jsonParsed, una cuenta que el RPC no sabe parsear vuelve como
            # texto en base64 en vez de como diccionario.
            data = (account or {}).get("data")
            parsed = data.get("parsed") if isinstance(data, dict) else None
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if not isinstance(info, dict):
                # Cuenta inexistente, o que no es un mint (otra chain, token raro):
                # no se sabe nada, así que no se cachea ni se bloquea.
                continue
            problems = []
            if info.get("mintAuthority"):
                problems.append("se pueden acuñar más")
            if info.get("freezeAuthority"):
                problems.append("se pueden congelar")
            reason = "el creador conserva autoridades: " + " y ".join(problems) if problems else None
            self._cache[address] = (reason, now)
