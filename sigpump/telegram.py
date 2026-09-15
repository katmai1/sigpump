"""
sigpump/telegram.py

Formatea y envía la alerta de un par de mercado como mensaje de Telegram
(HTML) usando python-telegram-bot.
"""

import asyncio
import html
import warnings
from datetime import timedelta

from telegram import Bot  # type: ignore[import-not-found]
from telegram.constants import ParseMode  # type: ignore[import-not-found]
from telegram.error import RetryAfter  # type: ignore[import-not-found]
from telegram.warnings import PTBDeprecationWarning  # type: ignore[import-not-found]

from sigpump.signals import (
    RECENT_HIGH_MINUTES,
    CandleStats,
    EarlySignal,
    buy_ratio_m5,
    volume_acceleration,
)
from sigpump.util import to_float

# Espera máxima aceptable ante flood control. Más que esto bloquearía el
# loop del radar demasiado tiempo: se deja fallar y se reintenta la próxima pasada.
MAX_RETRY_AFTER_SECONDS = 60.0


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

    def format_message(
        self,
        pair: dict,
        score: float,
        candles: CandleStats | None = None,
        early: EarlySignal | None = None,
    ) -> str:
        """Arma el HTML del mensaje de alerta para `pair` con su `score` y,
        si la verificación pidió velas, su posición respecto del mínimo y
        del máximo reciente.

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
        change_m5 = to_float((pair.get("priceChange") or {}).get("m5"))
        change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))
        # Datos de "¿llego a tiempo?": aceleración y compras de 5 min y, si hay
        # velas, cuánto subió ya y si cae desde el pico reciente.
        momentum = f"Aceleración vol. 5m: x{volume_acceleration(pair):.1f}\n"
        buy_ratio = buy_ratio_m5(pair)
        if buy_ratio is not None:
            momentum += f"Compras 5m: {buy_ratio * 100:.0f}%\n"
        if candles is not None:
            momentum += (
                f"Sobre mínimo 1h: +{candles.rise_from_low_pct:.0f}%\n"
                f"Bajo máximo {RECENT_HIGH_MINUTES}m: -{candles.drop_from_recent_high_pct:.0f}%\n"
            )
        # marketCap suele venir ausente en tokens nuevos; fdv es el fallback de DexScreener.
        market_cap_usd = to_float(pair.get("marketCap")) or to_float(pair.get("fdv"))
        pool = html.escape(str(pair.get("dexId", "?")))
        # quote=True porque va dentro de un atributo href="...": una comilla
        # sin escapar rompe el tag y Telegram rechaza el mensaje entero.
        raw_url = pair.get("url") or (
            f"https://dexscreener.com/{self._chain_id}/{base.get('address', '')}"
        )
        url = html.escape(str(raw_url), quote=True)
        links = f"<a href=\"{url}\">Ver en DexScreener</a>"
        # photon-sol solo cubre Solana.
        chain_id = pair.get("chainId") or self._chain_id
        token_address = base.get("address")
        if chain_id == "solana" and token_address:
            photon_url = html.escape(
                f"https://photon-sol.tinyastro.io/en/lp/{token_address}", quote=True
            )
            links += f" | <a href=\"{photon_url}\">Ver en Photon</a>"

        if early is None:
            header = f"🎯 <b>{name} ({symbol})</b>\n"
        else:
            # Prealerta: lo primero que se lee es el arranque, que es lo que
            # decide si todavía hay margen para entrar.
            header = (
                f"⚡ <b>PREALERTA {name} ({symbol})</b>\n"
                f"Arranque: <b>{early.price_move_pct:+.1f}%</b> sobre la base de "
                f"{early.baseline_minutes:.0f} min\n"
                f"Vol. 5m x{early.volume_ratio:.1f} y txns 5m x{early.txns_ratio:.1f} sobre la base\n"
            )
        return (
            f"{header}"
            f"Score: <b>{score}</b>/100\n"
            f"Precio: ${price_usd}\n"
            f"Cambio 5m: {change_m5:+.1f}%\n"
            f"Cambio 1h: {change_h1:+.1f}%\n"
            f"Volumen 1h: ${volume_h1:,.0f}\n"
            f"{momentum}"
            f"Liquidez: ${liquidity_usd:,.0f}\n"
            f"Cap. mercado: ${market_cap_usd:,.0f}\n"
            f"Pool: {pool}\n\n"
            f"<code>{address}</code>\n\n"
            f"{links}"
        )

    async def send(
        self,
        pair: dict,
        score: float,
        candles: CandleStats | None = None,
        early: EarlySignal | None = None,
    ) -> None:
        """Arma y envía el mensaje de alerta (o de prealerta, con `early`) para
        `pair` con su `score` ya calculado."""
        kwargs = dict(
            chat_id=self._chat_id,
            text=self.format_message(pair, score, candles, early),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            message_thread_id=self._message_thread_id,
        )
        try:
            await self._bot.send_message(**kwargs)
        except RetryAfter as exc:
            # Flood control (~20 mensajes/min en grupos), típico cuando salen
            # varias alertas juntas al arrancar. Se espera lo que pide Telegram
            # y se reintenta una vez; antes la alerta se perdía hasta la
            # próxima pasada.
            # PTB avisa que retry_after pasará de int a timedelta; se soportan
            # ambos, así que el aviso solo ensuciaría el log.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PTBDeprecationWarning)
                retry_after = exc.retry_after
            seconds = (
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else float(retry_after)
            )
            if seconds > MAX_RETRY_AFTER_SECONDS:
                raise
            await asyncio.sleep(seconds)
            await self._bot.send_message(**kwargs)
