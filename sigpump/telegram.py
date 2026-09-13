"""
sigpump/telegram.py

Formatea y envía la alerta de un par de mercado como mensaje de Telegram
(HTML) usando python-telegram-bot.
"""

import html

from telegram import Bot  # type: ignore[import-not-found]
from telegram.constants import ParseMode  # type: ignore[import-not-found]

from sigpump.util import to_float


class TelegramAlerter:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        message_thread_id: int | None = None,
        chain_id: str = "solana",
    ):
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        # Opcional: ID de un "topic" dentro de un grupo con Temas activados.
        self._message_thread_id = message_thread_id
        # Solo se usa para armar el link de fallback si la API no trae `url`.
        self._chain_id = chain_id

    async def __aenter__(self) -> "TelegramAlerter":
        """initialize() valida el token contra la API (getMe) y prepara el
        cliente HTTP. Sin esto un token inválido recién se descubría al
        intentar mandar la primera alerta, horas después de arrancar."""
        await self._bot.initialize()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._bot.shutdown()

    def format_message(self, pair: dict, score: float) -> str:
        """Arma el HTML del mensaje de alerta para `pair` con su `score`.

        Todo lo que viene de la API pasa por html.escape() o por to_float():
        symbol/name/url los controla quien crea el token, y los campos
        numéricos pueden llegar como string o null."""
        base = pair.get("baseToken") or {}
        symbol = html.escape(str(base.get("symbol", "?")))
        name = html.escape(str(base.get("name", "?")))
        address = html.escape(str(base.get("address", "?")))
        price_usd = html.escape(str(pair.get("priceUsd", "?")))
        liquidity_usd = to_float((pair.get("liquidity") or {}).get("usd"))
        volume_h1 = to_float((pair.get("volume") or {}).get("h1"))
        change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))
        # marketCap suele venir ausente en tokens nuevos; fdv es el fallback de DexScreener.
        market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
        pool = html.escape(str(pair.get("dexId", "?")))
        # quote=True porque va dentro de un atributo href="...": una comilla
        # sin escapar rompe el tag y Telegram rechaza el mensaje entero.
        raw_url = pair.get("url") or (
            f"https://dexscreener.com/{self._chain_id}/{base.get('address', '')}"
        )
        url = html.escape(str(raw_url), quote=True)

        return (
            f"🎯 <b>{name} ({symbol})</b>\n"
            f"Score: <b>{score}</b>/100\n"
            f"Precio: ${price_usd}\n"
            f"Cambio 1h: {change_h1:+.1f}%\n"
            f"Volumen 1h: ${volume_h1:,.0f}\n"
            f"Liquidez: ${liquidity_usd:,.0f}\n"
            f"Cap. mercado: ${market_cap_usd:,.0f}\n"
            f"Pool: {pool}\n"
            f"<a href=\"{url}\">Ver en DexScreener</a>\n"
            f"<code>{address}</code>"
        )

    async def send(self, pair: dict, score: float) -> None:
        """Arma y envía el mensaje de alerta para `pair` con su `score` ya calculado."""
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=self.format_message(pair, score),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            message_thread_id=self._message_thread_id,
        )
