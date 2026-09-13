"""
Radar de solo-lectura para memecoins trending en DexScreener.
No ejecuta trades: descubre candidatos, los puntúa y avisa por Telegram
cuando cruzan el umbral configurado.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import tomllib
from pathlib import Path

from sigpump.config import Config
from sigpump.radar import MemecoinRadar


log = logging.getLogger()


def _setup_logging(verbose: bool) -> None:
    """Logging a stdout; nivel DEBUG si radar.verbose=true en el TOML, si no INFO."""
    logging.basicConfig(
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # basicConfig ignora `level` si ya hay handlers configurados, así que el
    # nivel se aplica siempre acá: _setup_logging se llama dos veces (antes y
    # después de leer el TOML, para que un error de config ya salga formateado).
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    """Punto de entrada del CLI: parsea --config, valida credenciales de
    Telegram y arranca el loop infinito del radar."""
    parser = argparse.ArgumentParser(description="Radar de memecoins trending (DexScreener)")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    _setup_logging(verbose=False)
    try:
        config = Config.from_toml(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        # Un TOML ausente, mal formado o con valores inválidos es un error de
        # uso: mensaje claro y exit 1, no un traceback crudo.
        raise SystemExit(f"Configuración inválida: {e}") from e
    _setup_logging(config.verbose)

    # Sin estas dos claves no hay forma de notificar, así que fallamos rápido
    # en vez de arrancar un radar que nunca podrá avisar nada.
    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise SystemExit("Falta telegram.bot_token o telegram.chat_id en config.toml")

    radar = MemecoinRadar(config)

    try:
        asyncio.run(radar.run())
    except KeyboardInterrupt:
        log.info("Interrupción recibida, deteniendo el radar...")
    except Exception:
        # exception() conserva el traceback y el exit 1 le avisa al supervisor
        # (systemd, Docker) que el proceso murió y hay que reiniciarlo.
        log.exception("Error inesperado, deteniendo el radar")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
