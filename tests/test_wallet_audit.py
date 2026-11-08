"""Tests de la revisión de wallets en la cadena (wallet_audit.py)."""

import asyncio
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.buyers import TransactionReader
from sigpump.wallet_audit import (
    BUY,
    BUY_CURVE,
    BUY_SMALL,
    BUY_STABLE,
    OTHER,
    SELL,
    Ledger,
    Report,
    WalletAudit,
    WalletAuditor,
    classify,
    read_wallets,
)
from sigpump.wallets import LAMPORTS_PER_SOL, WSOL_MINT

WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
OTRA = "7Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j9"
TOKEN = "TokenMint1111111111111111111111111111111pump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AHORA = 1_800_000_000.0


def _saldo(mint, cantidad, owner=WALLET):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(int(cantidad * 10**6)), "decimals": 6}}


def _tx(sol=0.0, pre=(), post=(), firma="F", ts=AHORA - 60, logs=(), err=None):
    """Transacción de WALLET (cuenta 0) que gasta `sol` SOL (negativo = recibe)."""
    return {
        "blockTime": ts,
        "transaction": {"signatures": [firma], "message": {"accountKeys": [{"pubkey": WALLET}]}},
        "meta": {
            "err": err,
            "fee": 5000,
            "preBalances": [int(10 * LAMPORTS_PER_SOL)],
            "postBalances": [int((10 - sol) * LAMPORTS_PER_SOL) - 5000],
            "preTokenBalances": list(pre),
            "postTokenBalances": list(post),
            "logMessages": list(logs),
        },
    }


class Clasificacion(unittest.TestCase):
    def test_compra_de_min_sol_o_mas(self):
        clase = classify(_tx(0.5, post=[_saldo(TOKEN, 1000)]), WALLET, min_sol=0.1)
        self.assertEqual((clase.kind, clase.mints, clase.sol_spent), (BUY, (TOKEN,), 0.5))

    def test_compra_por_debajo_de_min_sol(self):
        # parse_buys la descarta en silencio: aquí se cuenta aparte.
        self.assertEqual(classify(_tx(0.05, post=[_saldo(TOKEN, 1000)]), WALLET, 0.1).kind, BUY_SMALL)

    def test_pagar_solo_la_renta_no_es_compra(self):
        # Recibir un token pagando la cuenta (~0,002 SOL) es un airdrop.
        self.assertEqual(classify(_tx(0.002, post=[_saldo(TOKEN, 1000)]), WALLET, 0.1).kind, OTHER)

    def test_compra_con_stable(self):
        tx = _tx(0.0, pre=[_saldo(USDC, 100)], post=[_saldo(USDC, 0), _saldo(TOKEN, 1000)])
        self.assertEqual(classify(tx, WALLET, 0.1).kind, BUY_STABLE)

    def test_compra_con_wsol(self):
        tx = _tx(0.0, pre=[_saldo(WSOL_MINT, 1)], post=[_saldo(WSOL_MINT, 0.5), _saldo(TOKEN, 1000)])
        self.assertEqual(classify(tx, WALLET, 0.1).kind, BUY)

    def test_bonding_curve_por_los_logs(self):
        # Por un agregador, pump.fun solo aparece en una llamada interna.
        logs = ["Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]", f"Program {PUMP} invoke [2]"]
        tx = _tx(0.5, post=[_saldo(TOKEN, 1000)], logs=logs)
        self.assertEqual(classify(tx, WALLET, 0.1).kind, BUY_CURVE)

    def test_bonding_curve_aunque_sea_pequena(self):
        tx = _tx(0.05, post=[_saldo(TOKEN, 1000)], logs=[f"Program {PUMP} invoke [1]"])
        self.assertEqual(classify(tx, WALLET, 0.1).kind, BUY_CURVE)

    def test_venta(self):
        tx = _tx(-0.4, pre=[_saldo(TOKEN, 1000)], post=[_saldo(TOKEN, 0)])
        self.assertEqual(classify(tx, WALLET, 0.1).kind, SELL)

    def test_envio_de_sol_es_otra(self):
        self.assertEqual(classify(_tx(1.0), WALLET, 0.1).kind, OTHER)

    def test_fallida(self):
        tx = _tx(0.5, post=[_saldo(TOKEN, 1000)], err={"InstructionError": [0, "Custom"]})
        self.assertEqual(classify(tx, WALLET, 0.1).kind, "fallida")

    def test_saldos_de_otra_wallet_no_cuentan(self):
        tx = _tx(0.0, post=[_saldo(TOKEN, 1000, owner=OTRA)])
        self.assertEqual(classify(tx, WALLET, 0.1).kind, OTHER)


