"""Tests de la actividad por wallet y día (actividad_wallet)."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.activity import FLUSH_SECONDS, WalletActivity, day_of, read_activity
from sigpump.radar import MemecoinRadar
from sigpump.solana import RpcClient
from sigpump.wallets import BUY, TX_FAILED, TX_TRANSFER, WalletWatcher
from tests.helpers import (
    TOKEN,
    WALLET,
    ConFicheroDeWallets,
    _FakeRpc,
    _FakeWs,
    _LOGS_SWAP,
    _LOGS_TRANSFERENCIA,
    _aviso,
    _saldo,
    _tx,
    conectar,
    pedir_pasada_completa,
)
from tests.test_avisos import _config

TS = 1_700_000_000.0


def _filas(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute("SELECT wallet, dia, tipo, n, sol, ultima FROM actividad_wallet"))
    finally:
        conn.close()


class Volcado(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "alertas.db"

    def test_suma_entre_volcados(self):
        actividad = WalletActivity(self.path)
        actividad.count(WALLET, BUY, 0.5, TS)
        actividad.count(WALLET, BUY, 0.25, TS + 60)
        actividad.count(WALLET, TX_TRANSFER, ts=TS)
        self.assertEqual(actividad.flush(), 2)
        actividad.count(WALLET, BUY, 1.0, TS - 60)
        self.assertEqual(actividad.flush(), 1)
        dia = day_of(TS)
        self.assertEqual(_filas(self.path), [
            (WALLET, dia, BUY, 3, 1.75, TS + 60),
            (WALLET, dia, TX_TRANSFER, 1, 0.0, TS),
        ])
        # Sin nada pendiente no abre la base.
        self.assertEqual(actividad.flush(), 0)

    def test_un_dia_por_fila(self):
        actividad = WalletActivity(self.path)
        actividad.count(WALLET, BUY, 0.5, TS)
        actividad.count(WALLET, BUY, 0.5, TS + 86400)
        actividad.flush()
        self.assertEqual([f[1] for f in _filas(self.path)], [day_of(TS), day_of(TS + 86400)])

    def test_si_no_puede_escribir_guarda_la_cuenta(self):
        self.path.mkdir()  # un directorio no se puede abrir como base
        actividad = WalletActivity(self.path)
        actividad.count(WALLET, BUY, 0.5, TS)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(actividad.flush(), 0)
        self.path.rmdir()
        self.assertEqual(actividad.flush(), 1)
        self.assertEqual(_filas(self.path)[0][3], 1)

    def test_due_cada_flush_seconds(self):
        reloj = [0.0]
        actividad = WalletActivity(self.path, clock=lambda: reloj[0])
        self.assertFalse(actividad.due())
        reloj[0] = FLUSH_SECONDS
        self.assertTrue(actividad.due())
        actividad.flush()
        self.assertFalse(actividad.due())

    def test_leer_sin_tabla(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(read_activity(conn, "2026-01-01"), [])


class _Contador:
    def __init__(self):
        self.cuentas = []

    def count(self, wallet, kind, sol=0.0, ts=None):
        self.cuentas.append((wallet, kind, round(sol, 4)))


class ConteoDelWatcher(ConFicheroDeWallets):
    async def _conectado(self, rpc, actividad):
        watcher = WalletWatcher(RpcClient(rpc, "http://rpc"), self.path, 0.1, activity=actividad)
        await watcher.poll()
        conectar(watcher)
        ws = _FakeWs()
        await watcher.sync_subscriptions(ws)
        watcher.handle_message({"jsonrpc": "2.0", "id": ws.enviados[0]["id"], "result": 77})
        return watcher

    async def test_cuenta_lo_que_salta_y_lo_que_lee(self):
        compra = {**_tx(post_tokens=[_saldo(TOKEN, 1)], sol_gastado=0.5), "blockTime": TS}
        rpc = _FakeRpc(firmas=["VIEJA"], txs={"C": compra})
        actividad = _Contador()
        watcher = await self._conectado(rpc, actividad)
        watcher.handle_message(_aviso(77, "F", err={"e": 1}))
        watcher.handle_message(_aviso(77, "T", logs=_LOGS_TRANSFERENCIA))
        watcher.handle_message(_aviso(77, "C", logs=_LOGS_SWAP))
        [buy] = await watcher.poll()
        self.assertEqual(buy.signature, "C")
        self.assertEqual(actividad.cuentas, [
            (WALLET, TX_FAILED, 0.0), (WALLET, TX_TRANSFER, 0.0), (WALLET, BUY, 0.5),
        ])
        # La pasada completa no vuelve a contar las ya vistas por el WebSocket.
        rpc.firmas = ["C", "T", "F", "VIEJA"]
        pedir_pasada_completa(watcher)
        self.assertEqual(await watcher.poll(), [])
        self.assertEqual(len(actividad.cuentas), 3)

    async def test_sin_registro_no_cuenta_nada(self):
        rpc = _FakeRpc(firmas=["VIEJA"])
        watcher = await self._conectado(rpc, None)
        watcher.handle_message(_aviso(77, "T", logs=_LOGS_TRANSFERENCIA))
        self.assertEqual(await watcher.poll(), [])


class AlParar(unittest.IsolatedAsyncioTestCase):
    async def test_vuelca_al_salir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "alertas.db"
        radar = MemecoinRadar(_config(alert_log_path=str(path)))

        async def seguir(blacklist):
            radar._activity.count(WALLET, BUY, 0.5, time.time())
            raise asyncio.CancelledError

        radar._follow = seguir
        with self.assertRaises(asyncio.CancelledError):
            await radar.run()
        [fila] = _filas(path)
        self.assertEqual(fila[2:5], (BUY, 1, 0.5))


if __name__ == "__main__":
    unittest.main()
