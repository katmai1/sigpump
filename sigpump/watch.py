"""
sigpump/watch.py

Memoria entre pasadas: guarda fotos periódicas de cada pool (precio, volumen
y txns de 5 min) y detecta cuándo uno que estaba tranquilo arranca,
comparándolo con su propia historia reciente. La media de la última hora
(la que usa el score) ya incluye la subida cuando esta lleva unos minutos;
la historia propia no, y eso permite avisar antes.
"""

import statistics
from collections import deque
from dataclasses import dataclass

from sigpump.signals import EarlySignal, buy_ratio_m5
from sigpump.util import normalize_address, to_float

# Minutos de fotos que se conservan por pool.
HISTORY_MINUTES = 20
# La base son las fotos de hace al menos 5 min: sus ventanas de 5 min
# (volume.m5, txns.m5) no se pisan con la de la foto actual.
BASELINE_MIN_AGE_SECONDS = 5 * 60
# Con una sola foto, un valor raro de ese momento decidiría todo.
MIN_BASELINE_SNAPSHOTS = 2
# Pisos de la base: sin ellos, un pool con $5 de volumen en 5 min daría x20
# con apenas $100.
MIN_BASELINE_VOLUME_M5_USD = 100.0
MIN_BASELINE_TXNS_M5 = 3.0


@dataclass(frozen=True)
class Snapshot:
    ts: float
    price: float
    volume_m5: float
    txns_m5: float


@dataclass(frozen=True)
class EarlyThresholds:
    """Umbrales de [watch] para considerar que un pool arrancó."""
    min_price_move_pct: float
    max_price_move_pct: float
    min_volume_ratio: float
    min_txns_ratio: float
    min_buy_ratio: float


def _pool_key(pair: dict) -> str:
    return str(normalize_address(pair.get("pairAddress") or ""))


def _snapshot(pair: dict, now: float) -> Snapshot:
    txns = (pair.get("txns") or {}).get("m5")
    if not isinstance(txns, dict):
        txns = {}
    return Snapshot(
        ts=now,
        price=to_float(pair.get("priceUsd")),
        volume_m5=to_float((pair.get("volume") or {}).get("m5")),
        txns_m5=to_float(txns.get("buys")) + to_float(txns.get("sells")),
    )


class PoolHistory:
    """Fotos recientes por pool. Se indexa por pool y no por token porque el
    volumen y las txns de cada pool de un mismo token no son comparables."""

    def __init__(self) -> None:
        self._snapshots: dict[str, deque[Snapshot]] = {}

    def __len__(self) -> int:
        return len(self._snapshots)

    def observe(self, pair: dict, now: float) -> None:
        """Agrega la foto actual de `pair`. Los pares sin pool o sin precio se
        ignoran: no sirven como base."""
        pool = _pool_key(pair)
        snapshot = _snapshot(pair, now)
        if not pool or snapshot.price <= 0:
            return
        snapshots = self._snapshots.setdefault(pool, deque())
        snapshots.append(snapshot)
        self._trim(snapshots, now)

    def prune(self, now: float) -> None:
        """Olvida las fotos viejas y los pools que se dejaron de consultar,
        para que la memoria no crezca en ejecuciones largas."""
        for pool in list(self._snapshots):
            snapshots = self._snapshots[pool]
            self._trim(snapshots, now)
            if not snapshots:
                del self._snapshots[pool]

    @staticmethod
    def _trim(snapshots: deque[Snapshot], now: float) -> None:
        while snapshots and snapshots[0].ts < now - HISTORY_MINUTES * 60:
            snapshots.popleft()

    def early_signal(
        self,
        pair: dict,
        now: float,
        thresholds: EarlyThresholds,
        check_max_move: bool = True,
    ) -> EarlySignal | None:
        """
        EarlySignal si `pair` arranca respecto de su base (las fotos de hace
        5-20 min): precio dentro del rango de arranque, volumen y txns de
        5 min multiplicados, mayoría de compras y precio de 5 min subiendo.
        None si no arranca o no hay base suficiente para saberlo: sin base,
        el token podría llevar una hora subiendo.

        check_max_move=False no aplica el tope de subida: sirve para ver si un
        arranque ya avisado se sostiene, donde seguir subiendo es lo esperado.
        """
        snapshots = self._snapshots.get(_pool_key(pair))
        if not snapshots:
            return None
        baseline = [s for s in snapshots if s.ts <= now - BASELINE_MIN_AGE_SECONDS]
        if len(baseline) < MIN_BASELINE_SNAPSHOTS:
            return None
        current = _snapshot(pair, now)
        if current.price <= 0:
            return None
        # Tiene que estar subiendo ahora, no haber subido hace 4 minutos.
        if to_float((pair.get("priceChange") or {}).get("m5")) <= 0:
            return None

        # Medianas: una foto rara en la base (un pico aislado) no la mueve.
        base_price = statistics.median(s.price for s in baseline)
        price_move_pct = (current.price / base_price - 1) * 100
        if price_move_pct < thresholds.min_price_move_pct:
            return None
        if check_max_move and price_move_pct > thresholds.max_price_move_pct:
            return None
        base_volume = max(statistics.median(s.volume_m5 for s in baseline), MIN_BASELINE_VOLUME_M5_USD)
        volume_ratio = current.volume_m5 / base_volume
        if volume_ratio < thresholds.min_volume_ratio:
            return None
        base_txns = max(statistics.median(s.txns_m5 for s in baseline), MIN_BASELINE_TXNS_M5)
        txns_ratio = current.txns_m5 / base_txns
        if txns_ratio < thresholds.min_txns_ratio:
            return None
        buy_ratio = buy_ratio_m5(pair)
        if buy_ratio is None or buy_ratio < thresholds.min_buy_ratio:
            return None
        return EarlySignal(
            price_move_pct=price_move_pct,
            volume_ratio=volume_ratio,
            txns_ratio=txns_ratio,
            buy_ratio=buy_ratio,
            baseline_minutes=(now - min(s.ts for s in baseline)) / 60,
        )