class Diagnostico(unittest.TestCase):
    def _audit(self, **kinds):
        audit = WalletAudit(WALLET, "w", read=sum(kinds.values()) or 0)
        audit.kinds.update(kinds)
        return audit

    def test_inactiva(self):
        self.assertTrue(self._audit().diagnosis(0.1, 30).startswith("inactiva"))

    def test_lista_negra_manda(self):
        audit = self._audit(**{BUY: 3})
        audit.banned = "bot"
        self.assertEqual(audit.diagnosis(0.1, 30), "lista negra: bot")

    def test_solo_ventas(self):
        self.assertTrue(self._audit(**{SELL: 4, OTHER: 2}).diagnosis(0.1, 30).startswith("no compra"))

    def test_solo_pequenas_con_mediana(self):
        audit = self._audit(**{BUY_SMALL: 3, BUY_CURVE: 1})
        audit.spent[BUY_SMALL] = [0.02, 0.05, 0.08]
        self.assertEqual(audit.diagnosis(0.1, 30), "solo compras < 0.1 SOL (mediana 0.050)")

    def test_con_stable_avisa_igual(self):
        # parse_buys cuenta las compras con stable aunque no gasten SOL.
        audit = self._audit(**{BUY_STABLE: 2, BUY_CURVE: 3})
        audit.tokens, audit.tokens_in_db = {TOKEN}, 1
        self.assertEqual(audit.diagnosis(0.1, 30), "ok")

    def test_solo_curva(self):
        self.assertEqual(self._audit(**{BUY_CURVE: 5, BUY_SMALL: 1}).diagnosis(0.1, 30), "solo bonding curve")

    def test_compra_sin_rastro_en_la_base(self):
        audit = self._audit(**{BUY: 2})
        audit.tokens = {TOKEN}
        self.assertEqual(audit.diagnosis(0.1, 30), "compra pero no está en la base")
        audit.tokens_in_db = 1
        self.assertEqual(audit.diagnosis(0.1, 30), "ok")


class _FakeRpc:
    """getSignaturesForAddress y getTransaction por wallet; sin
    getTransactionsForAddress, para que TransactionReader use el método rpc."""

    def __init__(self, firmas, txs):
        self.firmas = firmas
        self.txs = txs
        self.calls = 0
        self.credits = 0
        self.metodos = []

    async def call(self, method, params):
        self.calls += 1
        self.metodos.append(method)
        if method == "getSignaturesForAddress":
            return self.firmas.get(params[0], [])
        if method == "getTransaction":
            return self.txs.get(params[0])
        raise ValueError("Method not found")


def _firma(nombre, ts, err=None):
    return {"signature": nombre, "blockTime": ts, "err": err}


class Auditor(unittest.TestCase):
    def _auditar(self, rpc, audit, days=30):
        auditor = WalletAuditor(rpc, TransactionReader(rpc, 50, "rpc"), 0.1, days, now=AHORA)
        return asyncio.run(auditor.audit(audit))

    def test_cuenta_actividad_y_clasifica(self):
        firmas = [
            _firma("c", AHORA - 60),
            _firma("p", AHORA - 120),
            _firma("v", AHORA - 180),
            _firma("x", AHORA - 240, err={"e": 1}),
            _firma("vieja", AHORA - 10 * 86400),
        ]
        txs = {
            "c": _tx(0.5, post=[_saldo(TOKEN, 1000)], firma="c"),
            "p": _tx(0.05, post=[_saldo("Otro", 10)], firma="p"),
            "v": _tx(-0.3, pre=[_saldo(TOKEN, 1000)], post=[_saldo(TOKEN, 0)], firma="v"),
            "vieja": _tx(1.0, firma="vieja", ts=AHORA - 10 * 86400),
        }
        audit = self._auditar(_FakeRpc({WALLET: firmas}, txs), WalletAudit(WALLET, "w"))
        self.assertEqual((audit.txs_7d, audit.failed_7d, audit.last_ts), (4, 1, AHORA - 60))
        # La fallida no se lee: las firmas ya dicen que falló.
        self.assertEqual(audit.read, 4)
        self.assertEqual((audit.kinds[BUY], audit.kinds[BUY_SMALL], audit.kinds[SELL]), (1, 1, 1))
        self.assertEqual(audit.tokens, {TOKEN})
        self.assertEqual(audit.last_buy_ts, AHORA - 60)
        self.assertEqual(audit.spent[BUY_SMALL], [0.05])

    def test_sin_actividad_en_la_ventana_no_lee_transacciones(self):
        rpc = _FakeRpc({WALLET: [_firma("vieja", AHORA - 40 * 86400)]}, {})
        audit = self._auditar(rpc, WalletAudit(WALLET, "w"))
        self.assertEqual(rpc.metodos, ["getSignaturesForAddress"])
        self.assertTrue(audit.diagnosis(0.1, 30).startswith("inactiva"))

    def test_error_del_rpc_queda_en_la_wallet(self):
        class _Roto(_FakeRpc):
            async def call(self, method, params):
                raise ValueError("429 Too Many Requests https://x/?api-key=SECRETO")

        audit = self._auditar(_Roto({}, {}), WalletAudit(WALLET, "w"))
        self.assertNotIn("SECRETO", audit.error)
        self.assertTrue(audit.diagnosis(0.1, 30).startswith("error"))


