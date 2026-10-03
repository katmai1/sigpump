"""
sigpump/config.py

Carga la configuración desde config.toml hacia dataclasses tipadas y
contiene la función de scoring (score_pair) que puntúa cada par de
mercado para decidir si dispara una alerta.
"""

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from sigpump.signals import buy_ratio_m5, volume_acceleration
from sigpump.solana import MAINNET_RPC_URL
from sigpump.util import normalize_address, to_float

log = logging.getLogger()

# Claves válidas por sección del TOML. scoring_weights no figura porque la
# valida ScoringWeights.from_raw (con error, no aviso).
_KNOWN_KEYS: dict[str, set[str]] = {
    "dexscreener": {
        "chain_id",
        "min_liquidity_usd",
        "min_volume_h1_usd",
        "min_market_cap_usd",
        "min_pair_age_minutes",
        "min_txns_h1",
        "min_sell_ratio_h1",
        "max_avg_trade_usd",
        "max_price_change_h1_pct",
        "quote_tokens",
        "min_txns_m5",
        "min_volume_m5_usd",
    },
    "geckoterminal": {
        "trending_pages",
        "verify_before_alert",
        "max_price_deviation_pct",
        "max_candle_drop_pct",
        "max_rise_from_low_pct",
        "max_drop_from_recent_high_pct",
    },
    "radar": {
        "alerts_enabled",
        "poll_interval_seconds",
        "alert_cooldown_minutes",
        "score_alert_threshold",
        "top_n_candidates",
        "alert_log_path",
        "verbose",
    },
    "scoring": {"late_penalty_start_h1_pct", "late_penalty_end_h1_pct"},
    "solana": {"check_token_authorities", "rpc_url"},
    "watch": {
        "enabled",
        "interval_seconds",
        "max_tokens",
        "min_price_move_pct",
        "max_price_move_pct",
        "min_volume_ratio",
        "min_txns_ratio",
        "min_buy_ratio",
        "cooldown_minutes",
        "suppress_alert_minutes",
        "require_sustained_seconds",
    },
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
        "blacklist_file",
        "rug_drop_pct",
    },
    "telegram": {"bot_token", "chat_id", "message_thread_id"},
}

