"""
sigpump/radar.py

Orquesta el seguimiento de wallets: busca las compras nuevas de las wallets
seguidas, trae de DexScreener el par del token comprado, aplica los filtros
y avisa por Telegram, registrando cada compra para medir su resultado.
"""

import time
import asyncio
import dataclasses
import logging
import re
import statistics
from pathlib import Path

import aiohttp  # type: ignore[import-not-found]

from sigpump.blacklist import Blacklist, import_blacklist_file
from sigpump.config import Config
from sigpump.cooldowns import Cooldowns
from sigpump.filters import first_failing, pair_rejection, venue_reason
from sigpump.screener import DexScreenerClient
from sigpump.solana import RpcClient, TokenAuthorities
from sigpump.summary import Summary
from sigpump.telegram import TelegramAlerter
from sigpump.tracker import AlertTracker, format_minutes, pair_from_row
from sigpump.util import liquidity_usd, normalize_address
from sigpump.wallets import WalletBuy, WalletSignal, WalletWatcher, ws_url

log = logging.getLogger()

# dexId de DexScreener de los pools que son bonding curves de un launchpad
# (el token todavía no se graduó a un AMM): las compras de wallets ahí se ignoran.
BONDING_CURVE_DEX_IDS = {"pumpfun", "meteoradbc", "launchlab", "moonshot"}
# Motivos de descarte que no dependen del par (confluencia, cooldown por
# token, wallet hiperactiva): esas compras sí pasaron sus filtros. Solo se
# usa para las filas registradas antes de que existiera pasa_filtros_par.
_NOT_PAIR_REASON_RE = re.compile(r"\d+ de \d+ wallets$|token ya avisado|wallet hiperactiva")


def _mint(pair: dict) -> str:
    """Dirección del token del par, "" si la API no la informa (o la manda
    como null: con .get(..., "") quedaba None y la clave era "None")."""
    return str(normalize_address((pair.get("baseToken") or {}).get("address") or ""))


def _symbol(pair: dict) -> object:
    return (pair.get("baseToken") or {}).get("symbol")


@dataclasses.dataclass(frozen=True)
class _Decision:
    """Qué hacer con una compra: `reason` es el motivo para no avisar (None =
    se avisa), `pair_ok` si pasó los filtros del par y las autoridades (lo
    usa la lista negra por rug) y `update` si es un reaviso de un token ya
    avisado al que entraron más wallets. `kind` agrupa el motivo en el
    resumen del log: la clave del filtro del par, "autoridades"..."""
    reason: str | None
    kind: str = ""
    pair_ok: bool = True
    update: bool = False


