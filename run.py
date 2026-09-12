"""
Radar de solo-lectura para memecoins trending en DexScreener.
No ejecuta trades: descubre candidatos, los puntúa y avisa por Telegram
cuando cruzan el umbral configurado.
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
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
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
    
    try:
        asyncio.run(radar.run())
    except KeyboardInterrupt:
        print("Interrupción recibida, deteniendo el radar...")
    except Exception as e:
        log.error(f"Error inesperado: {e}")

if __name__ == "__main__":
    main()