class SoloLectura(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_fichero_no_se_toca_aunque_haya_repetidas(self):
        path = self.dir / "wallets.txt"
        texto = f"# comentario\n{WALLET} * # ballena\n{WALLET}\nbasura\n{OTRA}\n"
        path.write_text(texto, encoding="utf-8")
        self.assertEqual(read_wallets(path), {WALLET: "ballena", OTRA: f"{OTRA[:4]}…{OTRA[-4:]}"})
        self.assertEqual(path.read_text(encoding="utf-8"), texto)

    def test_base_lista_negra_vigente_y_tokens(self):
        path = self.dir / "alertas.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE lista_negra (wallet TEXT PRIMARY KEY, motivo TEXT, fecha TEXT, timestamp REAL, hasta REAL)")
        db.execute("CREATE TABLE alertas (wallet TEXT, token TEXT)")
        db.executemany("INSERT INTO lista_negra (wallet, motivo, hasta) VALUES (?, ?, ?)",
                       [(WALLET, "bot", None), (OTRA, "abandonada", AHORA - 1), ("Z", "castigo", AHORA + 99)])
        db.executemany("INSERT INTO alertas VALUES (?, ?)", [(WALLET, TOKEN), (WALLET, TOKEN), (OTRA, "B")])
        db.commit()
        db.close()
        antes = path.stat().st_mtime_ns
        ledger = Ledger(path)
        self.assertEqual(ledger.banned(AHORA), {WALLET: "bot", "Z": "castigo"})
        self.assertEqual(ledger.tokens(WALLET), {TOKEN})
        with self.assertRaises(sqlite3.OperationalError):
            ledger._db.execute("DELETE FROM alertas")
        ledger.close()
        self.assertEqual(path.stat().st_mtime_ns, antes)

    def test_sin_base(self):
        ledger = Ledger(self.dir / "no_existe.db")
        self.assertEqual((ledger.banned(AHORA), ledger.tokens(WALLET)), ({}, set()))
        self.assertFalse((self.dir / "no_existe.db").exists())


class Informe(unittest.TestCase):
    def test_tabla_y_resumen(self):
        ok = WalletAudit(WALLET, "ballena", read=3, tokens={TOKEN}, tokens_in_db=1)
        ok.kinds[BUY] = 3
        inactiva = WalletAudit(OTRA, "pez", last_ts=AHORA - 40 * 86400)
        rpc = _FakeRpc({}, {})
        out = io.StringIO()
        Report(0.1, 30, 50).print([ok, inactiva], rpc, AHORA, out=out)
        texto = out.getvalue()
        # Primero las que no avisan.
        self.assertLess(texto.index("pez"), texto.index("ballena"))
        self.assertIn("1/1", texto)
        self.assertIn("40 d", texto)
        self.assertIn("1  inactiva", texto)
        self.assertIn("1  ok", texto)



class DesdeLaBase(unittest.TestCase):
    """--base: la actividad que guardó el bot, sin RPC."""

    def test_rellena_por_dia(self):
        from sigpump.activity import day_of
        from sigpump.wallet_audit import fill_from_activity

        hoy, hace_10 = day_of(AHORA), day_of(AHORA - 10 * 86400)
        filas = [
            (WALLET, hoy, BUY, 2, 1.0, AHORA - 60),
            (WALLET, hoy, "fallida", 3, 0.0, AHORA - 30),
            (WALLET, hace_10, BUY_SMALL, 4, 0.2, AHORA - 10 * 86400),
        ]
        audit = fill_from_activity(WalletAudit(WALLET, "w"), filas, AHORA)
        self.assertEqual((audit.read, audit.txs_7d, audit.failed_7d), (9, 5, 3))
        self.assertEqual((audit.last_ts, audit.last_buy_ts), (AHORA - 30, AHORA - 60))
        self.assertEqual(audit.spent[BUY_SMALL], [0.05] * 4)
        self.assertEqual(day_of(audit.oldest_read_ts), hace_10)

    def test_compra_sin_filas_en_la_base(self):
        audit = WalletAudit(WALLET, "w", read=2, rows_in_db=0)
        audit.kinds[BUY] = 2
        self.assertEqual(audit.diagnosis(0.1, 30), "compra pero no está en la base")
        audit.rows_in_db = 2
        self.assertEqual(audit.diagnosis(0.1, 30), "ok")

    def test_informe_sin_rpc(self):
        audit = WalletAudit(WALLET, "ballena", read=2, rows_in_db=1, oldest_read_ts=AHORA - 86400)
        audit.kinds[BUY] = 2
        out = io.StringIO()
        Report(0.1, 30, None).print([audit], None, AHORA, out=out)
        texto = out.getvalue()
        self.assertIn("Actividad guardada por el bot", texto)
        self.assertIn("Datos desde", texto)
        self.assertNotIn("Llamadas al RPC", texto)

    def test_informe_con_tabla_vacia(self):
        out = io.StringIO()
        Report(0.1, 30, None).print([WalletAudit(WALLET, "w")], None, AHORA, out=out)
        self.assertIn("La tabla está vacía", out.getvalue())
        self.assertNotIn("inactiva", out.getvalue())


if __name__ == "__main__":
    unittest.main()
