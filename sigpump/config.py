"""
sigpump/config.py

Carga la configuración desde config.toml hacia un dataclass tipado y la
valida al arrancar.
"""

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sigpump.solana import MAINNET_RPC_URL

log = logging.getLogger()

# Claves válidas por sección del TOML.
_KNOWN_KEYS: dict[str, set[str]] = {
    "dexscreener": {"chain_id", "quote_tokens", "dex_ids"},
    "radar": {"alert_log_path", "verbose"},
    "solana": {"check_token_authorities", "rpc_url"},
    "wallets": {
        "enabled",
        "file",
        "interval_seconds",
        "min_sol",
        "cooldown_minutes",
        "confluence_minutes",
        "min_wallets",
        "token_cooldown_minutes",
        "realert_new_wallets",
        "max_tokens_per_hour",
        "max_txs_per_hour",
        "max_inactive_days",
        "blacklist_file",
        "rug_drop_pct",
        "loser_min_signals",
        "loser_max_median_ret_pct",
        "min_pair_age_minutes",
        "max_price_change_h1_pct",
        "min_market_cap_usd",
        "websocket",
        "ws_url",
        "full_poll_minutes",
    },
    "telegram": {"bot_token", "chat_id", "message_thread_id"},
}

_NUMBER = (int, float)
# Campo de Config -> (nombre en el TOML, tipos aceptados).
_FIELD_TYPES: dict[str, tuple[str, tuple[type, ...]]] = {
    "chain_id": ("[dexscreener].chain_id", (str,)),
    "quote_tokens": ("[dexscreener].quote_tokens", (list,)),
    "dex_ids": ("[dexscreener].dex_ids", (list,)),
    "check_token_authorities": ("[solana].check_token_authorities", (bool,)),
    "solana_rpc_url": ("[solana].rpc_url", (str,)),
    "alert_log_path": ("[radar].alert_log_path", (str,)),
    "wallets_enabled": ("[wallets].enabled", (bool,)),
    "wallets_file": ("[wallets].file", (str,)),
    "wallets_interval_seconds": ("[wallets].interval_seconds", _NUMBER),
    "wallets_min_sol": ("[wallets].min_sol", _NUMBER),
    "wallets_cooldown_minutes": ("[wallets].cooldown_minutes", _NUMBER),
    "wallets_confluence_minutes": ("[wallets].confluence_minutes", _NUMBER),
    "wallets_min_wallets": ("[wallets].min_wallets", (int,)),
    "wallets_token_cooldown_minutes": ("[wallets].token_cooldown_minutes", _NUMBER),
    "wallets_realert_new_wallets": ("[wallets].realert_new_wallets", (int,)),
    "wallets_max_tokens_per_hour": ("[wallets].max_tokens_per_hour", (int,)),
    "wallets_max_txs_per_hour": ("[wallets].max_txs_per_hour", (int,)),
    "wallets_max_inactive_days": ("[wallets].max_inactive_days", _NUMBER),
    "wallets_blacklist_file": ("[wallets].blacklist_file", (str,)),
    "wallets_rug_drop_pct": ("[wallets].rug_drop_pct", _NUMBER),
    "wallets_loser_min_signals": ("[wallets].loser_min_signals", (int,)),
    "wallets_loser_max_median_ret_pct": ("[wallets].loser_max_median_ret_pct", _NUMBER),
    "wallets_min_pair_age_minutes": ("[wallets].min_pair_age_minutes", _NUMBER),
    "wallets_max_price_change_h1_pct": ("[wallets].max_price_change_h1_pct", _NUMBER),
    "wallets_min_market_cap_usd": ("[wallets].min_market_cap_usd", _NUMBER),
    "wallets_websocket": ("[wallets].websocket", (bool,)),
    "wallets_ws_url": ("[wallets].ws_url", (str,)),
    "wallets_full_poll_minutes": ("[wallets].full_poll_minutes", _NUMBER),
    "verbose": ("[radar].verbose", (bool,)),
    "telegram_bot_token": ("[telegram].bot_token", (str,)),
    "telegram_chat_id": ("[telegram].chat_id", (str,)),
    "telegram_message_thread_id": ("[telegram].message_thread_id", (int, type(None))),
}


