"""
sigpump/path_backfill.py

Rellena camino_precio en las compras registradas antes del 2026-11-07, para
que cuenten en los aciertos del aviso. Se ejecuta a mano:

    python path_backfill.py                 # solo dice qué haría
    python path_backfill.py --escribir      # lo escribe en alertas.db

El 2026-11-08, con la columna recién estrenada, casi todos los avisos salían
"sin datos todavía": de 2629 primeras compras solo 68 tenían camino.

Las filas viejas sí tienen el mejor y el peor de los primeros 30 minutos y
los checkpoints de 1 h, 4 h y 24 h (en orden). Con eso se sabe el resultado
salvo cuando en esos 30 minutos se tocaron el objetivo y el stop: sin saber
cuál fue antes no se sabe si acertó (el 2026-11-07, 789 de 2558). Si se
tocó el objetivo en la primera media hora, el acierto llegó antes que
cualquier checkpoint posterior; si no, los checkpoints ya van en orden.

- Las demás: camino con el peor antes del mejor (lo prudente: si los dos
  hubieran cruzado sería un fallo) y después los checkpoints largos.
  "fuente": "registro". Sirven para los umbrales de [wallets] de ahora;
  con otros umbrales habría que volver a pasar.
- Las que tocaron los dos: velas de 1 minuto del pool en GeckoTerminal
  (API pública, sin créditos del RPC), con el mínimo de cada vela antes que
  el máximo (lo prudente si los dos caen en el mismo minuto).
  "fuente": "geckoterminal". Si las velas no llegan a las dos marcas (el
  seguimiento vio mechas que la vela no recoge), se quedan con el camino
  prudente del registro y salen como "sin resolver". Antes se prueban
  otros pools del mismo token ("pool" en el JSON dice cuál resolvió); con
  --reintentar se vuelven a pasar las que se quedaron sin resolver.
  hit_rates no cuenta las "sin resolver".

Solo toca las primeras compras de cada token por wallet (las que cuentan en
hit_rates), sin las de bonding curve, ya fuera de seguimiento y con
camino_precio vacío (o "sin resolver"): nunca pisa un camino del
seguimiento en directo. Para
deshacerlo: UPDATE alertas SET camino_precio = NULL WHERE
json_extract(camino_precio, '$.fuente') IS NOT NULL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp  # type: ignore[import-not-found]

from sigpump.config import Config
from sigpump.tracker import _FIRST_BUYS, UNRESOLVED_PATH, AlertTracker, TRACKING_MINUTES, extend_path, path_hit

log = logging.getLogger()

API = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
TOKEN_POOLS_API = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{token}/pools?page=1"
# Si el pool del aviso no resuelve, cuántos otros pools del token se prueban.
# 2026-11-08: de 789 que pedían velas, 147 se quedaron sin resolver; en 39 de
# las 59 primeras GeckoTerminal no tenía ni una vela del pool (pares raros,
# como uno contra PUMP).
OTHER_POOLS = 2
# 2026-11-08: la API pública daba 429 en cuanto se pasaba de unas 10 por
# minuto; a una cada 6 s casi no hay reintentos.
DEFAULT_PAUSE_SECONDS = 6.0
MAX_RETRIES = 6
RETRY_WAIT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 20
# Muestras de los checkpoints largos, en el orden en que el seguimiento las
# añade al camino.
LATER_CHECKPOINTS = ("ret_60m_pct", "ret_240m_pct", "ret_1440m_pct")
_BEST, _WORST = f"mejor_ret_{TRACKING_MINUTES}m_pct", f"peor_ret_{TRACKING_MINUTES}m_pct"
# Lo mismo que el margen del checkpoint de 30 min: el seguimiento muestrea
# hasta ahí.
WINDOW_SECONDS = (TRACKING_MINUTES + 5) * 60

FROM_RECORD, FROM_CANDLES, UNRESOLVED = "registro", "geckoterminal", UNRESOLVED_PATH


def _with_checkpoints(path: dict, row: Any) -> dict:
    for column in LATER_CHECKPOINTS:
        if row[column] is not None:
            path = extend_path(path, row[column])
    return path


def path_from_record(row: Any) -> dict:
    """Camino con lo que guardaba el registro: el peor de 30 min antes que el
    mejor, y después los checkpoints largos."""
    path = extend_path(extend_path({"peor": 0.0, "picos": []}, row[_WORST]), row[_BEST])
    return {**_with_checkpoints(path, row), "fuente": FROM_RECORD}


def needs_candles(row: Any, hit_pct: float, max_drop_pct: float) -> bool:
    """Si en los primeros 30 min se tocaron el objetivo y el stop: solo
    entonces el orden cambia el resultado."""
    return row[_BEST] >= hit_pct and row[_WORST] < -max_drop_pct


def path_from_candles(row: Any, candles: list, hit_pct: float, max_drop_pct: float) -> dict | None:
    """Camino con las velas de 1 minuto ([t, open, high, low, close, vol]) de
    los primeros 30 min, y después los checkpoints largos. None si las velas
    no llegan al objetivo y al stop: entonces no dicen cuál fue antes."""
    start = row["timestamp"]
    candles = sorted(c for c in candles if start - 60 < c[0] <= start + WINDOW_SECONDS)
    price = row["precio_usd"]
    path = {"peor": 0.0, "picos": []}
    for candle in candles:
        for value in (candle[3], candle[2]):
            path = extend_path(path, round((value / price - 1) * 100, 2))
    if not path["picos"] or path["picos"][-1][0] < hit_pct or path["peor"] >= -max_drop_pct:
        return None
    return {**_with_checkpoints(path, row), "fuente": FROM_CANDLES}


class GeckoTerminal:
    """Velas de 1 minuto y pools de un token, de una llamada en una y con
    pausa entre llamadas para no comerse el límite de la API pública."""

    def __init__(self, session: aiohttp.ClientSession, pause: float = DEFAULT_PAUSE_SECONDS):
        self._session = session
        self._pause = pause
        self._last = 0.0
        self.calls = 0

    async def _wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _get(self, url: str, what: str) -> Any:
        """JSON de `url`, reintentando 429, 5xx y fallos de red. {} si no
        existe (404), None si la API falla después de reintentar."""
        for attempt in range(MAX_RETRIES):
            await self._wait(max(0.0, self._last + self._pause - time.monotonic()))
            self._last = time.monotonic()
            self.calls += 1
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                async with self._session.get(url, timeout=timeout) as resp:
                    if resp.status == 404:
                        return {}
                    if resp.status != 429 and resp.status < 500:
                        resp.raise_for_status()
                        return await resp.json(content_type=None) or {}
                    reason = f"HTTP {resp.status}"
            except aiohttp.ClientResponseError as exc:
                log.warning("GeckoTerminal %s en %s", exc.status, what)
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            log.debug("GeckoTerminal %s en %s, reintento %d", reason, what, attempt + 1)
            await self._wait(RETRY_WAIT_SECONDS * (attempt + 1))
        log.warning("GeckoTerminal sin respuesta para %s", what)
        return None

    async def candles(self, pool: str, start: float, token: str = "base") -> list | None:
        """Velas desde un minuto antes de `start` hasta WINDOW_SECONDS
        después, con el precio de `token` (su dirección, o "base"). None si
        la API falla después de reintentar."""
        before = int(start + WINDOW_SECONDS + 60)
        url = f"{API.format(pool=pool)}?aggregate=1&before_timestamp={before}&limit=40&currency=usd&token={token}"
        data = await self._get(url, pool)
        if data is None:
            return None
        return ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []

    async def token_pools(self, token: str) -> list[str] | None:
        """Pools del token, en el orden de GeckoTerminal (los de más volumen
        primero). None si la API falla."""
        data = await self._get(TOKEN_POOLS_API.format(token=token), token)
        if data is None:
            return None
        return [p["attributes"]["address"] for p in data.get("data") or [] if (p.get("attributes") or {}).get("address")]


def pending_rows(conn: sqlite3.Connection, now: float, retry: bool = False) -> list[sqlite3.Row]:
    """Primeras compras sin camino, ya fuera de seguimiento y con el mejor
    y el peor de 30 min anotados. Con `retry`, también las que se quedaron
    "sin resolver" en una pasada anterior."""
    unresolved = f"OR json_extract(camino_precio, '$.fuente') = '{UNRESOLVED}'" if retry else ""
    return conn.execute(
        f"SELECT * FROM alertas WHERE id IN ({_FIRST_BUYS}) AND (camino_precio IS NULL {unresolved}) "
        f"AND precio_usd > 0 AND {_BEST} IS NOT NULL AND {_WORST} IS NOT NULL "
        "AND timestamp < :cutoff ORDER BY id",
        {"wallet": None, "cutoff": AlertTracker._cutoff(now)},
    ).fetchall()


def _load_cache(path: Path | None) -> dict[str, list]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_cache(path: Path | None, cache: dict[str, list]) -> None:
    if path is not None:
        path.write_text(json.dumps(cache))


async def _other_pools(row: Any, gecko: GeckoTerminal | None, cache: dict[str, list]) -> list[str]:
    """Hasta OTHER_POOLS pools del token que no son el del aviso."""
    key = f"pools:{row['token']}"
    if key not in cache and gecko is not None:
        pools = await gecko.token_pools(row["token"])
        if pools is not None:
            cache[key] = pools
    return [pool for pool in cache.get(key, []) if pool != row["pool"]][:OTHER_POOLS]


async def _resolve(
    row: Any, gecko: GeckoTerminal | None, hit_pct: float, max_drop_pct: float, cache: dict[str, list],
) -> dict:
    """Camino con las velas del pool del aviso y, si no resuelven, con las de
    los otros pools del token (ya cacheadas, o pedidas si hay `gecko`)."""
    path = path_from_candles(row, cache.get(str(row["id"]), []), hit_pct, max_drop_pct)
    if path is not None:
        return path
    for pool in await _other_pools(row, gecko, cache):
        key = f"{row['id']}:{pool}"
        if key not in cache and gecko is not None:
            # Por la dirección del token y no "base": en otro pool puede ir
            # de quote.
            candles = await gecko.candles(pool, row["timestamp"], row["token"])
            if candles is not None:
                cache[key] = candles
        path = path_from_candles(row, cache.get(key, []), hit_pct, max_drop_pct)
        if path is not None:
            return {**path, "pool": pool}
    return {**path_from_record(row), "fuente": UNRESOLVED}


async def build_paths(
    rows: list, gecko: GeckoTerminal | None, hit_pct: float, max_drop_pct: float,
    cache: dict[str, list], cache_path: Path | None = None,
) -> tuple[dict[int, dict], Counter]:
    """id -> camino de cada fila, y cuántas salieron de cada fuente. Las
    velas ya en `cache` (id de la fila, o id:pool para los otros pools ->
    velas) no se vuelven a pedir. Sin `gecko`, las que piden velas y no
    están en la caché se dejan fuera: si se guardaran como "sin resolver",
    la pasada siguiente ya no las tocaría (sin --reintentar)."""
    paths: dict[int, dict] = {}
    sources: Counter = Counter()
    ambiguous = [row for row in rows if needs_candles(row, hit_pct, max_drop_pct)]
    for row in rows:
        if not needs_candles(row, hit_pct, max_drop_pct):
            paths[row["id"]] = path_from_record(row)
            sources[FROM_RECORD] += 1
    for done, row in enumerate(ambiguous, 1):
        key = str(row["id"])
        if key not in cache and gecko is not None:
            candles = await gecko.candles(row["pool"], row["timestamp"])
            if candles is not None:
                cache[key] = candles
        if key not in cache and gecko is None:
            continue
        path = await _resolve(row, gecko, hit_pct, max_drop_pct, cache)
        paths[row["id"]] = path
        sources[path["fuente"]] += 1
        if done % 20 == 0:
            _save_cache(cache_path, cache)
        if done % 25 == 0 or done == len(ambiguous):
            print(f"  velas: {done}/{len(ambiguous)}", file=sys.stderr)
    _save_cache(cache_path, cache)
    return paths, sources


def write_paths(conn: sqlite3.Connection, paths: dict[int, dict]) -> int:
    """Guarda los caminos en tandas cortas (el bot escribe en la misma base)
    sin pisar los que ya tengan uno, salvo los "sin resolver" de una pasada
    anterior. Devuelve cuántas filas cambió."""
    changed = 0
    items = list(paths.items())
    for i in range(0, len(items), 100):
        with conn:
            for row_id, path in items[i:i + 100]:
                changed += conn.execute(
                    "UPDATE alertas SET camino_precio = ? WHERE id = ? AND (camino_precio IS NULL "
                    f"OR json_extract(camino_precio, '$.fuente') = '{UNRESOLVED}')",
                    (json.dumps(path), row_id),
                ).rowcount
    return changed


def summary(rows: list, paths: dict[int, dict], hit_pct: float, max_drop_pct: float) -> list[str]:
    """Líneas del informe: aciertos por fuente y wallets que pasan a tener datos."""
    by_source: dict[str, list[bool]] = {}
    wallets = set()
    rows = [row for row in rows if row["id"] in paths]
    for row in rows:
        path = paths[row["id"]]
        by_source.setdefault(path["fuente"], []).append(bool(path_hit(path, hit_pct, max_drop_pct)))
        wallets.add(row["wallet"])
    lines = [f"{len(rows)} primeras compras sin camino, de {len(wallets)} wallets"]
    for source in (FROM_RECORD, FROM_CANDLES, UNRESOLVED):
        hits = by_source.get(source, [])
        if hits:
            rate = "no cuentan" if source == UNRESOLVED else f"aciertos {sum(hits) / len(hits):.0%}"
            lines.append(f"  {source:>13}: {len(hits):5d}, {rate}")
    every = [h for source, hits in by_source.items() if source != UNRESOLVED for h in hits]
    if every:
        lines.append(f"  {'total':>13}: {len(every):5d}, aciertos {sum(every) / len(every):.0%}")
    return lines


async def _run(config: Config, args: argparse.Namespace) -> None:
    hit_pct, max_drop_pct = config.wallets_hit_pct, config.wallets_hit_max_drop_pct
    db = Path(config.alert_log_path)
    if args.escribir:
        conn = sqlite3.connect(db, timeout=30)
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = pending_rows(conn, time.time(), args.reintentar)
        if args.limit:
            rows = rows[:args.limit]
        cache = _load_cache(args.cache)
        missing = sum(
            1 for row in rows if needs_candles(row, hit_pct, max_drop_pct) and str(row["id"]) not in cache
        )
        print(f"Objetivo {hit_pct:+g}%, stop -{max_drop_pct:g}%. {len(rows)} filas, "
              f"{missing} piden velas a GeckoTerminal (~{missing * args.pausa / 60:.0f} min)", file=sys.stderr)
        if args.sin_red:
            paths, _ = await build_paths(rows, None, hit_pct, max_drop_pct, cache)
            if missing:
                print(f"--sin-red: {missing} sin velas en la caché se quedan para otra pasada", file=sys.stderr)
        else:
            async with aiohttp.ClientSession() as session:
                gecko = GeckoTerminal(session, args.pausa)
                paths, _ = await build_paths(rows, gecko, hit_pct, max_drop_pct, cache, args.cache)
        print("\n".join(summary(rows, paths, hit_pct, max_drop_pct)))
        if args.escribir:
            print(f"Escritas {write_paths(conn, paths)} filas en {db}")
        else:
            print("Sin --escribir: no se tocó la base")
    finally:
        conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rellena camino_precio en las compras de antes del 2026-11-07")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--escribir", action="store_true", help="guardar los caminos en la base")
    parser.add_argument("--cache", type=Path, help="fichero JSON con las velas ya pedidas (se reutiliza)")
    parser.add_argument("--pausa", type=float, default=DEFAULT_PAUSE_SECONDS,
                        help="segundos entre llamadas a GeckoTerminal")
    parser.add_argument("--sin-red", action="store_true",
                        help="no pedir velas: las que no estén en la caché se dejan para otra pasada")
    parser.add_argument("--reintentar", action="store_true",
                        help="volver a pasar las que quedaron sin resolver (otros pools del token)")
    parser.add_argument("--limit", type=int, help="solo las primeras N filas")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(format="[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    try:
        config = Config.from_toml(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"Configuración inválida: {e}") from e
    asyncio.run(_run(config, args))


if __name__ == "__main__":
    main()
