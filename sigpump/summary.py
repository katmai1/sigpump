"""
sigpump/summary.py

Resumen periódico en el log de lo que hizo el seguimiento: compras vistas,
avisos, descartes por motivo, wallets seguidas, estado del WebSocket y
llamadas al RPC. Sin él, para saber si el bot iba bien o se estaba comiendo
los créditos había que leer el log entero.
"""

import time
from collections import Counter
from typing import Callable

SUMMARY_SECONDS = 3600


class Summary:
    """Cuenta lo que pasa entre dos resúmenes. El radar suma en cada vuelta
    y pide take() cuando toca (due())."""

    def __init__(self, every_seconds: float = SUMMARY_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._every = every_seconds
        self._clock = clock
        self._next = clock() + every_seconds
        self._rpc_calls_before = 0
        self._reset()

    def _reset(self) -> None:
        self.buys = 0
        self.no_pool = 0
        self.in_cooldown = 0
        self.sent = 0
        self.send_errors = 0
        # Clave del motivo (la del filtro del par, "min_wallets"...) -> compras.
        self.discarded: Counter[str] = Counter()

    def due(self) -> bool:
        return self._clock() >= self._next

    def take(self, wallets: int, banned: int, websocket: str, rpc_calls: int) -> str:
        """La línea del resumen, y empieza a contar de cero. `rpc_calls` es el
        total del cliente RPC: se informa lo gastado desde el anterior."""
        minutes = self._every / 60
        period = f"{minutes / 60:g} h" if minutes >= 60 else f"{minutes:g} min"
        discarded = sum(self.discarded.values())
        reasons = ", ".join(f"{kind} {count}" for kind, count in self.discarded.most_common())
        line = (
            f"Resumen de {period}: {self.buys} compras de wallets seguidas, "
            f"{self.sent} avisos"
            + (f" ({self.send_errors} sin poder enviar)" if self.send_errors else "")
            + f", {discarded} descartadas"
            + (f" ({reasons})" if reasons else "")
            + f", {self.no_pool} sin pool, {self.in_cooldown} repetidas en cooldown"
            f" | {wallets} wallets" + (f" ({banned} castigadas)" if banned else "")
            + f", WebSocket {websocket} | {rpc_calls - self._rpc_calls_before} llamadas al RPC"
        )
        self._rpc_calls_before = rpc_calls
        self._next = self._clock() + self._every
        self._reset()
        return line
