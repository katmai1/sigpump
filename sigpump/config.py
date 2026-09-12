"""
sigpump/config.py

Carga la configuración desde config.toml hacia dataclasses tipadas y
contiene la función de scoring (score_pair) que puntúa cada par de
mercado para decidir si dispara una alerta.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScoringWeights:
    """
    Pesos relativos (deben sumar ~1.0) de cada componente del score final
    calculado en score_pair(). Configurables vía [scoring_weights] en el TOML.
    """
    volume_h1: float = 0.35
    price_change_h1: float = 0.25
    price_change_h6: float = 0.15
    liquidity: float = 0.15
    boosted: float = 0.10

@dataclass
class Config:
    """Configuración completa del radar, con defaults sensatos si el TOML no los define."""
    chain_id: str = "solana"
    poll_interval_seconds: int = 90
    alert_cooldown_minutes: int = 60
    min_liquidity_usd: float = 5_000.0
    min_volume_h1_usd: float = 2_000.0
    score_alert_threshold: float = 70.0
    top_n_candidates: int = 40
    verbose: bool = False
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int | None = None

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
        radar = raw.get("radar", {})
        dexscreener = raw.get("dexscreener", {})
        telegram = raw.get("telegram", {})
        weights_raw = raw.get("scoring_weights", {})

        return cls(
            chain_id=dexscreener.get("chain_id", "solana"),
            poll_interval_seconds=radar.get("poll_interval_seconds", 90),
            alert_cooldown_minutes=radar.get("alert_cooldown_minutes", 60),
            min_liquidity_usd=dexscreener.get("min_liquidity_usd", 5_000.0),
            min_volume_h1_usd=dexscreener.get("min_volume_h1_usd", 2_000.0),
            score_alert_threshold=radar.get("score_alert_threshold", 70.0),
            top_n_candidates=radar.get("top_n_candidates", 40),
            verbose=radar.get("verbose", False),
            # **weights_raw solo cubre las claves presentes en el TOML; el resto
            # toma los defaults de ScoringWeights.
            weights=ScoringWeights(**weights_raw) if weights_raw else ScoringWeights(),
            telegram_bot_token=telegram.get("bot_token", ""),
            telegram_chat_id=telegram.get("chat_id", ""),
            telegram_message_thread_id=telegram.get("message_thread_id"),
        )

# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Recorta value al rango [lo, hi]; usado para que cada sub-score quede en 0-100."""
    return max(lo, min(hi, value))


def score_pair(pair: dict, weights: ScoringWeights, boosted_addresses: set[str]) -> float:
    """
    Puntaje 0-100 combinando:
      - volumen 1h (normalizado log-scale contra un techo razonable)
      - momentum de precio 1h y 6h
      - liquidez (más liquidez = menos riesgo de rug/slippage)
      - si el token tiene boost activo (señal de marketing/interés)
    """
    # `pair` es el JSON crudo devuelto por DexScreener; los campos anidados
    # pueden venir ausentes o null, de ahí el `or {}` / `or 0.0` defensivo.
    volume_h1 = float((pair.get("volume") or {}).get("h1") or 0.0)
    price_change_h1 = float((pair.get("priceChange") or {}).get("h1") or 0.0)
    price_change_h6 = float((pair.get("priceChange") or {}).get("h6") or 0.0)
    liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
    token_address = (pair.get("baseToken") or {}).get("address", "")
    is_boosted = token_address in boosted_addresses

    # Normalizaciones simples: cada métrica se lleva a una escala 0-100 antes
    # de ponderarla. Los techos (50_000, 100_000, etc.) son heurísticos y
    # conviene ajustarlos según lo que observes en la práctica.
    volume_score = _clamp((volume_h1 / 50_000.0) * 100)
    price_h1_score = _clamp(50 + price_change_h1)  # +50% h1 -> tope
    price_h6_score = _clamp(50 + price_change_h6 / 2)
    liquidity_score = _clamp((liquidity_usd / 100_000.0) * 100)
    boost_score = 100.0 if is_boosted else 0.0

    # Combinación lineal ponderada de los sub-scores; el resultado ya está
    # en 0-100 porque cada sub-score lo está y los pesos suman 1.0.
    total = (
        volume_score * weights.volume_h1
        + price_h1_score * weights.price_change_h1
        + price_h6_score * weights.price_change_h6
        + liquidity_score * weights.liquidity
        + boost_score * weights.boosted
    )
    return round(_clamp(total), 1)