_NUMBER = (int, float)
# Campo de Config -> (nombre en el TOML, tipos aceptados).
_FIELD_TYPES: dict[str, tuple[str, tuple[type, ...]]] = {
    "chain_id": ("[dexscreener].chain_id", (str,)),
    "alerts_enabled": ("[radar].alerts_enabled", (bool,)),
    "poll_interval_seconds": ("[radar].poll_interval_seconds", _NUMBER),
    "alert_cooldown_minutes": ("[radar].alert_cooldown_minutes", _NUMBER),
    "min_liquidity_usd": ("[dexscreener].min_liquidity_usd", _NUMBER),
    "min_volume_h1_usd": ("[dexscreener].min_volume_h1_usd", _NUMBER),
    "min_market_cap_usd": ("[dexscreener].min_market_cap_usd", _NUMBER),
    "min_pair_age_minutes": ("[dexscreener].min_pair_age_minutes", _NUMBER),
    "min_txns_h1": ("[dexscreener].min_txns_h1", _NUMBER),
    "min_sell_ratio_h1": ("[dexscreener].min_sell_ratio_h1", _NUMBER),
    "max_avg_trade_usd": ("[dexscreener].max_avg_trade_usd", _NUMBER),
    "max_price_change_h1_pct": ("[dexscreener].max_price_change_h1_pct", _NUMBER),
    "quote_tokens": ("[dexscreener].quote_tokens", (list,)),
    "min_txns_m5": ("[dexscreener].min_txns_m5", _NUMBER),
    "min_volume_m5_usd": ("[dexscreener].min_volume_m5_usd", _NUMBER),
    "check_token_authorities": ("[solana].check_token_authorities", (bool,)),
    "solana_rpc_url": ("[solana].rpc_url", (str,)),
    "score_alert_threshold": ("[radar].score_alert_threshold", _NUMBER),
    # Se usan como índice de slice y en range(): tienen que ser enteros.
    "top_n_candidates": ("[radar].top_n_candidates", (int,)),
    "geckoterminal_pages": ("[geckoterminal].trending_pages", (int,)),
    "verify_before_alert": ("[geckoterminal].verify_before_alert", (bool,)),
    "max_price_deviation_pct": ("[geckoterminal].max_price_deviation_pct", _NUMBER),
    "max_candle_drop_pct": ("[geckoterminal].max_candle_drop_pct", _NUMBER),
    "max_rise_from_low_pct": ("[geckoterminal].max_rise_from_low_pct", _NUMBER),
    "max_drop_from_recent_high_pct": ("[geckoterminal].max_drop_from_recent_high_pct", _NUMBER),
    "late_penalty_start_h1_pct": ("[scoring].late_penalty_start_h1_pct", _NUMBER),
    "late_penalty_end_h1_pct": ("[scoring].late_penalty_end_h1_pct", _NUMBER),
    "alert_log_path": ("[radar].alert_log_path", (str,)),
    "watch_enabled": ("[watch].enabled", (bool,)),
    "watch_interval_seconds": ("[watch].interval_seconds", _NUMBER),
    # Se usa como índice de slice: tiene que ser entero.
    "watch_max_tokens": ("[watch].max_tokens", (int,)),
    "early_min_price_move_pct": ("[watch].min_price_move_pct", _NUMBER),
    "early_max_price_move_pct": ("[watch].max_price_move_pct", _NUMBER),
    "early_min_volume_ratio": ("[watch].min_volume_ratio", _NUMBER),
    "early_min_txns_ratio": ("[watch].min_txns_ratio", _NUMBER),
    "early_min_buy_ratio": ("[watch].min_buy_ratio", _NUMBER),
    "early_cooldown_minutes": ("[watch].cooldown_minutes", _NUMBER),
    "early_suppress_alert_minutes": ("[watch].suppress_alert_minutes", _NUMBER),
    "early_require_sustained_seconds": ("[watch].require_sustained_seconds", _NUMBER),
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
    "wallets_blacklist_file": ("[wallets].blacklist_file", (str,)),
    "wallets_rug_drop_pct": ("[wallets].rug_drop_pct", _NUMBER),
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
class ScoringWeights:
    """
    Pesos relativos (deben sumar ~1.0) de cada componente del score final
    calculado en score_pair(). Configurables vía [scoring_weights] en el TOML.
    """
    volume_h1: float = 0.20
    # Aceleración y presión compradora de los últimos 5 min: son las que
    # detectan un movimiento que empieza, en vez de uno que ya ocurrió.
    volume_acceleration: float = 0.25
    buy_pressure: float = 0.10
    price_change_h1: float = 0.20
    price_change_h6: float = 0.05
    liquidity: float = 0.10
    boosted: float = 0.10

    @property
    def total(self) -> float:
        """Suma de todos los pesos; define el score máximo alcanzable."""
        return sum(getattr(self, f.name) for f in fields(self))

    @classmethod
    def from_raw(cls, raw: dict) -> "ScoringWeights":
        """Construye los pesos desde [scoring_weights], validando las claves.

        Sin esta validación una clave mal escrita en el TOML terminaba en un
        `TypeError: unexpected keyword argument` sin contexto útil."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"Claves desconocidas en [scoring_weights]: {', '.join(unknown)}. "
                f"Válidas: {', '.join(sorted(known))}"
            )
        for key, value in raw.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"[scoring_weights].{key} debe ser un número, no {value!r}")
            if value < 0:
                raise ValueError(f"[scoring_weights].{key} no puede ser negativo ({value})")

        weights = cls(**raw)
        # Los pesos definen el score máximo: si suman 0.5, ningún token puede
        # pasar de 50 y un umbral de 70 no dispara jamás. Es un error silencioso
        # difícil de diagnosticar, así que al menos se avisa.
        if abs(weights.total - 1.0) > 0.01:
            log.warning(
                "Los pesos de [scoring_weights] suman %.2f en vez de 1.0: "
                "el score máximo posible es %.1f, no 100",
                weights.total,
                weights.total * 100,
            )
        return weights


@dataclass
class Config:
    """Configuración completa del radar, con defaults sensatos si el TOML no los define."""
    chain_id: str = "solana"
    # Alertas completas (las de la pasada con score y velas). Las prealertas
    # se apagan con watch_enabled.
    alerts_enabled: bool = True
    poll_interval_seconds: int = 90
    alert_cooldown_minutes: int = 60
    min_liquidity_usd: float = 5_000.0
    min_volume_h1_usd: float = 2_000.0
    min_market_cap_usd: float = 0.0
    min_pair_age_minutes: float = 60.0
    min_txns_h1: float = 50
    min_sell_ratio_h1: float = 0.1
    max_avg_trade_usd: float = 5_000.0
    max_price_change_h1_pct: float = 1_000.0
    # Monedas contra las que debe cotizar el par (símbolo o dirección del
    # quote). Vacío = cualquiera.
    quote_tokens: list[str] = field(default_factory=list)
    # Actividad mínima de los últimos 5 minutos: por debajo, el arranque es de
    # dos operaciones sueltas.
    min_txns_m5: float = 0.0
    min_volume_m5_usd: float = 0.0
    score_alert_threshold: float = 70.0
    top_n_candidates: int = 200
    geckoterminal_pages: int = 5
    verify_before_alert: bool = True
    max_price_deviation_pct: float = 50.0
    max_candle_drop_pct: float = 30.0
    max_rise_from_low_pct: float = 150.0
    max_drop_from_recent_high_pct: float = 20.0
    late_penalty_start_h1_pct: float = 60.0
    late_penalty_end_h1_pct: float = 250.0
    alert_log_path: str = "alertas.db"
    watch_enabled: bool = True
    # DexScreener refresca sus datos cada ~30 s: consultar más seguido no aporta.
    watch_interval_seconds: float = 30.0
    watch_max_tokens: int = 90
    early_min_price_move_pct: float = 4.0
    early_max_price_move_pct: float = 30.0
    early_min_volume_ratio: float = 2.5
    early_min_txns_ratio: float = 2.0
    early_min_buy_ratio: float = 0.55
    # Las prealertas repetidas del mismo token rendían mucho peor que la
    # primera: una cada 6 horas.
    early_cooldown_minutes: float = 360.0
    # Alertas completas de tokens con prealerta reciente: llegaban con la
    # subida ya hecha. 0 = mandarlas igual.
    early_suppress_alert_minutes: float = 360.0
    # Segundos que el arranque tiene que seguir cumpliéndose (con datos nuevos)
    # antes de avisar. Los que no se sostenían rendían bastante peor.
    early_require_sustained_seconds: float = 30.0
    # Seguimiento de wallets: avisa cuando una de las del fichero entra en un token.
    wallets_enabled: bool = False
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
    # avisa ni cuenta para la confluencia de otras. 0 = sin tope.
    wallets_max_tokens_per_hour: int = 0
    # Lista negra: sus wallets se quitan del fichero y no se vuelven a seguir.
    # Vacío = sin lista negra.
    wallets_blacklist_file: str = ""
    # Una wallet cuya compra cae este % (rug) en el seguimiento entra en la
    # lista negra sola. 0 = no hacerlo.
    wallets_rug_drop_pct: float = 90.0
    verbose: bool = False
    # Comprobación en la blockchain de que el token no se pueda acuñar ni congelar.
    check_token_authorities: bool = True
    solana_rpc_url: str = MAINNET_RPC_URL
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int | None = None

    def __post_init__(self) -> None:
        """Valida los rangos apenas se construye el Config, para que un TOML
        inconsistente falle al arrancar y no a mitad de una pasada."""
        # Tipos primero: con `poll_interval_seconds = "90"` la comparación de
        # abajo tiraba un TypeError crudo en vez de un error de configuración.
        for name, (toml_name, types) in _FIELD_TYPES.items():
            value = getattr(self, name)
            # bool es subclase de int: `top_n_candidates = true` no es un número.
            if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
                raise ValueError(f"{toml_name} tiene un tipo inválido: {value!r}")
        if not self.chain_id:
            raise ValueError("[dexscreener].chain_id no puede estar vacío")
        if not all(isinstance(quote, str) and quote for quote in self.quote_tokens):
            raise ValueError(
                f"[dexscreener].quote_tokens debe ser una lista de textos ({self.quote_tokens!r})"
            )
        if self.check_token_authorities and not self.solana_rpc_url:
            raise ValueError("[solana].rpc_url no puede estar vacío con check_token_authorities")
        if not (self.alerts_enabled or self.watch_enabled or self.wallets_enabled):
            raise ValueError(
                "No hay nada que avisar: [radar].alerts_enabled, [watch].enabled y "
                "[wallets].enabled están todos desactivados"
            )
        if self.wallets_enabled and not self.solana_rpc_url:
            raise ValueError("[solana].rpc_url no puede estar vacío con [wallets].enabled")
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
        if not 0 <= self.wallets_rug_drop_pct <= 100:
            raise ValueError(
                f"[wallets].rug_drop_pct debe estar entre 0 y 100 ({self.wallets_rug_drop_pct})"
            )
        if self.wallets_enabled and not self.wallets_file:
            raise ValueError("[wallets].file no puede estar vacío con [wallets].enabled")
        if self.wallets_interval_seconds <= 0:
            raise ValueError(
                f"[wallets].interval_seconds debe ser > 0 ({self.wallets_interval_seconds})"
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"[radar].poll_interval_seconds debe ser > 0 ({self.poll_interval_seconds})"
            )
        if self.top_n_candidates <= 0:
            raise ValueError(
                f"[radar].top_n_candidates debe ser > 0 ({self.top_n_candidates})"
            )
        if not 0 <= self.geckoterminal_pages <= 10:
            raise ValueError(
                f"[geckoterminal].trending_pages debe estar entre 0 y 10 "
                f"({self.geckoterminal_pages})"
            )
        if not 0 <= self.score_alert_threshold <= 100:
            raise ValueError(
                f"[radar].score_alert_threshold debe estar entre 0 y 100 "
                f"({self.score_alert_threshold})"
            )
        if not 0 <= self.min_sell_ratio_h1 <= 1:
            raise ValueError(
                f"[dexscreener].min_sell_ratio_h1 debe estar entre 0 y 1 "
                f"({self.min_sell_ratio_h1})"
            )
        if not 0 <= self.max_candle_drop_pct <= 100:
            raise ValueError(
                f"[geckoterminal].max_candle_drop_pct debe estar entre 0 y 100 "
                f"({self.max_candle_drop_pct})"
            )
        if not 0 <= self.max_drop_from_recent_high_pct <= 100:
            raise ValueError(
                f"[geckoterminal].max_drop_from_recent_high_pct debe estar entre 0 y 100 "
                f"({self.max_drop_from_recent_high_pct})"
            )
        if 0 < self.late_penalty_start_h1_pct and (
            self.late_penalty_end_h1_pct <= self.late_penalty_start_h1_pct
        ):
            raise ValueError(
                f"[scoring].late_penalty_end_h1_pct ({self.late_penalty_end_h1_pct}) debe "
                f"ser mayor que late_penalty_start_h1_pct ({self.late_penalty_start_h1_pct})"
            )
        if self.watch_interval_seconds <= 0:
            raise ValueError(
                f"[watch].interval_seconds debe ser > 0 ({self.watch_interval_seconds})"
            )
        if self.watch_max_tokens <= 0:
            raise ValueError(f"[watch].max_tokens debe ser > 0 ({self.watch_max_tokens})")
        if not 0 <= self.early_min_price_move_pct < self.early_max_price_move_pct:
            raise ValueError(
                f"[watch].min_price_move_pct ({self.early_min_price_move_pct}) debe ser >= 0 "
                f"y menor que max_price_move_pct ({self.early_max_price_move_pct})"
            )
        if not 0 <= self.early_min_buy_ratio <= 1:
            raise ValueError(
                f"[watch].min_buy_ratio debe estar entre 0 y 1 ({self.early_min_buy_ratio})"
            )
        for name in (
            "min_txns_m5",
            "min_volume_m5_usd",
            "early_require_sustained_seconds",
            "wallets_min_sol",
            "wallets_cooldown_minutes",
            "wallets_confluence_minutes",
            "wallets_token_cooldown_minutes",
            "early_min_volume_ratio",
            "early_min_txns_ratio",
            "early_cooldown_minutes",
            "early_suppress_alert_minutes",
            "max_rise_from_low_pct",
            "late_penalty_start_h1_pct",
            "late_penalty_end_h1_pct",
            "alert_cooldown_minutes",
            "min_liquidity_usd",
            "min_volume_h1_usd",
            "min_market_cap_usd",
            "min_pair_age_minutes",
            "min_txns_h1",
            "max_avg_trade_usd",
            "max_price_change_h1_pct",
            "max_price_deviation_pct",
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
        unknown_sections = sorted(set(raw) - set(_KNOWN_KEYS) - {"scoring_weights"})
        if unknown_sections:
            log.warning("Secciones desconocidas en el TOML, se ignoran: %s", ", ".join(unknown_sections))
        radar = _section(raw, "radar")
        dexscreener = _section(raw, "dexscreener")
        geckoterminal = _section(raw, "geckoterminal")
        scoring = _section(raw, "scoring")
        solana = _section(raw, "solana")
        watch = _section(raw, "watch")
        wallets = _section(raw, "wallets")
        telegram = _section(raw, "telegram")
        weights_raw = raw.get("scoring_weights", {})
        if not isinstance(weights_raw, dict):
            raise ValueError(f"[scoring_weights] debe ser una sección (tabla), no {weights_raw!r}")

        chat_id = telegram.get("chat_id", "")
        # Los chat_id son números (-100123...) y es natural escribirlos sin
        # comillas; Telegram acepta ambos, así que se normaliza a str.
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            chat_id = str(chat_id)

        return cls(
            chain_id=dexscreener.get("chain_id", "solana"),
            alerts_enabled=radar.get("alerts_enabled", True),
            poll_interval_seconds=radar.get("poll_interval_seconds", 90),
            alert_cooldown_minutes=radar.get("alert_cooldown_minutes", 60),
            min_liquidity_usd=dexscreener.get("min_liquidity_usd", 5_000.0),
            min_volume_h1_usd=dexscreener.get("min_volume_h1_usd", 2_000.0),
            min_market_cap_usd=dexscreener.get("min_market_cap_usd", 0.0),
            min_pair_age_minutes=dexscreener.get("min_pair_age_minutes", 60.0),
            min_txns_h1=dexscreener.get("min_txns_h1", 50),
            min_sell_ratio_h1=dexscreener.get("min_sell_ratio_h1", 0.1),
            max_avg_trade_usd=dexscreener.get("max_avg_trade_usd", 5_000.0),
            max_price_change_h1_pct=dexscreener.get("max_price_change_h1_pct", 1_000.0),
            quote_tokens=dexscreener.get("quote_tokens", []),
            min_txns_m5=dexscreener.get("min_txns_m5", 0.0),
            min_volume_m5_usd=dexscreener.get("min_volume_m5_usd", 0.0),
            check_token_authorities=solana.get("check_token_authorities", True),
            solana_rpc_url=solana.get("rpc_url", MAINNET_RPC_URL),
            score_alert_threshold=radar.get("score_alert_threshold", 70.0),
            top_n_candidates=radar.get("top_n_candidates", 200),
            geckoterminal_pages=geckoterminal.get("trending_pages", 5),
            verify_before_alert=geckoterminal.get("verify_before_alert", True),
            max_price_deviation_pct=geckoterminal.get("max_price_deviation_pct", 50.0),
            max_candle_drop_pct=geckoterminal.get("max_candle_drop_pct", 30.0),
            max_rise_from_low_pct=geckoterminal.get("max_rise_from_low_pct", 150.0),
            max_drop_from_recent_high_pct=geckoterminal.get("max_drop_from_recent_high_pct", 20.0),
            late_penalty_start_h1_pct=scoring.get("late_penalty_start_h1_pct", 60.0),
            late_penalty_end_h1_pct=scoring.get("late_penalty_end_h1_pct", 250.0),
            alert_log_path=radar.get("alert_log_path", "alertas.db"),
            watch_enabled=watch.get("enabled", True),
            watch_interval_seconds=watch.get("interval_seconds", 30.0),
            watch_max_tokens=watch.get("max_tokens", 90),
            early_min_price_move_pct=watch.get("min_price_move_pct", 4.0),
            early_max_price_move_pct=watch.get("max_price_move_pct", 30.0),
            early_min_volume_ratio=watch.get("min_volume_ratio", 2.5),
            early_min_txns_ratio=watch.get("min_txns_ratio", 2.0),
            early_min_buy_ratio=watch.get("min_buy_ratio", 0.55),
            early_cooldown_minutes=watch.get("cooldown_minutes", 360.0),
            early_suppress_alert_minutes=watch.get("suppress_alert_minutes", 360.0),
            early_require_sustained_seconds=watch.get("require_sustained_seconds", 30.0),
            wallets_enabled=wallets.get("enabled", False),
            wallets_file=wallets.get("file", "wallets.txt"),
            wallets_interval_seconds=wallets.get("interval_seconds", 15.0),
            wallets_min_sol=wallets.get("min_sol", 0.1),
            wallets_cooldown_minutes=wallets.get("cooldown_minutes", 60.0),
            wallets_confluence_minutes=wallets.get("confluence_minutes", 60.0),
            wallets_min_wallets=wallets.get("min_wallets", 1),
            wallets_token_cooldown_minutes=wallets.get("token_cooldown_minutes", 0.0),
            wallets_realert_new_wallets=wallets.get("realert_new_wallets", 2),
            wallets_max_tokens_per_hour=wallets.get("max_tokens_per_hour", 0),
            wallets_blacklist_file=wallets.get("blacklist_file", ""),
            wallets_rug_drop_pct=wallets.get("rug_drop_pct", 90.0),
            verbose=radar.get("verbose", False),
            # from_raw solo cubre las claves presentes en el TOML; el resto
            # toma los defaults de ScoringWeights.
            weights=ScoringWeights.from_raw(weights_raw),
            telegram_bot_token=telegram.get("bot_token", ""),
            telegram_chat_id=chat_id,
            telegram_message_thread_id=telegram.get("message_thread_id"),
        )

# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Recorta value al rango [lo, hi]; usado para que cada sub-score quede en 0-100."""
    return max(lo, min(hi, value))


def score_pair(
    pair: dict,
    weights: ScoringWeights,
    boosted_addresses: set[str],
    late_penalty_start_h1_pct: float = 0.0,
    late_penalty_end_h1_pct: float = 0.0,
) -> float:
    """
    Puntaje 0-100 combinando:
      - volumen 1h (normalizado lineal contra un techo razonable)
      - aceleración del volumen y presión compradora de los últimos 5 min
      - momentum de precio 1h y 6h
      - liquidez (más liquidez = menos riesgo de rug/slippage)
      - si el token tiene boost activo (señal de marketing/interés)
    Con late_penalty_start_h1_pct > 0, un cambio de 1h por encima de ese
    valor multiplica el total por un factor que baja linealmente de 1 a 0
    al llegar a late_penalty_end_h1_pct: premiar la subida sin tope hacía
    que las alertas salieran con el movimiento ya hecho.
    """
    # `pair` es el JSON crudo devuelto por DexScreener; los campos anidados
    # pueden venir ausentes, null o como string, de ahí to_float().
    volume_h1 = to_float((pair.get("volume") or {}).get("h1"))
    price_change_h1 = to_float((pair.get("priceChange") or {}).get("h1"))
    price_change_h6 = to_float((pair.get("priceChange") or {}).get("h6"))
    liquidity_usd = to_float((pair.get("liquidity") or {}).get("usd"))
    token_address = normalize_address((pair.get("baseToken") or {}).get("address", ""))
    is_boosted = token_address in boosted_addresses

    # Normalizaciones simples: cada métrica se lleva a una escala 0-100 antes
    # de ponderarla. Los techos (50_000, 100_000, etc.) son heurísticos y
    # conviene ajustarlos según lo que observes en la práctica.
    volume_score = _clamp((volume_h1 / 50_000.0) * 100)
    # x1 (mismo ritmo que la media de la hora) -> 0; x3 -> tope. Un token que
    # ya hizo su subida y está quieto queda por debajo de x1. Solo cuenta con
    # el precio de 5 min subiendo: el volumen también se acelera en un dump.
    price_change_m5 = to_float((pair.get("priceChange") or {}).get("m5"))
    acceleration_score = (
        _clamp((volume_acceleration(pair) - 1) * 50) if price_change_m5 > 0 else 0.0
    )
    # 50% de compras (mercado equilibrado) -> 0; 75% -> tope.
    buy_ratio = buy_ratio_m5(pair)
    buy_pressure_score = _clamp((buy_ratio - 0.5) * 400) if buy_ratio is not None else 0.0
    # El momentum solo premia subidas: 0% o caídas puntúan 0. Antes la curva
    # centraba el 0% en 50/100 y un token plano (o cayendo) sumaba puntos
    # gratis, suficientes para disparar alertas con volumen y liquidez altos.
    price_h1_score = _clamp(price_change_h1 * 2)  # +50% h1 -> tope
    price_h6_score = _clamp(price_change_h6)  # +100% h6 -> tope
    liquidity_score = _clamp((liquidity_usd / 100_000.0) * 100)
    boost_score = 100.0 if is_boosted else 0.0

    # Combinación lineal ponderada de los sub-scores; el resultado ya está
    # en 0-100 porque cada sub-score lo está y los pesos suman 1.0.
    total = (
        volume_score * weights.volume_h1
        + acceleration_score * weights.volume_acceleration
        + buy_pressure_score * weights.buy_pressure
        + price_h1_score * weights.price_change_h1
        + price_h6_score * weights.price_change_h6
        + liquidity_score * weights.liquidity
        + boost_score * weights.boosted
    )
    if late_penalty_end_h1_pct > late_penalty_start_h1_pct > 0:
        excess = price_change_h1 - late_penalty_start_h1_pct
        span = late_penalty_end_h1_pct - late_penalty_start_h1_pct
        total *= _clamp(1 - excess / span, 0.0, 1.0)
    return round(_clamp(total), 1)
