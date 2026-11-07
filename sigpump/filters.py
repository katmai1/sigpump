"""
sigpump/filters.py

Filtros del par para los avisos de wallets, en una tabla: cada uno con la
clave de config que lo controla y si se aplica a las compras en bonding
curve. Es la única fuente de ese subconjunto; el README, config.example.toml
y config.py lo describen, y tests/test_filters.py comprueba que digan lo
mismo (antes se desincronizaron: min_volume_h1_usd se aplicaba a las wallets
de confianza, que ya no existen, sin que lo dijera ninguno).
"""

import time
from dataclasses import dataclass
from typing import Callable

from sigpump.config import Config
from sigpump.util import market_cap_usd, normalize_address, to_float, txns_m5


def matches_quote(pair: dict, wanted: list[str]) -> bool:
    """True si el par cotiza contra una de las monedas pedidas, comparando por
    símbolo o por dirección del quote."""
    quote = pair.get("quoteToken") or {}
    symbol = str(quote.get("symbol") or "").upper()
    address = str(normalize_address(quote.get("address") or ""))
    return any(str(w).upper() == symbol or str(normalize_address(w)) == address for w in wanted)


def _dex(cfg: Config, pair: dict, now: float) -> str | None:
    if cfg.dex_ids:
        dex_id = str(pair.get("dexId") or "")
        if dex_id.lower() not in {d.lower() for d in cfg.dex_ids}:
            return f"par en {dex_id or '?'}, no en {' o '.join(cfg.dex_ids)}"
    return None


def _quote(cfg: Config, pair: dict, now: float) -> str | None:
    if cfg.quote_tokens and not matches_quote(pair, cfg.quote_tokens):
        quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
        return f"par contra {quote}, no contra {' o '.join(cfg.quote_tokens)}"
    return None


def _age(cfg: Config, pair: dict, now: float) -> str | None:
    created_at = to_float(pair.get("pairCreatedAt"))
    # Sin pairCreatedAt no se puede evaluar la edad: no se descarta por ella.
    if cfg.wallets_min_pair_age_minutes and created_at > 0:
        age_minutes = (now - created_at / 1000) / 60
        if age_minutes < cfg.wallets_min_pair_age_minutes:
            return f"par creado hace {age_minutes:.0f} min"
    return None


def _change_h1(cfg: Config, pair: dict, now: float) -> str | None:
    change = to_float((pair.get("priceChange") or {}).get("h1"))
    if cfg.wallets_max_price_change_h1_pct and change > cfg.wallets_max_price_change_h1_pct:
        return f"cambio 1h {change:+,.0f}% > {cfg.wallets_max_price_change_h1_pct:,.0f}%"
    return None


def _change_m5(cfg: Config, pair: dict, now: float) -> str | None:
    change = to_float((pair.get("priceChange") or {}).get("m5"))
    if cfg.wallets_max_price_change_m5_pct and change > cfg.wallets_max_price_change_m5_pct:
        return f"cambio 5m {change:+,.0f}% > {cfg.wallets_max_price_change_m5_pct:,.0f}%"
    return None


def _market_cap(cfg: Config, pair: dict, now: float) -> str | None:
    market_cap = market_cap_usd(pair)
    if market_cap < cfg.wallets_min_market_cap_usd:
        return f"market cap ${market_cap:,.0f} < ${cfg.wallets_min_market_cap_usd:,.0f}"
    return None


def _txns(cfg: Config, pair: dict, now: float) -> str | None:
    # Sin txns no se puede evaluar la actividad: no se descarta por ella.
    if cfg.wallets_min_txns_m5 and isinstance((pair.get("txns") or {}).get("m5"), dict):
        count = int(txns_m5(pair))
        if count < cfg.wallets_min_txns_m5:
            return f"{count} txns en 5 min < {cfg.wallets_min_txns_m5}"
    return None


def _volume(cfg: Config, pair: dict, now: float) -> str | None:
    # Sin volume no se puede evaluar la actividad: no se descarta por ella.
    volume_h1 = (pair.get("volume") or {}).get("h1")
    if cfg.wallets_min_volume_h1_usd and volume_h1 is not None:
        if to_float(volume_h1) < cfg.wallets_min_volume_h1_usd:
            return f"volumen 1h ${to_float(volume_h1):,.0f} < ${cfg.wallets_min_volume_h1_usd:,.0f}"
    return None


def _turnover(cfg: Config, pair: dict, now: float) -> str | None:
    volume_h1 = (pair.get("volume") or {}).get("h1")
    market_cap = market_cap_usd(pair)
    if cfg.wallets_max_turnover_h1 and volume_h1 is not None and market_cap > 0:
        turnover = to_float(volume_h1) / market_cap
        if turnover > cfg.wallets_max_turnover_h1:
            return f"volumen 1h {turnover:.0%} del market cap > {cfg.wallets_max_turnover_h1:.0%}"
    return None


@dataclass(frozen=True)
class PairFilter:
    """Un filtro del par: la clave de config que lo controla, la función que
    da el motivo de descarte (None si pasa) y a qué compras se aplica."""
    key: str
    check: Callable[[Config, dict, float], str | None]
    # Compras de tokens que siguen en la bonding curve de un launchpad.
    curve: bool


# En orden: el motivo de descarte es el del primero que falla.
#
# Bonding curve: un token sin graduar incumple siempre el dex, la edad, el
# market cap y el turnover.
#
# Hasta el 2026-11-07 las wallets de confianza (* en el fichero) se saltaban
# el cambio 1h, el market cap, min_txns_m5 y el turnover. Se quitaron a
# mano: desde el 17-oct eso dejaba pasar 45 avisos (de 724), que daban +2%
# de mediana a 1 h y -2% a 24 h (todos los avisos, +1% y -8%).
PAIR_FILTERS: tuple[PairFilter, ...] = (
    PairFilter("dex_ids", _dex, curve=False),
    PairFilter("quote_tokens", _quote, curve=True),
    PairFilter("min_pair_age_minutes", _age, curve=False),
    PairFilter("max_price_change_h1_pct", _change_h1, curve=True),
    PairFilter("max_price_change_m5_pct", _change_m5, curve=True),
    PairFilter("min_market_cap_usd", _market_cap, curve=False),
    PairFilter("min_txns_m5", _txns, curve=True),
    PairFilter("min_volume_h1_usd", _volume, curve=True),
    PairFilter("max_turnover_h1", _turnover, curve=False),
)


def venue_reason(cfg: Config, pair: dict) -> str | None:
    """Motivo para descartar un par por dónde cotiza ([dexscreener].dex_ids
    y quote_tokens), o None si pasa."""
    return _dex(cfg, pair, 0.0) or _quote(cfg, pair, 0.0)


def first_failing(
    cfg: Config, pair: dict, now: float | None = None, curve: bool = False
) -> tuple[str, str] | None:
    """(clave, motivo) del primer filtro del par que descarta la compra, o
    None si pasa. `now` es la hora contra la que se mide la edad del par
    (ahora si no se da). Con `curve` (token en bonding curve) solo se
    aplican los filtros de PAIR_FILTERS que lo permiten."""
    now = time.time() if now is None else now
    for f in PAIR_FILTERS:
        if curve and not f.curve:
            continue
        if reason := f.check(cfg, pair, now):
            return f.key, reason
    return None


def pair_rejection(
    cfg: Config, pair: dict, now: float | None = None, curve: bool = False
) -> str | None:
    """Motivo de first_failing(), sin la clave del filtro."""
    failing = first_failing(cfg, pair, now, curve)
    return failing[1] if failing else None