class MemecoinRadar:
    def __init__(self, config: Config):
        self._config = config
        self._tracker = AlertTracker(Path(config.alert_log_path)) if config.alert_log_path else None
        # Se crean en run(), con la sesión HTTP.
        self._authorities: TokenAuthorities | None = None
        self._wallet_watcher: WalletWatcher | None = None
        self._cooldowns = Cooldowns(config)
        self._summary = Summary()
        # Se crea en run(), con la sesión HTTP; el resumen cuenta sus llamadas.
        self._rpc: RpcClient | None = None

    def restore_cooldowns(self) -> None:
        """Recupera del registro las compras recientes de wallets. Sin esto un
        reinicio olvidaba los cooldowns y la confluencia."""
        if not self._tracker:
            return
        now = time.time()
        self._cooldowns.restore(self._tracker.recent_wallet_buys(self._cooldowns.restore_since(now)), now)

    def _mark_old_rows(self) -> None:
        """Anota pasa_filtros_par en las filas de versiones anteriores, con los
        filtros de ahora. Sin esto la lista negra por rug echaba wallets por
        compras en pares recién creados que ya nunca se avisarían."""
        if not self._tracker:
            return
        rows = self._tracker.unmarked_wallet_rows()
        if not rows:
            return
        passed = {row["id"]: self._row_passes_pair_filters(row) for row in rows}
        self._tracker.mark_pair_filters(passed)
        log.info(
            "Registro: %d de %d compras anteriores pasan los filtros del par actuales",
            sum(passed.values()), len(passed),
        )

    def _row_passes_pair_filters(self, row: dict) -> bool:
        """Si una compra registrada pasaría los filtros del par de ahora. El
        dex y las autoridades no se guardan: se dan por buenos si no fue
        descartada por el par (ni por eso ni por otro filtro de entonces)."""
        reason = row["motivo_descarte"]
        if not row["enviada"] and not (reason and _NOT_PAIR_REASON_RE.match(reason)):
            return False
        pair = pair_from_row(row, self._config.dex_ids[0] if self._config.dex_ids else "")
        return pair_rejection(self._config, pair, now=row["timestamp"] or 0.0) is None

    def best_pair_per_token(self, pairs: list[dict], addresses: list[str]) -> list[dict]:
        """
        Un token suele cotizar en varios pools. Se queda con el par de mayor
        liquidez por token, que es el que mejor representa su mercado real.

        Además descarta los pares donde el token pedido es el quote (p. ej.
        SOL en un par SOL/TOKEN): ahí el baseToken es otro token y avisar
        de él sería avisar del token equivocado.

        Prefiere los pares que cumplen [dexscreener].dex_ids y quote_tokens:
        un token con su pool más profundo en otro dex o contra USDC se evalúa
        con su pool contra SOL en pumpswap, si lo tiene. Si no tiene ninguno
        se queda el de más liquidez, que luego se descarta por eso.
        """
        def rank(pair: dict) -> tuple[bool, float]:
            return venue_reason(self._config, pair) is None, liquidity_usd(pair)

        wanted = set(addresses)
        best: dict[str, dict] = {}
        for pair in pairs:
            address = _mint(pair)
            if address not in wanted:
                continue
            current = best.get(address)
            if current is None or rank(pair) > rank(current):
                best[address] = pair
        return list(best.values())

    async def _unsafe_reasons(self, pairs: list[dict]) -> dict[str, str]:
        """
        Motivo por token de los que se pueden acuñar o congelar. {} si la
        comprobación está apagada o el RPC no responde: es preferible dejar
        pasar un aviso a perderlos todos por un RPC caído.
        """
        if self._authorities is None:
            return {}
        return await self._authorities.unsafe_reasons([_mint(p) for p in pairs])

    def _blacklist_ruggers(self) -> None:
        """Mete en la lista negra las wallets seguidas que compraron un token
        que luego hizo rug (cayó [wallets].rug_drop_pct en el seguimiento)."""
        watcher = self._wallet_watcher
        drop = self._config.wallets_rug_drop_pct
        if not (self._tracker and watcher and drop and self._config.wallets_blacklist):
            return
        for wallet, reason in self._tracker.rug_wallets(drop).items():
            info = watcher.wallets.get(wallet)
            if info is not None:
                watcher.ban(wallet, info.label, reason)

    def _blacklist_losers(self) -> None:
        """Mete en la lista negra las wallets seguidas cuyas compras, con al
        menos [wallets].loser_min_signals tokens medidos, tienen una mediana a
        loser_checkpoint_minutes de loser_max_median_ret_pct o peor: copiarlas
        hace perder."""
        watcher = self._wallet_watcher
        cfg = self._config
        if not (self._tracker and watcher and cfg.wallets_loser_min_signals and cfg.wallets_blacklist):
            return
        minutes = cfg.wallets_loser_checkpoint_minutes
        for wallet, returns in self._tracker.wallet_returns(minutes).items():
            info = watcher.wallets.get(wallet)
            if info is None or len(returns) < cfg.wallets_loser_min_signals:
                continue
            median = statistics.median(returns)
            if median > cfg.wallets_loser_max_median_ret_pct:
                continue
            watcher.ban(
                wallet, info.label, f"mediana {median:+.0f}% a {format_minutes(minutes)} en {len(returns)} tokens"
            )

    def _blacklist_hyperactive(self, buy: WalletBuy) -> None:
        """Mete en la lista negra la wallet de `buy` si pasó de
        [wallets].max_tokens_per_hour: compra de todo y no sirve copiarla."""
        watcher = self._wallet_watcher
        if not (watcher and self._config.wallets_blacklist and self._cooldowns.hyperactive(buy.wallet)):
            return
        if buy.wallet not in watcher.wallets:
            return
        watcher.ban(buy.wallet, buy.label, f"hiperactiva, {self._cooldowns.tokens_last_hour(buy.wallet)} tokens en 1h")

    def _trusted(self, buy: WalletBuy) -> bool:
        """Si la wallet de `buy` es de confianza según el fichero de ahora. Las
        compras recuperadas del registro tras un reinicio no lo guardan, y
        sin esto perdían la ⭐ en la confluencia."""
        info = self._wallet_watcher.wallets.get(buy.wallet) if self._wallet_watcher else None
        return info.trusted if info else buy.trusted

    async def run_once(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """
        Vuelta del seguimiento de wallets: busca sus compras nuevas y avisa
        de cada una, salvo tokens en bonding curve (sin pool en un AMM, si
        no está [wallets].bonding_curve),
        tokens que se pueden acuñar o congelar, compras repetidas de la
        misma wallet en el mismo token dentro de su cooldown, wallets
        hiperactivas, tokens ya avisados sin wallets nuevas suficientes y,
        con [wallets].min_wallets > 1, compras sin suficientes wallets en el
        token (salvo de wallets de confianza).
        """
        if self._wallet_watcher is None:
            return
        cooldowns = self._cooldowns
        cooldowns.prune()
        # Muestrea el precio de las compras registradas para medir su resultado.
        if self._tracker:
            try:
                await self._tracker.update(client, self._config.chain_id)
            except Exception:
                log.exception("Error actualizando el registro de alertas")
        self._blacklist_ruggers()
        self._blacklist_losers()
        buys = await self._wallet_watcher.poll()
        if not buys:
            return
        summary = self._summary
        summary.buys += len(buys)
        for buy in buys:
            cooldowns.add_activity(buy)
        for buy in buys:
            self._blacklist_hyperactive(buy)
        signals = [
            WalletSignal(buy, cooldowns.confluence(buy, self._trusted)) for buy in buys
            if not cooldowns.wallet_cooldown_active(buy)
        ]
        summary.in_cooldown += len(buys) - len(signals)
        if not signals:
            return

        pairs, curve_mints = await self._pairs_for(client, [s.buy.mint for s in signals])
        unsafe = await self._unsafe_reasons(list(pairs.values()))

        for signal in signals:
            buy = signal.buy
            pair = pairs.get(buy.mint)
            if pair is None:
                log.info("Compra de %s en %s ignorada: sin pool fuera de bonding curve", buy.label, buy.mint)
                summary.no_pool += 1
                continue
            signal = dataclasses.replace(
                signal, bonding_curve=buy.mint in curve_mints, transfer_fee_bps=self._transfer_fee_bps(buy.mint)
            )
            # Una wallet que compra en varias tx seguidas da una señal por tx
            # en la misma vuelta: solo cuenta la primera.
            if cooldowns.wallet_cooldown_active(buy):
                summary.in_cooldown += 1
                continue
            # Se marca también al descartar, para no registrar una fila por
            # cada compra repetida del mismo token peligroso.
            cooldowns.start_wallet_cooldown(buy)
            decision = self.decide(signal, pair, unsafe.get(buy.mint))
            if decision.reason:
                log.info("Compra de %s en %s descartada: %s", buy.label, _symbol(pair), decision.reason)
                summary.discarded[decision.kind] += 1
                self._record_wallet(pair, signal, sent=False, reason=decision.reason, pair_ok=decision.pair_ok)
                continue
            if decision.update:
                signal = dataclasses.replace(signal, update=True)
            await self._alert(alerter, pair, signal)

    async def _pairs_for(self, client: DexScreenerClient, mints: list[str]) -> tuple[dict[str, dict], set[str]]:
        """Par de DexScreener de cada token (mint -> par) y cuáles siguen en la
        bonding curve. Se usa el pool de un AMM si lo tiene; el de la curva,
        solo con [wallets].bonding_curve y si no tiene otro."""
        mints = list(dict.fromkeys(mints))
        all_pairs = await client.get_pairs_for_tokens(self._config.chain_id, mints)
        amm_pairs = [p for p in all_pairs if p.get("dexId") not in BONDING_CURVE_DEX_IDS]
        pairs = {_mint(p): p for p in self.best_pair_per_token(amm_pairs, mints)}
        curve_mints: set[str] = set()
        if self._config.wallets_bonding_curve:
            curve_pairs = [p for p in all_pairs if p.get("dexId") in BONDING_CURVE_DEX_IDS]
            for pair in self.best_pair_per_token(curve_pairs, [m for m in mints if m not in pairs]):
                mint = _mint(pair)
                pairs[mint] = pair
                curve_mints.add(mint)
        return pairs, curve_mints

    def _transfer_fee_bps(self, mint: str) -> int | None:
        """Comisión de transferencia del token, de la consulta de autoridades
        de esta misma vuelta. None si está apagada o no se pudo consultar."""
        return self._authorities.transfer_fee_bps(mint) if self._authorities else None

    def decide(self, signal: WalletSignal, pair: dict, unsafe_reason: str | None = None) -> _Decision:
        """Si se avisa de la compra de `signal` en `pair`, y si no, por qué. En
        orden: autoridades del mint (`unsafe_reason`), filtros del par, wallet
        hiperactiva, [wallets].min_wallets (salvo las de confianza) y un aviso
        previo del token sin realert_new_wallets wallets más. No cambia nada:
        el cooldown de la wallet se mira y se marca antes."""
        buy = signal.buy
        cfg = self._config
        cooldowns = self._cooldowns
        if unsafe_reason:
            return _Decision(unsafe_reason, "autoridades", pair_ok=False)
        failing = first_failing(cfg, pair, curve=signal.bonding_curve, trusted=buy.trusted)
        if failing:
            return _Decision(failing[1], failing[0], pair_ok=False)
        if cooldowns.hyperactive(buy.wallet):
            return _Decision(
                f"wallet hiperactiva ({cooldowns.tokens_last_hour(buy.wallet)} tokens en 1h)", "hiperactiva"
            )
        wallets = 1 + len(signal.others)
        if wallets < cfg.wallets_min_wallets and not buy.trusted:
            return _Decision(f"{wallets} de {cfg.wallets_min_wallets} wallets", "min_wallets")
        previous = cooldowns.last_token_alert(buy.mint)
        if previous is None:
            return _Decision(None)
        realert = cfg.wallets_realert_new_wallets
        if not realert or wallets < previous + realert:
            return _Decision(f"token ya avisado con {previous} wallets", "token ya avisado")
        return _Decision(None, update=True)

    async def _alert(self, alerter: TelegramAlerter, pair: dict, signal: WalletSignal) -> None:
        """Envía el aviso y lo registra. Si Telegram falla no se registra y se
        quita el cooldown de la wallet: su próxima compra lo vuelve a intentar."""
        buy = signal.buy
        log.info(
            "Wallet%s: %s compró %s (%.2f SOL, %s)%s",
            " (actualización)" if signal.update else "",
            buy.label,
            _symbol(pair),
            buy.sol_spent,
            "entrada nueva" if buy.new_position else "amplía",
            f" | también {', '.join(signal.others)}" if signal.others else "",
        )
        try:
            await alerter.send(pair, signal)
        except Exception:
            log.exception("Error enviando aviso de wallet para %s", buy.mint)
            self._cooldowns.cancel_wallet_cooldown(buy)
            self._summary.send_errors += 1
            return
        self._summary.sent += 1
        self._cooldowns.mark_token_alerted(buy.mint, 1 + len(signal.others))
        self._record_wallet(pair, signal, sent=True, pair_ok=True)

    def _record_wallet(
        self, pair: dict, signal: WalletSignal, sent: bool, reason: str = "", pair_ok: bool = True
    ) -> None:
        if not self._tracker:
            return
        buy = signal.buy
        # En el mismo INSERT: con un UPDATE aparte eran dos escrituras por compra.
        self._tracker.record(pair, sent=sent, reason=reason, extra={
            "wallet": buy.wallet,
            "wallet_etiqueta": buy.label,
            "sol_gastado": buy.sol_spent,
            "stable_gastado": buy.stable_spent,
            "entrada_nueva": int(buy.new_position),
            "wallets_confluencia": len(signal.others),
            "tx": buy.signature,
            "pasa_filtros_par": int(pair_ok),
            "bonding_curve": int(signal.bonding_curve),
            "comision_transferencia_bps": signal.transfer_fee_bps,
        })

    async def run(self) -> None:
        """Loop principal: crea la sesión HTTP y el bot de Telegram una sola
        vez y sigue las wallets indefinidamente: una vuelta cada
        [wallets].interval_seconds y, con websocket, los avisos del RPC."""
        blacklist = None
        try:
            if self._tracker:
                # Abre la base al arrancar (y no en el primer aviso) para que un
                # problema con el archivo se vea enseguida en el log.
                self._tracker.open()
                self._mark_old_rows()
                self.restore_cooldowns()
            blacklist = self._open_blacklist()
            await self._follow(blacklist)
        finally:
            # Al salir (SIGTERM, Ctrl+C o un error) se cierran las dos
            # conexiones a la base; antes quedaban abiertas hasta que moría
            # el proceso. Cada escritura ya hace commit: no se pierde nada.
            if self._tracker:
                self._tracker.close()
            if blacklist is not None:
                blacklist.close()

    def _open_blacklist(self) -> Blacklist | None:
        """La lista negra si [wallets].blacklist está activa, con el fichero
        de versiones anteriores ya pasado a la base."""
        if not self._config.wallets_blacklist:
            return None
        blacklist = Blacklist(Path(self._config.alert_log_path))
        if self._config.wallets_blacklist_file:
            try:
                import_blacklist_file(
                    blacklist, Path(self._config.wallets_blacklist_file), Path(self._config.wallets_file)
                )
            except OSError as exc:
                log.error("No se pudo pasar la lista negra a la base: %s", exc)
        return blacklist

    async def _follow(self, blacklist: Blacklist | None) -> None:
        """Sigue las wallets hasta que se cancele, con la sesión HTTP y el bot."""
        alerter = TelegramAlerter(
            self._config.telegram_bot_token,
            self._config.telegram_chat_id,
            self._config.telegram_message_thread_id,
            self._config.chain_id,
        )
        # `async with alerter` valida el token al arrancar y cierra el cliente
        # HTTP de Telegram al salir.
        async with aiohttp.ClientSession() as session, alerter:
            client = DexScreenerClient(session)
            # Uno para todo el RPC: el tope de peticiones es por API key, y
            # la comprobación de autoridades iba por fuera de él.
            rpc = self._rpc = RpcClient(session, self._config.solana_rpc_url)
            if self._config.check_token_authorities:
                self._authorities = TokenAuthorities(rpc)
            self._wallet_watcher = WalletWatcher(
                rpc,
                Path(self._config.wallets_file),
                self._config.wallets_min_sol,
                blacklist,
                self._config.wallets_full_poll_minutes * 60,
                self._config.wallets_max_txs_per_hour,
                self._config.wallets_max_inactive_days * 86400,
            )
            loops = [self._wallets_loop(client, alerter)]
            if self._config.wallets_websocket:
                loops.append(self._wallet_watcher.listen(
                    self._config.wallets_ws_url or ws_url(self._config.solana_rpc_url)
                ))
            await asyncio.gather(*loops)

    async def _wallets_loop(self, client: DexScreenerClient, alerter: TelegramAlerter) -> None:
        """Una vuelta de run_once cada [wallets].interval_seconds. Errores
        inesperados (red, parsing, etc.) no deben matar el proceso: se loguean
        y se reintenta en la próxima vuelta."""
        while True:
            try:
                await self.run_once(client, alerter)
            except Exception:
                log.exception("Error siguiendo las wallets")
            if self._summary.due():
                self._log_summary()
            await asyncio.sleep(self._config.wallets_interval_seconds)

    def _log_summary(self) -> None:
        watcher = self._wallet_watcher
        wallets = watcher.wallets if watcher else {}
        if not self._config.wallets_websocket:
            websocket = "apagado"
        else:
            websocket = "conectado" if watcher and watcher.connected else "caído"
        log.info(self._summary.take(
            wallets=len(wallets),
            banned=sum(watcher.banned(w) for w in wallets) if watcher else 0,
            websocket=websocket,
            rpc_calls=self._rpc.calls if self._rpc else 0,
        ))