def _section(raw: dict, name: str) -> dict:
    """Sección `name` del TOML, avisando de claves desconocidas: un typo como
    `min_liquidty_usd` se ignoraba en silencio y se usaba el default."""
    section = raw.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"[{name}] debe ser una sección (tabla), no {section!r}")
    unknown = sorted(set(section) - _KNOWN_KEYS[name])
    if unknown:
        log.warning(
            "Claves desconocidas en [%s], se ignoran: %s. Válidas: %s",
            name,
            ", ".join(unknown),
            ", ".join(sorted(_KNOWN_KEYS[name])),
        )
    return section


@dataclass
class Config:
    """Configuración completa, con defaults sensatos si el TOML no los define."""
    chain_id: str = "solana"
    # Monedas contra las que debe cotizar el par (símbolo o dirección del
    # quote). Vacío = cualquiera.
    quote_tokens: list[str] = field(default_factory=list)
    # dexId de DexScreener en los que debe estar el par (pumpswap, raydium...).
    # Vacío = cualquiera.
    dex_ids: list[str] = field(default_factory=list)
    alert_log_path: str = "alertas.db"
    # Seguimiento de wallets: avisa cuando una de las del fichero entra en un token.
    wallets_enabled: bool = True
    wallets_file: str = "wallets.txt"
    wallets_interval_seconds: float = 15.0
    # Mínimo de SOL gastado para considerar que una transacción es una compra.
    wallets_min_sol: float = 0.1
    # Minutos sin repetir aviso de la misma wallet en el mismo token.
    wallets_cooldown_minutes: float = 60.0
    # Ventana en la que otras wallets seguidas que entraron en el mismo token
    # se mencionan en el aviso.
    wallets_confluence_minutes: float = 60.0
    # Wallets seguidas distintas que tienen que haber comprado el token dentro
    # de confluence_minutes para avisar. 1 = avisar de cada compra.
    wallets_min_wallets: int = 1
    # Minutos sin volver a avisar de un token ya avisado por wallets, salvo
    # que se sumen realert_new_wallets wallets más. 0 = sin límite por token.
    wallets_token_cooldown_minutes: float = 0.0
    wallets_realert_new_wallets: int = 2
    # Una wallet que compra más tokens distintos que esto en una hora no
    # avisa ni cuenta para la confluencia de otras, y con blacklist_file va a
    # la lista negra. 0 = sin tope.
    wallets_max_tokens_per_hour: int = 0
    # Una wallet con más transacciones que esto en una hora es un bot: con
    # blacklist_file va a la lista negra antes de leerlas. 0 = sin tope.
    wallets_max_txs_per_hour: int = 0
    # Una wallet sin ninguna transacción en estos días está abandonada: con
    # blacklist_file va a la lista negra. 0 = no hacerlo.
    wallets_max_inactive_days: float = 30.0
    # Lista negra: sus wallets se quitan del fichero y no se vuelven a seguir.
    # Vacío = sin lista negra.
    wallets_blacklist_file: str = ""
    # Una wallet cuya compra cae este % (rug) en el seguimiento entra en la
    # lista negra sola. 0 = no hacerlo.
    wallets_rug_drop_pct: float = 90.0
    # Una wallet con al menos loser_min_signals tokens registrados cuya
    # mediana a 30 min es loser_max_median_ret_pct o peor entra en la lista
    # negra sola. 0 = no hacerlo.
    wallets_loser_min_signals: int = 0
    wallets_loser_max_median_ret_pct: float = -30.0
    # Filtros del par para los avisos de wallets (0 = sin filtro): los pares
    # recién creados, los que ya subieron mucho en 1h y los de market cap
    # pequeño daban casi siempre pérdidas.
    wallets_min_pair_age_minutes: float = 0.0
    wallets_max_price_change_h1_pct: float = 0.0
    wallets_min_market_cap_usd: float = 0.0
    # Avisos de actividad por el WebSocket del RPC: solo se consultan las
    # wallets que hicieron algo. ws_url vacío = el de rpc_url con wss://.
    wallets_websocket: bool = True
    wallets_ws_url: str = ""
    # Con el WebSocket, cada cuánto se consultan todas por si se perdió algún aviso.
    wallets_full_poll_minutes: float = 10.0
    verbose: bool = False
    # Comprobación en la blockchain de que el token no se pueda acuñar ni congelar.
    check_token_authorities: bool = True
    solana_rpc_url: str = MAINNET_RPC_URL
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int | None = None

    def __post_init__(self) -> None:
        """Valida los rangos apenas se construye el Config, para que un TOML
        inconsistente falle al arrancar y no a mitad de una vuelta."""
        # Tipos primero: con `interval_seconds = "15"` la comparación de
        # abajo tiraba un TypeError crudo en vez de un error de configuración.
        for name, (toml_name, types) in _FIELD_TYPES.items():
            value = getattr(self, name)
            # bool es subclase de int: `min_wallets = true` no es un número.
            if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
                raise ValueError(f"{toml_name} tiene un tipo inválido: {value!r}")
        if not self.chain_id:
            raise ValueError("[dexscreener].chain_id no puede estar vacío")
        if not all(isinstance(quote, str) and quote for quote in self.quote_tokens):
            raise ValueError(
                f"[dexscreener].quote_tokens debe ser una lista de textos ({self.quote_tokens!r})"
            )
        if not all(isinstance(dex, str) and dex for dex in self.dex_ids):
            raise ValueError(f"[dexscreener].dex_ids debe ser una lista de textos ({self.dex_ids!r})")
        if not self.wallets_enabled:
            raise ValueError("No hay nada que avisar: [wallets].enabled está desactivado")
        if not self.solana_rpc_url:
            raise ValueError("[solana].rpc_url no puede estar vacío")
        if self.wallets_min_wallets < 1:
            raise ValueError(f"[wallets].min_wallets debe ser >= 1 ({self.wallets_min_wallets})")
        if self.wallets_realert_new_wallets < 1:
            raise ValueError(
                f"[wallets].realert_new_wallets debe ser >= 1 ({self.wallets_realert_new_wallets})"
            )
        if self.wallets_max_tokens_per_hour < 0:
            raise ValueError(
                f"[wallets].max_tokens_per_hour debe ser >= 0 ({self.wallets_max_tokens_per_hour})"
            )
        if self.wallets_max_txs_per_hour < 0:
            raise ValueError(
                f"[wallets].max_txs_per_hour debe ser >= 0 ({self.wallets_max_txs_per_hour})"
            )
        if self.wallets_max_inactive_days < 0:
            raise ValueError(
                f"[wallets].max_inactive_days debe ser >= 0 ({self.wallets_max_inactive_days})"
            )
        if not 0 <= self.wallets_rug_drop_pct <= 100:
            raise ValueError(
                f"[wallets].rug_drop_pct debe estar entre 0 y 100 ({self.wallets_rug_drop_pct})"
            )
        if self.wallets_loser_min_signals < 0:
            raise ValueError(
                f"[wallets].loser_min_signals debe ser >= 0 ({self.wallets_loser_min_signals})"
            )
        for name in ("min_pair_age_minutes", "max_price_change_h1_pct", "min_market_cap_usd"):
            if getattr(self, f"wallets_{name}") < 0:
                raise ValueError(f"[wallets].{name} debe ser >= 0 ({getattr(self, f'wallets_{name}')})")
        if not self.wallets_file:
            raise ValueError("[wallets].file no puede estar vacío")
        if self.wallets_full_poll_minutes <= 0:
            raise ValueError(
                f"[wallets].full_poll_minutes debe ser > 0 ({self.wallets_full_poll_minutes})"
            )
        if self.wallets_interval_seconds <= 0:
            raise ValueError(
                f"[wallets].interval_seconds debe ser > 0 ({self.wallets_interval_seconds})"
            )
        for name in (
            "wallets_min_sol",
            "wallets_cooldown_minutes",
            "wallets_confluence_minutes",
            "wallets_token_cooldown_minutes",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} no puede ser negativo ({getattr(self, name)})")

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        """Lee config.toml y arma un Config, usando los defaults del dataclass
        para cualquier clave ausente en el archivo (config.toml no necesita
        tener todas las secciones/claves)."""
        if not path.is_file():
            raise FileNotFoundError(f"Archivo de configuración no encontrado: {path}")

        with open(path, "rb") as f:
            raw = tomllib.load(f)

        # Cada sección del TOML se mapea a un grupo de campos de Config.
        unknown_sections = sorted(set(raw) - set(_KNOWN_KEYS))
        if unknown_sections:
            log.warning("Secciones desconocidas en el TOML, se ignoran: %s", ", ".join(unknown_sections))
        radar = _section(raw, "radar")
        dexscreener = _section(raw, "dexscreener")
        solana = _section(raw, "solana")
        wallets = _section(raw, "wallets")
        telegram = _section(raw, "telegram")

        chat_id = telegram.get("chat_id", "")
        # Los chat_id son números (-100123...) y es natural escribirlos sin
        # comillas; Telegram acepta ambos, así que se normaliza a str.
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            chat_id = str(chat_id)

        return cls(
            chain_id=dexscreener.get("chain_id", "solana"),
            quote_tokens=dexscreener.get("quote_tokens", []),
            dex_ids=dexscreener.get("dex_ids", []),
            check_token_authorities=solana.get("check_token_authorities", True),
            solana_rpc_url=solana.get("rpc_url", MAINNET_RPC_URL),
            alert_log_path=radar.get("alert_log_path", "alertas.db"),
            wallets_enabled=wallets.get("enabled", True),
            wallets_file=wallets.get("file", "wallets.txt"),
            wallets_interval_seconds=wallets.get("interval_seconds", 15.0),
            wallets_min_sol=wallets.get("min_sol", 0.1),
            wallets_cooldown_minutes=wallets.get("cooldown_minutes", 60.0),
            wallets_confluence_minutes=wallets.get("confluence_minutes", 60.0),
            wallets_min_wallets=wallets.get("min_wallets", 1),
            wallets_token_cooldown_minutes=wallets.get("token_cooldown_minutes", 0.0),
            wallets_realert_new_wallets=wallets.get("realert_new_wallets", 2),
            wallets_max_tokens_per_hour=wallets.get("max_tokens_per_hour", 0),
            wallets_max_txs_per_hour=wallets.get("max_txs_per_hour", 0),
            wallets_max_inactive_days=wallets.get("max_inactive_days", 30.0),
            wallets_blacklist_file=wallets.get("blacklist_file", ""),
            wallets_rug_drop_pct=wallets.get("rug_drop_pct", 90.0),
            wallets_loser_min_signals=wallets.get("loser_min_signals", 0),
            wallets_loser_max_median_ret_pct=wallets.get("loser_max_median_ret_pct", -30.0),
            wallets_min_pair_age_minutes=wallets.get("min_pair_age_minutes", 0.0),
            wallets_max_price_change_h1_pct=wallets.get("max_price_change_h1_pct", 0.0),
            wallets_min_market_cap_usd=wallets.get("min_market_cap_usd", 0.0),
            wallets_websocket=wallets.get("websocket", True),
            wallets_ws_url=wallets.get("ws_url", ""),
            wallets_full_poll_minutes=wallets.get("full_poll_minutes", 10.0),
            verbose=radar.get("verbose", False),
            telegram_bot_token=telegram.get("bot_token", ""),
            telegram_chat_id=chat_id,
            telegram_message_thread_id=telegram.get("message_thread_id"),
        )
