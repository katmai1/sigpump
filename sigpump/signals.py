"""
sigpump/signals.py

Métricas derivadas de los datos de mercado que sirven para distinguir un
token que empieza a moverse de uno que ya hizo la subida: aceleración del
volumen y presión compradora de los últimos 5 minutos (DexScreener) y la
posición del precio respecto del mínimo y del máximo reciente (velas de
GeckoTerminal). Las usan el scoring, la verificación, la alerta y el registro.
"""

from dataclasses import dataclass

from sigpump.util import to_float

# Por debajo de esto las compras/ventas de 5 min son ruido: 3 compras y
# 1 venta dan 75% sin decir nada del mercado.
MIN_TXNS_M5 = 5
# Ventana del "máximo reciente": si el precio ya cae bastante desde el pico
# de estos últimos minutos, la subida probablemente terminó.
RECENT_HIGH_MINUTES = 15


def volume_acceleration(pair: dict) -> float:
    """
    Ritmo del volumen de los últimos 5 min comparado con el de la última
    hora: volumen 5m × 12 / volumen 1h. x1 es el mismo ritmo que la media de
    la hora; x3 es que en estos 5 min se opera el triple. Como la hora
    incluye los últimos 5 min, el máximo posible es x12. 0.0 sin volumen 1h.
    """
    volume = pair.get("volume") or {}
    volume_h1 = to_float(volume.get("h1"))
    if volume_h1 <= 0:
        return 0.0
    return to_float(volume.get("m5")) * 12 / volume_h1


def buy_ratio_m5(pair: dict) -> float | None:
    """Fracción de compras sobre el total de txns de los últimos 5 min (0-1).
    None si hubo menos de MIN_TXNS_M5 txns, porque con tan pocas no dice nada."""
    txns_m5 = (pair.get("txns") or {}).get("m5")
    if not isinstance(txns_m5, dict):
        return None
    buys = to_float(txns_m5.get("buys"))
    total = buys + to_float(txns_m5.get("sells"))
    if total < MIN_TXNS_M5:
        return None
    return buys / total


@dataclass(frozen=True)
class EarlySignal:
    """Arranque de un pool detectado contra su propia historia reciente."""
    # Precio actual frente a la mediana de la base, en %.
    price_move_pct: float
    # Volumen y txns de los últimos 5 min frente a la mediana de la base.
    volume_ratio: float
    txns_ratio: float
    # Fracción de compras en las txns de los últimos 5 min (0-1).
    buy_ratio: float
    # Antigüedad de la foto más vieja de la base, en minutos.
    baseline_minutes: float


@dataclass(frozen=True)
class CandleStats:
    """Qué había pasado en las velas hasta la señal: dónde está el último
    precio y cómo venía la subida."""
    # Cuánto está el último precio por encima del mínimo de toda la ventana, en %.
    rise_from_low_pct: float
    # Cuánto está por debajo del máximo de los últimos RECENT_HIGH_MINUTES, en %.
    drop_from_recent_high_pct: float
    # Cuánto está por encima del mínimo de los últimos RISE_WINDOW_MINUTES, en %.
    rise_15m_pct: float = 0.0
    # Velas alcistas seguidas al final (cierre > apertura).
    green_streak: int = 0
    # Volumen de las TREND_CANDLES últimas velas sobre el de las anteriores.
    # Alto: el volumen acaba de explotar (típico de un mini pump); cerca de 1:
    # venía sostenido. None si las velas no traen volumen.
    volume_trend: float | None = None
    # Mecha superior media de las TREND_CANDLES últimas velas, como fracción
    # de su rango (0-1).
    upper_wick: float = 0.0


# Ventana de la subida reciente, en minutos.
RISE_WINDOW_MINUTES = 15
# Velas que se comparan para la tendencia del volumen y la mecha.
TREND_CANDLES = 3


def candle_stats(candles: list[tuple[float, ...]], until: float | None = None) -> CandleStats | None:
    """
    CandleStats de velas (timestamp, open, high, low, close[, volume])
    ordenadas de la más vieja a la más nueva, tomando como precio actual el
    cierre de la última. Con `until`, solo cuentan las velas ya cerradas en
    ese momento: lo que se sabía al dar la señal. Las ventanas se miden por
    timestamp y no por cantidad de velas porque GeckoTerminal omite los
    minutos sin trades. None sin velas o sin precios válidos.
    """
    if until is not None:
        candles = [c for c in candles if c[0] + 60 <= until]
    if not candles:
        return None
    last_ts, close = candles[-1][0], candles[-1][4]
    lows = [c[3] for c in candles if c[3] > 0]
    recent_highs = [
        c[2] for c in candles if c[0] >= last_ts - RECENT_HIGH_MINUTES * 60 and c[2] > 0
    ]
    recent_lows = [
        c[3] for c in candles if c[0] >= last_ts - RISE_WINDOW_MINUTES * 60 and c[3] > 0
    ]
    if close <= 0 or not lows or not recent_highs:
        return None

    green_streak = 0
    for candle in reversed(candles):
        if candle[4] <= candle[1]:
            break
        green_streak += 1

    last = candles[-TREND_CANDLES:]
    previous = candles[-2 * TREND_CANDLES:-TREND_CANDLES]
    volume_trend = None
    if len(previous) == TREND_CANDLES and all(len(c) > 5 for c in last + previous):
        previous_volume = sum(c[5] for c in previous)
        if previous_volume > 0:
            volume_trend = sum(c[5] for c in last) / previous_volume
    wicks = [(c[2] - max(c[1], c[4])) / (c[2] - c[3]) for c in last if c[2] > c[3]]

    return CandleStats(
        rise_from_low_pct=max(0.0, (close / min(lows) - 1) * 100),
        drop_from_recent_high_pct=max(0.0, (1 - close / max(recent_highs)) * 100),
        rise_15m_pct=max(0.0, (close / min(recent_lows) - 1) * 100) if recent_lows else 0.0,
        green_streak=green_streak,
        volume_trend=volume_trend,
        upper_wick=sum(wicks) / len(wicks) if wicks else 0.0,
    )
