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
from pathlib import Path

from sigpump.config import Config
from sigpump.radar import MemecoinRadar


log = logging.getLogger()


def _setup_logging(verbose: bool) -> None:
    """Logging a stdout; nivel DEBUG si radar.verbose=true en el TOML, si no INFO."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main() -> None:
    """Punto de entrada del CLI: parsea --config, valida credenciales de
    Telegram y arranca el loop infinito del radar."""
    parser = argparse.ArgumentParser(description="Radar de memecoins trending (DexScreener)")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    config = Config.from_toml(args.config)
    _setup_logging(config.verbose)

    # Sin estas dos claves no hay forma de notificar, así que fallamos rápido
    # en vez de arrancar un radar que nunca podrá avisar nada.
    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise SystemExit("Falta telegram.bot_token o telegram.chat_id en config.toml")

    radar = MemecoinRadar(config)
    asyncio.run(radar.run())


if __name__ == "__main__":
    main()
