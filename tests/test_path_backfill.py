"""Tests de path_backfill: caminos rehechos para las compras de antes del
2026-11-07, sin red (GeckoTerminal con una sesión doble)."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sigpump.path_backfill import (
    FROM_CANDLES, FROM_RECORD, UNRESOLVED, GeckoTerminal, build_paths, needs_candles, path_from_candles,
    path_from_record, pending_rows, write_paths,
)
from sigpump.tracker import AlertTracker, path_hit

T0 = 1_790_000_000.0


def _fila(**valores):
    fila = {
        "id": 1, "wallet": "W", "token": "T", "pool": "P", "timestamp": T0, "precio_usd": 1.0,
        "mejor_ret_30m_pct": 5.0, "peor_ret_30m_pct": -5.0,
        "ret_60m_pct": None, "ret_240m_pct": None, "ret_1440m_pct": None,
    }
    fila.update(valores)
    return fila


def _vela(minuto, alto, bajo):
    """Vela de 1 min con máximo y mínimo en % sobre un precio de entrada de 1."""
    return [int(T0) + 60 * minuto, 1.0, 1 + alto / 100, 1 + bajo / 100, 1.0, 100.0]


class _Respuesta:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _Sesion:
    def __init__(self, *respuestas):
        self._respuestas = list(respuestas)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self._respuestas.pop(0)


class _Gecko(GeckoTerminal):
    async def _wait(self, seconds):
        pass


def _pools(*direcciones):
    return {"data": [{"attributes": {"address": d}} for d in direcciones]}


def _velas(*velas):
    return {"data": {"attributes": {"ohlcv_list": list(velas)}}}


class TestCaminoDelRegistro(unittest.TestCase):
    def test_sin_tocar_nada_y_acierta_en_un_checkpoint(self):
        path = path_from_record(_fila(ret_60m_pct=-3.0, ret_240m_pct=12.0))
        self.assertTrue(path_hit(path, 8, 15))
        self.assertEqual(path["fuente"], FROM_RECORD)

    def test_cae_al_stop_y_despues_sube_es_fallo(self):
        path = path_from_record(_fila(peor_ret_30m_pct=-20.0, ret_240m_pct=30.0))
        self.assertFalse(path_hit(path, 8, 15))

    def test_toca_el_objetivo_sin_stop_es_acierto(self):
        path = path_from_record(_fila(mejor_ret_30m_pct=10.0, peor_ret_30m_pct=-12.0, ret_60m_pct=-40.0))
        self.assertTrue(path_hit(path, 8, 15))

    def test_sin_tocar_nada_en_24_h_queda_sin_decidir(self):
        # hit_rates lo cuenta como fallo al acabar el seguimiento.
        self.assertIsNone(path_hit(path_from_record(_fila(ret_1440m_pct=2.0)), 8, 15))

    def test_solo_las_que_tocan_las_dos_marcas_piden_velas(self):
        self.assertTrue(needs_candles(_fila(mejor_ret_30m_pct=8.0, peor_ret_30m_pct=-16.0), 8, 15))
        self.assertFalse(needs_candles(_fila(mejor_ret_30m_pct=8.0, peor_ret_30m_pct=-15.0), 8, 15))
        self.assertFalse(needs_candles(_fila(mejor_ret_30m_pct=7.9, peor_ret_30m_pct=-30.0), 8, 15))


class TestCaminoDeLasVelas(unittest.TestCase):
    FILA = _fila(mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0)

    def test_objetivo_antes_que_el_stop_es_acierto(self):
        path = path_from_candles(self.FILA, [_vela(0, 2, -3), _vela(1, 11, 0), _vela(2, 0, -20)], 8, 15)
        self.assertTrue(path_hit(path, 8, 15))
        self.assertEqual(path["fuente"], FROM_CANDLES)

    def test_stop_antes_que_el_objetivo_es_fallo(self):
        path = path_from_candles(self.FILA, [_vela(1, 0, -18), _vela(2, 11, 0)], 8, 15)
        self.assertFalse(path_hit(path, 8, 15))

    def test_las_dos_en_el_mismo_minuto_cuenta_el_stop_primero(self):
        path = path_from_candles(self.FILA, [_vela(1, 11, -18)], 8, 15)
        self.assertFalse(path_hit(path, 8, 15))

    def test_velas_desordenadas_y_fuera_de_ventana(self):
        velas = [_vela(2, 0, -18), _vela(1, 11, 0), _vela(-5, 0, -50), _vela(40, 0, -50)]
        self.assertTrue(path_hit(path_from_candles(self.FILA, velas, 8, 15), 8, 15))

    def test_sin_llegar_a_las_dos_marcas_no_resuelve(self):
        self.assertIsNone(path_from_candles(self.FILA, [_vela(1, 6, -18)], 8, 15))
        self.assertIsNone(path_from_candles(self.FILA, [], 8, 15))


class TestGeckoTerminal(unittest.IsolatedAsyncioTestCase):
    async def test_429_se_reintenta(self):
        sesion = _Sesion(_Respuesta(429), _Respuesta(200, _velas(_vela(1, 1, 0))))
        gecko = _Gecko(sesion)
        self.assertEqual(await gecko.candles("P", T0), [_vela(1, 1, 0)])
        self.assertEqual(gecko.calls, 2)
        self.assertIn("/pools/P/ohlcv/minute", sesion.urls[0])
        self.assertIn(f"before_timestamp={int(T0) + 36 * 60}", sesion.urls[0])

    async def test_pool_desconocido_sin_velas(self):
        self.assertEqual(await _Gecko(_Sesion(_Respuesta(404))).candles("P", T0), [])

    async def test_build_paths_usa_la_cache_y_marca_las_no_resueltas(self):
        filas = [
            _fila(id=1),
            _fila(id=2, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0),
            _fila(id=3, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0),
            _fila(id=4, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0),
        ]
        cache = {"2": [_vela(1, 11, 0), _vela(2, 0, -20)]}
        sesion = _Sesion(
            _Respuesta(200, _velas(_vela(1, 0, -18), _vela(2, 11, 0))),
            _Respuesta(200, _velas()),
            _Respuesta(200, _pools("P")),  # el token no tiene otro pool
        )
        paths, fuentes = await build_paths(filas, _Gecko(sesion), 8, 15, cache)
        self.assertEqual(len(sesion.urls), 3)
        self.assertEqual(fuentes, {FROM_RECORD: 1, FROM_CANDLES: 2, UNRESOLVED: 1})
        self.assertTrue(path_hit(paths[2], 8, 15))
        self.assertFalse(path_hit(paths[3], 8, 15))
        self.assertFalse(path_hit(paths[4], 8, 15))
        self.assertEqual(cache["4"], [])

    async def test_sin_velas_en_el_pool_prueba_otro_del_token(self):
        fila = _fila(id=5, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0)
        sesion = _Sesion(
            _Respuesta(200, _velas()),
            _Respuesta(200, _pools("P", "Q", "R", "S")),
            _Respuesta(200, _velas(_vela(1, 6, -18))),  # Q: no llega al objetivo
            _Respuesta(200, _velas(_vela(1, 11, 0), _vela(2, 0, -20))),
        )
        cache = {}
        paths, fuentes = await build_paths([fila], _Gecko(sesion), 8, 15, cache)
        self.assertEqual(fuentes, {FROM_CANDLES: 1})
        self.assertTrue(path_hit(paths[5], 8, 15))
        self.assertEqual(paths[5]["pool"], "R")
        self.assertIn("/pools/R/ohlcv/minute", sesion.urls[3])
        self.assertIn("token=T", sesion.urls[3])
        self.assertIn("/tokens/T/pools", sesion.urls[1])
        # Con todo en la caché no vuelve a llamar.
        paths, _ = await build_paths([fila], None, 8, 15, cache)
        self.assertEqual(paths[5]["pool"], "R")

    async def test_sin_red_deja_fuera_las_que_no_estan_en_la_cache(self):
        filas = [
            _fila(id=1),
            _fila(id=2, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0),
            _fila(id=3, mejor_ret_30m_pct=12.0, peor_ret_30m_pct=-20.0),
        ]
        paths, fuentes = await build_paths(filas, None, 8, 15, {"2": [_vela(1, 11, 0), _vela(2, 0, -20)]})
        self.assertEqual(sorted(paths), [1, 2])
        self.assertEqual(fuentes, {FROM_RECORD: 1, FROM_CANDLES: 1})


class TestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "alertas.db"
        tracker = AlertTracker(self.db)
        conn = tracker._db()
        viejo = time.time() - 3 * 86400

        def fila(token, **extra):
            valores = {
                "tipo": "wallet", "wallet": "W", "token": token, "pool": "P", "precio_usd": 1.0,
                "timestamp": viejo, "mejor_ret_30m_pct": 5.0, "peor_ret_30m_pct": -5.0, **extra,
            }
            conn.execute(
                f"INSERT INTO alertas ({', '.join(valores)}) VALUES ({', '.join('?' * len(valores))})",
                list(valores.values()),
            )

        fila("A")                                  # 1: se rellena
        fila("A")                                  # 2: compra repetida, no cuenta
        fila("B", camino_precio='{"peor": 0, "picos": []}')  # 3: ya tiene camino
        fila("C", bonding_curve=1)                 # 4: bonding curve
        fila("D", timestamp=time.time())           # 5: sigue en seguimiento
        fila("E", mejor_ret_30m_pct=None)          # 6: sin muestras
        fila("F", camino_precio='{"peor": -20, "picos": [], "fuente": "sin resolver"}')  # 7
        conn.commit()
        tracker.close()
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def test_solo_primeras_compras_terminadas_y_sin_camino(self):
        self.assertEqual([r["id"] for r in pending_rows(self.conn, time.time())], [1])
        self.assertEqual([r["id"] for r in pending_rows(self.conn, time.time(), retry=True)], [1, 7])

    def test_write_paths_reemplaza_las_sin_resolver(self):
        self.assertEqual(write_paths(self.conn, {7: {"peor": -20, "picos": [], "fuente": "geckoterminal"}}), 1)
        camino = self.conn.execute("SELECT camino_precio FROM alertas WHERE id = 7").fetchone()[0]
        self.assertEqual(json.loads(camino)["fuente"], "geckoterminal")

    def test_write_paths_no_pisa_un_camino(self):
        self.assertEqual(write_paths(self.conn, {1: {"peor": -1, "picos": [], "fuente": "registro"},
                                                 3: {"peor": -9, "picos": []}}), 1)
        caminos = dict(self.conn.execute("SELECT id, camino_precio FROM alertas WHERE id IN (1, 3)").fetchall())
        self.assertEqual(json.loads(caminos[1])["fuente"], "registro")
        self.assertEqual(json.loads(caminos[3]), {"peor": 0, "picos": []})


if __name__ == "__main__":
    unittest.main()
