"""
sigpump/config.py

Carga la configuración desde config.toml hacia un dataclass tipado y la
valida al arrancar.
"""

import logging
import tomllib
import types
import typing
from dataclasses import Field, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from sigpump.solana import MAINNET_RPC_URL
from sigpump.tracker import CHECKPOINTS_MINUTES

log = logging.getLogger()


def _key(toml_key: str, default: object = None, *, factory: Callable[[], object] | None = None) -> Any:
    """Campo de Config leído de `toml_key` ("sección.clave"). Las claves
    válidas, los tipos aceptados y la lectura del TOML salen de acá: antes
    cada clave se declaraba en tres sitios, con el default repetido."""
    metadata = {"toml": toml_key}
    if factory is not None:
        return field(default_factory=factory, metadata=metadata)
    return field(default=default, metadata=metadata)


def _toml_key(f: Field) -> tuple[str, str]:
    section, _, key = f.metadata["toml"].partition(".")
    return section, key


def _accepted_types(annotation: object) -> tuple[type, ...]:
    """Tipos que acepta un campo según su anotación: un float acepta también
    un int (`interval_seconds = 15`), list[str] se mira como list (sus
    elementos los valida __post_init__) e `int | None` acepta los dos."""
    if annotation is float:
        return (int, float)
    if isinstance(annotation, types.UnionType):
        return tuple(t for arg in annotation.__args__ for t in _accepted_types(arg))
    return (typing.get_origin(annotation) or annotation,)


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
class Config:
    """Configuración completa, con defaults sensatos si el TOML no los define."""
    chain_id: str = _key("dexscreener.chain_id", "solana")
    # Monedas contra las que debe cotizar el par (símbolo o dirección del
    # quote). Vacío = cualquiera.
    quote_tokens: list[str] = _key("dexscreener.quote_tokens", factory=list)
    # dexId de DexScreener en los que debe estar el par (pumpswap, raydium...).
    # Vacío = cualquiera.
    dex_ids: list[str] = _key("dexscreener.dex_ids", factory=list)
    alert_log_path: str = _key("radar.alert_log_path", "alertas.db")
    # Seguimiento de wallets: avisa cuando una de las del fichero entra en un token.
    wallets_file: str = _key("wallets.file", "wallets.txt")
    wallets_interval_seconds: float = _key("wallets.interval_seconds", 15.0)
    # Mínimo de SOL gastado para considerar que una transacción es una compra.
    wallets_min_sol: float = _key("wallets.min_sol", 0.1)
    # Minutos sin repetir aviso de la misma wallet en el mismo token.
    wallets_cooldown_minutes: float = _key("wallets.cooldown_minutes", 60.0)
    # Ventana en la que otras wallets seguidas que entraron en el mismo token
    # se mencionan en el aviso.
    wallets_confluence_minutes: float = _key("wallets.confluence_minutes", 60.0)
    # Wallets seguidas distintas que tienen que haber comprado el token dentro
    # de confluence_minutes para avisar. 1 = avisar de cada compra.
    wallets_min_wallets: int = _key("wallets.min_wallets", 1)
    # Minutos sin volver a avisar de un token ya avisado por wallets, salvo
    # que se sumen realert_new_wallets wallets más (0 = nunca). 0 = sin límite por token.
    wallets_token_cooldown_minutes: float = _key("wallets.token_cooldown_minutes", 0.0)
    wallets_realert_new_wallets: int = _key("wallets.realert_new_wallets", 2)
    # Una wallet que compra más tokens distintos que esto en una hora no
    # avisa ni cuenta para la confluencia de otras, y con blacklist va a
    # la lista negra. 0 = sin tope.
    wallets_max_tokens_per_hour: int = _key("wallets.max_tokens_per_hour", 0)
    # Una wallet con más transacciones que esto en una hora es un bot: con
    # blacklist va a la lista negra antes de leerlas. 0 = sin tope.
    wallets_max_txs_per_hour: int = _key("wallets.max_txs_per_hour", 0)
    # Una wallet sin ninguna transacción en estos días está abandonada: con
    # blacklist va a la lista negra por max_inactive_days, y al acabar se
    # comprueba si volvió a operar. 0 = no hacerlo.
    wallets_max_inactive_days: float = _key("wallets.max_inactive_days", 30.0)
    # Lista negra, en la tabla lista_negra de alert_log_path: sus wallets se
    # quitan del fichero y no se vuelven a seguir.
    wallets_blacklist: bool = _key("wallets.blacklist", False)
    # Fichero de lista negra de versiones anteriores: al arrancar se pasa a
    # la base y se renombra a .importado.
    wallets_blacklist_file: str = _key("wallets.blacklist_file", "")
    # Una wallet cuya compra cae este % (rug) en el seguimiento entra en la
    # lista negra sola. 0 = no hacerlo.
    wallets_rug_drop_pct: float = _key("wallets.rug_drop_pct", 90.0)
    # Una wallet con al menos loser_min_signals tokens registrados cuya
    # mediana a loser_checkpoint_minutes es loser_max_median_ret_pct o peor
    # entra en la lista negra sola. 0 = no hacerlo.
    wallets_loser_min_signals: int = _key("wallets.loser_min_signals", 0)
    wallets_loser_max_median_ret_pct: float = _key("wallets.loser_max_median_ret_pct", -30.0)
    # Checkpoint del registro (minutos, uno de tracker.CHECKPOINTS_MINUTES)
    # con el que se mide esa mediana. A 30 min muchas parecían buenas y se
    # hundían horas después.
    wallets_loser_checkpoint_minutes: int = _key("wallets.loser_checkpoint_minutes", 30)
    # Hora local (0-23) a la que se manda cada día por Telegram el ranking de
    # las wallets seguidas por su historial en el registro. -1 = no mandarlo.
    wallets_ranking_hour: int = _key("wallets.ranking_hour", -1)
    # Filtros del par para los avisos de wallets (0 = sin filtro): los pares
    # recién creados, los que ya subieron mucho en 1h y los de market cap
    # pequeño daban casi siempre pérdidas.
    wallets_min_pair_age_minutes: float = _key("wallets.min_pair_age_minutes", 0.0)
    wallets_max_price_change_h1_pct: float = _key("wallets.max_price_change_h1_pct", 0.0)
    # Lo que ya subió en los últimos 5 min: llegar tarde a la vela. Se aplica
    # también a las wallets de confianza y en bonding curve.
    wallets_max_price_change_m5_pct: float = _key("wallets.max_price_change_m5_pct", 0.0)
    wallets_min_market_cap_usd: float = _key("wallets.min_market_cap_usd", 0.0)
    # Actividad mínima del par: los tokens muertos (una o dos transacciones
    # en los últimos minutos) no se mueven y el aviso no sirve de nada.
    wallets_min_txns_m5: int = _key("wallets.min_txns_m5", 0)
    wallets_min_volume_h1_usd: float = _key("wallets.min_volume_h1_usd", 0.0)
    # Volumen de la última hora sobre el market cap: con mucho más volumen
    # del que corresponde al tamaño del token lo están bombeando o ya están
    # saliendo, y casi siempre se hundía después. 0 = sin filtro.
    wallets_max_turnover_h1: float = _key("wallets.max_turnover_h1", 0.0)
    # Avisar también de las compras de tokens que siguen en la bonding curve
    # de un launchpad (pump.fun...). A esas solo se les aplican quote_tokens,
    # max_price_change_h1_pct, max_price_change_m5_pct, min_txns_m5 y
    # min_volume_h1_usd: el dex, la edad, el market cap y el turnover las
    # descartarían siempre.
    wallets_bonding_curve: bool = _key("wallets.bonding_curve", False)
    # Avisos de actividad por el WebSocket del RPC: solo se consultan las
    # wallets que hicieron algo. ws_url vacío = el de rpc_url con wss://.
    wallets_websocket: bool = _key("wallets.websocket", True)
    wallets_ws_url: str = _key("wallets.ws_url", "")
    # Con el WebSocket, cada cuánto se consultan todas por si se perdió algún aviso.
    wallets_full_poll_minutes: float = _key("wallets.full_poll_minutes", 10.0)
    verbose: bool = _key("radar.verbose", False)
    # Comprobación en la blockchain de que el token no se pueda acuñar ni congelar.
    check_token_authorities: bool = _key("solana.check_token_authorities", True)
    solana_rpc_url: str = _key("solana.rpc_url", MAINNET_RPC_URL)
    telegram_bot_token: str = _key("telegram.bot_token", "")
    telegram_chat_id: str = _key("telegram.chat_id", "")
    telegram_message_thread_id: int | None = _key("telegram.message_thread_id", None)

    def __post_init__(self) -> None:
        """Valida los rangos apenas se construye el Config, para que un TOML
        inconsistente falle al arrancar y no a mitad de una vuelta."""
        # Tipos primero: con `interval_seconds = "15"` la comparación de
        # abajo tiraba un TypeError crudo en vez de un error de configuración.
        for f in fields(self):
            value = getattr(self, f.name)
            accepted = _accepted_types(f.type)
            # bool es subclase de int: `min_wallets = true` no es un número.
            if not isinstance(value, accepted) or (isinstance(value, bool) and bool not in accepted):
                section, key = _toml_key(f)
                raise ValueError(f"[{section}].{key} tiene un tipo inválido: {value!r}")
        if not self.chain_id:
            raise ValueError("[dexscreener].chain_id no puede estar vacío")
        if not all(isinstance(quote, str) and quote for quote in self.quote_tokens):
            raise ValueError(
                f"[dexscreener].quote_tokens debe ser una lista de textos ({self.quote_tokens!r})"
            )
        if not all(isinstance(dex, str) and dex for dex in self.dex_ids):
            raise ValueError(f"[dexscreener].dex_ids debe ser una lista de textos ({self.dex_ids!r})")
        if not self.solana_rpc_url:
            raise ValueError("[solana].rpc_url no puede estar vacío")
        if self.wallets_min_wallets < 1:
            raise ValueError(f"[wallets].min_wallets debe ser >= 1 ({self.wallets_min_wallets})")
        if self.wallets_realert_new_wallets < 0:
            raise ValueError(
                f"[wallets].realert_new_wallets debe ser >= 0 ({self.wallets_realert_new_wallets})"
            )
        if self.wallets_max_tokens_per_hour < 0:
            raise ValueError(
                f"[wallets].max_tokens_per_hour debe ser >= 0 ({self.wallets_max_tokens_per_hour})"
            )
        if self.wallets_max_txs_per_hour < 0:
            raise ValueError(
                f"[wallets].max_txs_per_hour debe ser >= 0 ({self.wallets_max_txs_per_hour})"
            )
        if self.wallets_max_inactive_days < 0:
            raise ValueError(
                f"[wallets].max_inactive_days debe ser >= 0 ({self.wallets_max_inactive_days})"
            )
        if not 0 <= self.wallets_rug_drop_pct <= 100:
            raise ValueError(
                f"[wallets].rug_drop_pct debe estar entre 0 y 100 ({self.wallets_rug_drop_pct})"
            )
        if self.wallets_loser_min_signals < 0:
            raise ValueError(
                f"[wallets].loser_min_signals debe ser >= 0 ({self.wallets_loser_min_signals})"
            )
        for name in (
            "min_pair_age_minutes", "max_price_change_h1_pct", "max_price_change_m5_pct", "min_market_cap_usd",
            "min_txns_m5", "min_volume_h1_usd", "max_turnover_h1",
        ):
            if getattr(self, f"wallets_{name}") < 0:
                raise ValueError(f"[wallets].{name} debe ser >= 0 ({getattr(self, f'wallets_{name}')})")
        if self.wallets_loser_checkpoint_minutes not in CHECKPOINTS_MINUTES:
            raise ValueError(
                f"[wallets].loser_checkpoint_minutes debe ser uno de "
                f"{', '.join(map(str, CHECKPOINTS_MINUTES))} ({self.wallets_loser_checkpoint_minutes})"
            )
        if not -1 <= self.wallets_ranking_hour <= 23:
            raise ValueError(f"[wallets].ranking_hour debe estar entre -1 y 23 ({self.wallets_ranking_hour})")
        if self.wallets_ranking_hour >= 0 and not self.alert_log_path:
            raise ValueError("[wallets].ranking_hour necesita [radar].alert_log_path: el ranking sale de esa base")
        if not self.wallets_file:
            raise ValueError("[wallets].file no puede estar vacío")
        if self.wallets_blacklist and not self.alert_log_path:
            raise ValueError("[wallets].blacklist necesita [radar].alert_log_path: la lista negra se guarda en esa base")
        if self.wallets_full_poll_minutes <= 0:
            raise ValueError(
                f"[wallets].full_poll_minutes debe ser > 0 ({self.wallets_full_poll_minutes})"
            )
        if self.wallets_interval_seconds <= 0:
            raise ValueError(
                f"[wallets].interval_seconds debe ser > 0 ({self.wallets_interval_seconds})"
            )
        for name in (
            "wallets_min_sol",
            "wallets_cooldown_minutes",
            "wallets_confluence_minutes",
            "wallets_token_cooldown_minutes",
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

        with open(path, "rb") as file:
            raw = tomllib.load(file)

        unknown_sections = sorted(set(raw) - set(_KNOWN_KEYS))
        if unknown_sections:
            log.warning("Secciones desconocidas en el TOML, se ignoran: %s", ", ".join(unknown_sections))
        sections = {name: _section(raw, name) for name in _KNOWN_KEYS}
        # Lo que no está en el TOML se queda con el default del campo.
        values: dict[str, object] = {}
        for f in fields(cls):
            section, key = _toml_key(f)
            if key in sections[section]:
                values[f.name] = sections[section][key]

        # Un config con blacklist_file de antes sigue teniendo lista negra.
        wallets = sections["wallets"]
        if "blacklist" not in wallets:
            values["wallets_blacklist"] = bool(wallets.get("blacklist_file"))
        # Los chat_id son números (-100123...) y es natural escribirlos sin
        # comillas; Telegram acepta ambos, así que se normaliza a str.
        chat_id = values.get("telegram_chat_id")
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            values["telegram_chat_id"] = str(chat_id)
        return cls(**values)


def _known_keys() -> dict[str, set[str]]:
    """Claves válidas por sección del TOML, sacadas de los campos de Config."""
    keys: dict[str, set[str]] = {}
    for f in fields(Config):
        section, key = _toml_key(f)
        keys.setdefault(section, set()).add(key)
    return keys


_KNOWN_KEYS = _known_keys()
