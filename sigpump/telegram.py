"""
sigpump/telegram.py

Formatea y envía la alerta de un par de mercado como mensaje de Telegram
(HTML) usando python-telegram-bot.
"""

import html

from telegram import Bot  # type: ignore[import-not-found]
from telegram.constants import ParseMode  # type: ignore[import-not-found]


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str, message_thread_id: int | None = None):
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        # Opcional: ID de un "topic" dentro de un grupo con Temas activados.
        self._message_thread_id = message_thread_id

    async def send(self, pair: dict, score: float) -> None:
        """Arma y envía el mensaje de alerta para `pair` con su `score` ya calculado."""
        base = pair.get("baseToken", {})
        # symbol/name vienen de la API y son controlados por quien crea el token:
        # deben escaparse antes de insertarlos en un mensaje con parse_mode HTML.
        symbol = html.escape(str(base.get("symbol", "?")))
        name = html.escape(str(base.get("name", "?")))
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
            disable_web_page_preview=True,
            message_thread_id=self._message_thread_id,
        )

