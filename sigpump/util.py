"""
sigpump/util.py

Helpers compartidos entre los módulos. La API de DexScreener devuelve los
campos numéricos como número, como string o directamente ausentes/null
según el par, así que la conversión defensiva se centraliza acá.
"""


def to_float(value: object, default: float = 0.0) -> float:
    """Convierte `value` a float; devuelve `default` si es None, no numérico
    o un string no parseable (en vez de reventar con TypeError/ValueError)."""
    if isinstance(value, bool):
        # bool es subclase de int: True daría 1.0, casi siempre por error.
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_address(address: object) -> object:
    """Clave comparable de una dirección de token.

    En chains EVM la misma dirección llega en minúsculas desde GeckoTerminal y
    con checksum (mayúsculas mezcladas) desde DexScreener, así que se pasa a
    minúsculas. Las de Solana (base58) distinguen mayúsculas y no se tocan."""
    if isinstance(address, str) and address[:2].lower() == "0x":
        return address.lower()
    return address


# Por debajo de esto las compras/ventas de 5 min son ruido: 3 compras y
# 1 venta dan 75% sin decir nada del mercado.
MIN_TXNS_M5 = 5


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


def txns_m5(pair: dict) -> float:
    """Compras + ventas de los últimos 5 minutos. 0.0 si la API no las informa."""
    txns = (pair.get("txns") or {}).get("m5")
    if not isinstance(txns, dict):
        return 0.0
    return to_float(txns.get("buys")) + to_float(txns.get("sells"))


def buy_ratio_m5(pair: dict) -> float | None:
    """Fracción de compras sobre el total de txns de los últimos 5 min (0-1).
    None si hubo menos de MIN_TXNS_M5 txns, porque con tan pocas no dice nada."""
    total = txns_m5(pair)
    if total < MIN_TXNS_M5:
        return None
    buys = to_float(((pair.get("txns") or {}).get("m5") or {}).get("buys"))
    return buys / total
