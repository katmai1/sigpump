"""
run.py

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
  python run.py --config config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sigpump.config import Config
from sigpump.radar import MemecoinRadar


# 
log = logging.getLogger()


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
