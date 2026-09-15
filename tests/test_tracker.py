"""Tests del registro de resultados: escritura en SQLite, checkpoints de
retorno, recuperación tras reinicio y tolerancia a fallos de la base."""

import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sigpump.signals import CandleStats, EarlySignal
from sigpump.tracker import CHECKPOINT_TOLERANCE_MINUTES, COLUMNS, AlertTracker


def _pair(address="TOK", pool="POOL", price="1.0"):
    return {
        "chainId": "solana",
        "pairAddress": pool,
        "baseToken": {"address": address, "symbol": "X"},
        "priceUsd": price,
        "priceChange": {"m5": 5, "h1": 40, "h6": 60},
        "volume": {"m5": 3_000, "h1": 12_000},
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "liquidity": {"usd": 80_000},
        "marketCap": 900_000,
        "url": "https://dexscreener.com/solana/POOL",
    }


class _FakeClient:
    def __init__(self, prices=None):
        # pool -> precio actual
        self.prices = prices if prices is not None else {}
        self.requested: list[list[str]] = []

    async def get_pairs_for_tokens(self, chain_id, addresses):
        self.requested.append(list(addresses))
        return [
            {"pairAddress": pool, "priceUsd": str(price)} for pool, price in self.prices.items()
        ]


class TestAlertTracker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "alertas.db"

    def _tracker(self):
        tracker = AlertTracker(self.path)
        self.addCleanup(tracker.close)
        return tracker

    def _rows(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM alertas ORDER BY id")]
        finally:
            conn.close()

    def _age(self, minutes):
        """Hace que las filas parezcan registradas hace `minutes` minutos más."""
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("UPDATE alertas SET timestamp = timestamp - ?", (minutes * 60,))
        conn.close()

    def test_record_escribe_la_fila_con_los_datos_del_momento(self):
        self._tracker().record(_pair(), 72.5, CandleStats(40.0, 3.0), sent=True)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), ["id", *COLUMNS])
        row = rows[0]
        self.assertEqual(row["enviada"], 1)
        self.assertIsNone(row["motivo_descarte"])
        self.assertEqual(row["score"], 72.5)
        self.assertEqual(row["precio_usd"], 1.0)
        self.assertEqual(row["aceleracion_volumen"], 3.0)
        self.assertEqual(row["compras_m5_pct"], 75.0)
        self.assertEqual(row["sobre_minimo_1h_pct"], 40.0)
        self.assertEqual(row["bajo_maximo_15m_pct"], 3.0)
        self.assertIsNone(row["ret_5m_pct"])

    def test_descarte_guarda_el_motivo(self):
        self._tracker().record(_pair(), 70.0, None, sent=False, reason="llega tarde")
        row = self._rows()[0]
        self.assertEqual((row["enviada"], row["motivo_descarte"]), (0, "llega tarde"))
        self.assertIsNone(row["sobre_minimo_1h_pct"])

    async def test_anota_checkpoints_y_mejor_peor(self):
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        client = _FakeClient({"POOL": 1.2})

        self._age(5)
        await tracker.update(client, "solana")
        client.prices = {"POOL": 0.9}
        self._age(10)
        await tracker.update(client, "solana")
        client.prices = {"POOL": 1.1}
        self._age(15)
        await tracker.update(client, "solana")

        row = self._rows()[0]
        self.assertEqual(row["ret_5m_pct"], 20.0)
        self.assertEqual(row["ret_15m_pct"], -10.0)
        self.assertEqual(row["ret_30m_pct"], 10.0)
        self.assertEqual(row["mejor_ret_30m_pct"], 20.0)
        self.assertEqual(row["peor_ret_30m_pct"], -10.0)
        # Terminada: no se vuelve a pedir su precio.
        await tracker.update(client, "solana")
        self.assertEqual(len(client.requested), 3)

    async def test_antes_del_primer_checkpoint_solo_actualiza_mejor_peor(self):
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        self._age(2)
        await tracker.update(_FakeClient({"POOL": 1.5}), "solana")
        row = self._rows()[0]
        self.assertIsNone(row["ret_5m_pct"])
        self.assertEqual(row["mejor_ret_30m_pct"], 50.0)

    async def test_checkpoint_fuera_del_margen_queda_vacio(self):
        """Tras un reinicio la primera muestra puede llegar a los 21 min: no
        se anota como retorno a +5m ni a +15m."""
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        self._age(15 + CHECKPOINT_TOLERANCE_MINUTES + 1)
        await tracker.update(_FakeClient({"POOL": 2.0}), "solana")
        row = self._rows()[0]
        self.assertEqual((row["ret_5m_pct"], row["ret_15m_pct"]), (None, None))

    async def test_usa_el_pool_de_la_alerta_y_no_otro_del_mismo_token(self):
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        self._age(5)
        await tracker.update(_FakeClient({"OTRO_POOL": 3.0}), "solana")
        self.assertIsNone(self._rows()[0]["ret_5m_pct"])

    async def test_pasado_el_margen_deja_de_seguir(self):
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        self._age(30 + CHECKPOINT_TOLERANCE_MINUTES)
        client = _FakeClient({"POOL": 1.0})
        await tracker.update(client, "solana")
        self.assertEqual(client.requested, [])

    async def test_sin_precio_en_la_alerta_no_se_sigue(self):
        tracker = self._tracker()
        tracker.record(_pair(price=None), 70.0, None, sent=True)
        client = _FakeClient({"POOL": 1.0})
        self._age(5)
        await tracker.update(client, "solana")
        self.assertEqual(client.requested, [])

    def test_prealerta_guarda_tipo_y_arranque(self):
        tracker = self._tracker()
        early = EarlySignal(10.0, 5.0, 3.3, 0.76, 12.0)
        tracker.record(_pair(), 55.0, None, sent=True, kind="prealerta", early=early)
        row = self._rows()[0]
        self.assertEqual(row["tipo"], "prealerta")
        self.assertEqual(
            (row["arranque_pct"], row["ratio_volumen_5m"], row["ratio_txns_5m"]), (10.0, 5.0, 3.3)
        )
        self.assertTrue(tracker.is_tracking("TOK", sent=True, kind="prealerta"))
        self.assertFalse(tracker.is_tracking("TOK", sent=True))

    def test_alerta_normal_sin_datos_de_arranque(self):
        self._tracker().record(_pair(), 70.0, None, sent=True)
        row = self._rows()[0]
        self.assertEqual(row["tipo"], "alerta")
        self.assertIsNone(row["arranque_pct"])

    def test_filas_sin_tipo_cuentan_como_alerta(self):
        # Bases creadas antes de las prealertas: la columna tipo se agrega vacía.
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=True)
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("UPDATE alertas SET tipo = NULL")
        conn.close()
        self.assertTrue(tracker.is_tracking("TOK", sent=True))
        self.assertFalse(tracker.is_tracking("TOK", sent=True, kind="prealerta"))

    def test_is_tracking_distingue_enviadas_de_descartadas(self):
        tracker = self._tracker()
        tracker.record(_pair(), 70.0, None, sent=False, reason="tarde")
        self.assertTrue(tracker.is_tracking("TOK", sent=False))
        self.assertFalse(tracker.is_tracking("TOK", sent=True))
        self.assertFalse(tracker.is_tracking("OTRO", sent=False))

    async def test_continua_las_pendientes_tras_reiniciar(self):
        anterior = self._tracker()
        anterior.record(_pair(), 70.0, None, sent=True)
        anterior.close()
        tracker = self._tracker()
        with self.assertLogs(level=logging.INFO) as logs:
            tracker.open()
        self.assertIn("1 en seguimiento", "".join(logs.output))
        self._age(5)
        await tracker.update(_FakeClient({"POOL": 1.3}), "solana")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ret_5m_pct"], 30.0)

    def test_base_de_una_version_anterior_gana_las_columnas_nuevas(self):
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("CREATE TABLE alertas (id INTEGER PRIMARY KEY, fecha TEXT, token TEXT)")
            conn.execute("INSERT INTO alertas (fecha, token) VALUES ('ayer', 'VIEJO')")
        conn.close()
        self._tracker().record(_pair(), 70.0, None, sent=True)
        rows = self._rows()
        self.assertEqual([r["token"] for r in rows], ["VIEJO", "TOK"])
        self.assertEqual(rows[1]["score"], 70.0)

    def test_archivo_que_no_es_una_base_desactiva_el_registro_sin_tocarlo(self):
        self.path.write_bytes(b"esto no es sqlite" * 10)
        tracker = self._tracker()
        with self.assertLogs(level=logging.ERROR):
            tracker.open()
        tracker.record(_pair(), 70.0, None, sent=True)
        self.assertFalse(tracker.is_tracking("TOK", sent=True))
        self.assertEqual(self.path.read_bytes(), b"esto no es sqlite" * 10)

    def test_ruta_invalida_no_propaga(self):
        tracker = AlertTracker(self.path / "no_existe" / "alertas.db")
        with self.assertLogs(level=logging.ERROR):
            tracker.record(_pair(), 70.0, None, sent=True)
        # Desactivado: no vuelve a intentarlo (ni a loguearlo) en cada pasada.
        with self.assertNoLogs(level=logging.ERROR):
            tracker.record(_pair(), 70.0, None, sent=True)


if __name__ == "__main__":
    unittest.main()
