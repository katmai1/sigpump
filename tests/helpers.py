"""Constantes y dobles compartidos por los tests del seguimiento de wallets
(transacciones, RPC, WebSocket, watcher, autoridades y Telegram)."""

import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.blacklist import Blacklist
from sigpump.wallet_list import FollowedWallet
from sigpump.wallets import LAMPORTS_PER_SOL, WalletBuy, WalletSignal

WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
OTRA = "7Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j9"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "TokenMint1111111111111111111111111111111pump"


def _abrir_negra(test, path):
    """Blacklist que se cierra al acabar el test (sin esto quedaban
    conexiones abiertas y avisos de ResourceWarning)."""
    negra = Blacklist(path)
    test.addCleanup(negra.close)
    return negra


def _permanentes(negra):
    return {address for address, until in negra.entries().items() if until is None}


def _motivos(negra):
    """Las entradas de la lista negra, una por línea como en el fichero de antes."""
    rows = negra._db().execute("SELECT wallet, motivo FROM lista_negra ORDER BY timestamp").fetchall()
    return "".join(f"{wallet}  # {reason}\n" for wallet, reason in rows)


def _saldo(mint, cantidad, owner=WALLET, decimals=6):
    return {
        "accountIndex": 1,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(int(cantidad * 10**decimals)), "decimals": decimals},
    }


def _tx(pre_tokens=(), post_tokens=(), sol_gastado=0.0, fee=5000, err=None, wallet=WALLET):
    pre_lamports = 10 * LAMPORTS_PER_SOL
    return {
        "blockTime": 1_700_000_000,
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": [pre_lamports],
            "postBalances": [pre_lamports - int(sol_gastado * LAMPORTS_PER_SOL) - fee],
            "preTokenBalances": list(pre_tokens),
            "postTokenBalances": list(post_tokens),
        },
    }


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRpc:
    """Responde getSignaturesForAddress con `firmas` (de la más nueva a la más
    vieja, cortadas en `until`, con su hora de `horas` si la tienen) y
    getTransaction con `txs`."""

    def __init__(self, firmas=(), txs=None, horas=None):
        self.firmas = list(firmas)
        self.txs = txs or {}
        self.horas = horas or {}
        self.llamadas: list[tuple[str, list]] = []

    def post(self, url, json=None, timeout=None):
        method, params = json["method"], json["params"]
        self.llamadas.append((method, params))
        if method == "getSignaturesForAddress":
            options = params[1]
            firmas = self.firmas
            if "until" in options:
                firmas = firmas[: firmas.index(options["until"])]
            result = [
                {"signature": f, "err": None, "blockTime": self.horas.get(f)}
                for f in firmas[: options["limit"]]
            ]
        else:
            result = self.txs.get(params[0])
        return _FakeResponse({"jsonrpc": "2.0", "result": result})


class _RpcPorWallet(_FakeRpc):
    """_FakeRpc con firmas distintas por wallet; `al_leer` se llama en cada
    getTransaction (para simular lo que llega por el WebSocket mientras)."""

    def __init__(self, por_wallet, txs=None, al_leer=None):
        super().__init__(txs=txs)
        self.por_wallet = por_wallet
        self.al_leer = al_leer

    def post(self, url, json=None, timeout=None):
        if json["method"] == "getSignaturesForAddress":
            self.firmas = self.por_wallet[json["params"][0]]
        elif self.al_leer:
            self.al_leer()
        return super().post(url, json, timeout)


class _FakeWs:
    def __init__(self):
        self.enviados: list[dict] = []

    async def send_json(self, data):
        self.enviados.append(data)


def _aviso(sub, firma="S", err=None, logs=None):
    return {
        "jsonrpc": "2.0", "method": "logsNotification",
        "params": {"subscription": sub, "result": {"value": {"signature": firma, "err": err, "logs": logs}}},
    }


_LOGS_TRANSFERENCIA = [
    "Program ComputeBudget111111111111111111111111111111 invoke [1]",
    "Program ComputeBudget111111111111111111111111111111 success",
    "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [1]",
    "Program log: Instruction: Transfer",
    "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success",
]
_LOGS_SWAP = [
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
    "Program log: Instruction: Route",
    "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]",
    "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success",
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success",
]


def _buy(wallet=WALLET, label="ballena", mint="TOK", ts=None, new=True, trusted=False):
    return WalletBuy(
        wallet=wallet, label=label, mint=mint, sol_spent=1.5, stable_spent=0.0,
        new_position=new, signature="SIG", ts=time.time() if ts is None else ts,
        trusted=trusted,
    )


class _FakeWatcher:
    def __init__(self, *rondas):
        self.rondas = list(rondas)
        self.wallets: dict[str, FollowedWallet] = {}
        self.connected = True

    def banned(self, address):
        return False

    async def poll(self):
        return self.rondas.pop(0) if self.rondas else []


class _FakeAutoridades:
    def __init__(self, unsafe=None, fees=None):
        self.unsafe = unsafe or {}
        # mint -> comisión de transferencia en bps (sin entrada = 0).
        self.fees = fees or {}

    async def unsafe_reasons(self, addresses):
        return {a: r for a, r in self.unsafe.items() if a in addresses}

    def transfer_fee_bps(self, address):
        return self.fees.get(address, 0)


class _FakeAlerter:
    def __init__(self):
        self.wallet: list[tuple[dict, WalletSignal]] = []

    async def send(self, pair, wallet):
        await asyncio.sleep(0)
        self.wallet.append((pair, wallet))


class ConFicheroDeWallets(unittest.IsolatedAsyncioTestCase):
    """Base de los tests con un fichero de wallets temporal con WALLET ('ballena')."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "wallets.txt"
        self.path.write_text(f"{WALLET}  # ballena\n", encoding="utf-8")


# Estado interno del watcher que los tests necesitan tocar, en un solo
# sitio: un refactor del watcher solo obliga a cambiar estas funciones.

def conectar(watcher, siguiente_pasada_completa=600.0):
    """Como si el WebSocket estuviera conectado y la pasada completa lejos."""
    watcher._connected = True
    watcher._next_full_poll = time.monotonic() + siguiente_pasada_completa


def desconectar(watcher):
    watcher._connected = False


def pedir_pasada_completa(watcher):
    watcher._next_full_poll = 0.0


def pendientes(watcher):
    """Firmas avisadas por el WebSocket pendientes de leer, por wallet."""
    return watcher._pending


def castigar(watcher, wallet, hasta):
    """Entrada temporal en la lista negra que vence en `hasta` (epoch)."""
    watcher._list._banned_until[wallet] = hasta


def forzar_relectura(watcher):
    """Que reload() relea el fichero aunque conserve la fecha (escrito en el
    mismo instante que la lectura anterior)."""
    watcher._list._loaded = None


def poner_actividad(watcher, wallet, firmas):
    """Transacciones (firma -> hora) que el watcher ya vio de `wallet`."""
    watcher._activity[wallet] = firmas


def poner_ultima_actividad(watcher, wallet, ts):
    watcher._last_active[wallet] = ts
