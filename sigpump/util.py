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
