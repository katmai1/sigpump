# sigpump

Seguimiento de solo lectura de wallets de Solana: avisa por Telegram cuando
una wallet seguida compra un token y registra en SQLite cómo le fue después.
**No ejecuta trades.** El README.md explica cada filtro y su porqué; leelo
antes de tocar la lógica de filtros, cooldowns o lista negra.

## Esta carpeta es la instancia en vivo

El bot corre desde acá. Estos ficheros son de producción y están en `.gitignore`:

- `config.toml`: credenciales reales (Telegram, API key del RPC). Nunca
  mostrar sus secretos en la salida, ni copiarlos a otros ficheros, ni
  subirlos al repo.
- `alertas.db`: el registro de compras y la tabla `lista_negra`. El bot
  escribe en ella mientras corre.
- `wallets.txt`: las wallets seguidas. El bot lo relee cuando cambia y quita
  solo las que entran en la lista negra.

Reglas:

- Consultar `alertas.db` solo en modo lectura:
  `sqlite3 -readonly alertas.db "..."`. Nada de `INSERT`/`UPDATE`/`DELETE`/
  `ALTER` sin permiso explícito.
- Antes de cambiar `config.toml` o `wallets.txt`, preguntar y mostrar el diff.
- No arrancar, parar ni reiniciar el bot sin que se pida. Para probar, usar
  los tests, no `run.py` contra el config real: manda avisos de verdad a
  Telegram.
- `buyers.py` y `wallet_audit.py` gastan créditos del RPC: ejecutarlos solo si se pide.

## Comandos

```bash
.venv/bin/python -m unittest discover -s tests -t .   # todos los tests (stdlib, sin red)
.venv/bin/python -m unittest tests.test_avisos         # un módulo
.venv/bin/python run.py --config config.toml           # el bot (no lanzarlo sin pedirlo)
.venv/bin/python buyers.py <MINT> --minutes 10         # quién compró un token (gasta créditos)
.venv/bin/python wallet_audit.py --txs 50              # qué hace cada wallet en la cadena (gasta créditos)
.venv/bin/python wallet_audit.py --base                # lo mismo desde actividad_wallet (gratis)
.venv/bin/python path_backfill.py --cache velas.json  # aciertos de antes del 7-nov (GeckoTerminal; --escribir toca la base)
```

Python 3.11+ (usa `tomllib`); el `.venv` es 3.13. Dependencias en
`requirements.txt`: solo `aiohttp` y `python-telegram-bot`. No añadir más sin
consultarlo.

## Arquitectura

- `run.py`: CLI, logging, SIGTERM → `MemecoinRadar.run()`.
- `sigpump/radar.py`: orquesta compra → par de DexScreener → autoridades →
  filtros/cooldowns → aviso → registro. El nombre `MemecoinRadar` y las filas
  `tipo` 'alerta'/'prealerta' son restos del antiguo radar de trending.
- `sigpump/wallets.py`: detección de compras por saldos antes/después
  (polling RPC + WebSocket, pasada completa cada `full_poll_minutes`).
  `WalletWatcher` sondea, lee transacciones y echa bots y abandonadas.
  `classify` dice qué fue cada transacción (compra, pequeña, curva, venta...).
- `sigpump/activity.py`: `WalletActivity`, contadores por wallet, día y tipo
  en la tabla `actividad_wallet` (se vuelcan cada 5 min y al parar).
- `sigpump/wallet_list.py`: `WalletList`, el fichero de wallets menos la
  lista negra y los castigos temporales.
- `sigpump/cooldowns.py`: `Cooldowns`, lo que el radar recuerda de las
  compras recientes (cooldowns, confluencia, actividad por wallet).
- `sigpump/summary.py`: resumen horario en el log (avisos, descartes por
  motivo, WebSocket, llamadas al RPC).
- `sigpump/filters.py`: tabla `PAIR_FILTERS` con los filtros del par y si
  cada uno se aplica en bonding curve.
- `sigpump/screener.py` (DexScreener), `solana.py` (`RpcClient`, compartido
  por todo lo que llama al RPC, y mint/freeze authority),
  `telegram.py` (formato y envío), `tracker.py` (SQLite y retornos a
  +5m…+24h), `blacklist.py` (tabla `lista_negra`), `config.py` (carga y
  validación del TOML), `util.py` (conversión defensiva de campos de la API).
- `sigpump/buyers.py`: herramienta aparte, a mano (`buyers.py` en la raíz la lanza).
- `sigpump/wallet_audit.py`: herramienta aparte, a mano: clasifica las últimas
  transacciones de cada wallet del fichero y las cruza con la base (solo lee).
  Con `--base` sale de `actividad_wallet`, sin RPC ni créditos.
- `sigpump/path_backfill.py`: herramienta aparte, a mano: rellena `camino_precio`
  de las compras de antes del 2026-11-07 (velas de GeckoTerminal donde hace falta).
  Con `--escribir` escribe en `alertas.db`: solo con permiso.

Todo es asyncio. Los tests usan dobles para la sesión HTTP, el RPC y el bot:
ningún test toca la red.

## Compatibilidad

- Bases y `config.toml` de versiones anteriores tienen que seguir
  funcionando: las columnas nuevas se añaden con migración al arrancar, las
  filas viejas no se tocan, y las claves que ya no existen solo se avisan en
  el log.
- Lo que se pierda al reiniciar (cooldowns, confluencia) se recupera del
  registro.

## Cambios en filtros y umbrales

Los filtros se deciden con datos. Antes de proponer un filtro nuevo o cambiar
un umbral:

1. Consultar `alertas.db` en solo lectura (filas `tipo = 'wallet'`; las
   descartadas tienen `enviada = 0` y `motivo_descarte`) y medir el efecto:
   cuántos avisos quita y qué retorno daban los que quita frente a los que
   quedan (`ret_*_pct`, normalmente mediana a 1 h, 4 h y 24 h).
2. Mostrar los números y la consulta antes de cambiar nada.
3. Dejar la justificación con fecha en el comentario de la clave en
   `config.example.toml`, en el estilo de los que ya hay
   ("2026-10-22: con 24 h se quitaban 83 de 146 avisos...").

Tener en cuenta la excepción existente: las compras en bonding curve tienen
su propio subconjunto de filtros (ver README). Las wallets de confianza (`*`)
se quitaron el 2026-11-07. Ese subconjunto se define solo en `PAIR_FILTERS`;
`tests/test_filters.py` comprueba que README, `config.example.toml` y
`config.py` digan lo mismo, así que al cambiar uno hay que ajustar los textos.

## Un cambio completo incluye

- Tests nuevos o ajustados, y la suite entera pasando.
- `README.md` actualizado si cambia el comportamiento o una clave.
- `config.example.toml` con la clave y su comentario explicativo.
- La clave aplicada también en `config.toml` local, **previa confirmación**.

## Estilo

- Todo en español: código de dominio, comentarios, docstrings, logs, mensajes
  de Telegram, README y commits. Columnas SQLite en español (`enviada`,
  `motivo_descarte`...).
- Los comentarios explican el porqué (un caso real, un dato), no el qué.
  Mantener esa densidad.
- Seguir el estilo de los módulos existentes: docstring de módulo con su
  ruta, type hints, `log = logging.getLogger()`.

## Git

- Rama de trabajo: `dev`; `main` es la principal.
- No hacer commit ni push salvo que se pida. Cuando se pida: mensajes cortos,
  en español y en minúscula, describiendo el cambio
  (p. ej. "lista negra en la base; las abandonadas se vuelven a comprobar").
