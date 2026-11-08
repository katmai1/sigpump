# sigpump

Seguimiento de solo-lectura de wallets de Solana. No ejecuta trades: avisa
por Telegram cuando una de las wallets seguidas compra un token, con los
datos de mercado del par en [DexScreener](https://dexscreener.com/), y
registra cada compra para medir después qué wallets vale la pena copiar.

## Cómo funciona

El bucle principal ([sigpump/radar.py](sigpump/radar.py)) consulta cada
`[wallets].interval_seconds` las transacciones nuevas de las wallets
listadas en `[wallets].file` (una por línea, con la etiqueta detrás de `#`)
y avisa por Telegram con un 👛 cuando una compra un token
([sigpump/wallets.py](sigpump/wallets.py)). Con `websocket = true` el RPC
avisa de la actividad de cada wallet y solo se leen esas transacciones;
cada `full_poll_minutes` se consultan todas por si se perdió algún aviso.

Para cada compra:

1. Se trae de DexScreener el par del token (`GET /tokens/v1/{chainId}/{addrs}`,
   [sigpump/screener.py](sigpump/screener.py)), quedándose con el de más
   liquidez entre los que cumplen `dex_ids` y `quote_tokens`.
2. Se comprueba en la blockchain que el token no se pueda acuñar ni congelar
   ([sigpump/solana.py](sigpump/solana.py)). De paso se lee su comisión de
   transferencia (Token-2022): si tiene, el aviso la muestra con un ⚠️, porque
   se cobra al comprar y al vender y los retornos del registro no la descuentan.
3. Se aplican los filtros y cooldowns de `[wallets]`.
4. Si pasa, se avisa por Telegram ([sigpump/telegram.py](sigpump/telegram.py))
   y se registra en `alert_log_path` ([sigpump/tracker.py](sigpump/tracker.py))
   con el retorno a +5, +15 y +30 minutos y a +1, +4 y +24 horas. Las descartadas también se
   registran, con su motivo.

## Seguimiento de wallets

Una compra se detecta por los saldos de la wallet antes y después de la
transacción: sube el de un token mientras gasta SOL (al menos `min_sol`) o
USDC/USDT. No depende del DEX ni del agregador por el que se haga, y recibir
un token sin pagar (airdrops de spam) no cuenta. Si en una misma
transacción entra en varios tokens, lo pagado se reparte entre ellos a
partes iguales. El aviso indica si es una
entrada nueva o amplía una posición, cuánto pagó y qué otras wallets
seguidas entraron en el mismo token en los últimos `confluence_minutes`.

Las compras de tokens que todavía están en la bonding curve de un launchpad
(pump.fun, Meteora DBC, LaunchLab, Moonshot) se ignoran, salvo con
`bonding_curve = true`: entonces se avisan marcadas con 🧪, y si el token ya
tiene pool en un AMM se usa ese. A esas solo se les aplican `quote_tokens`,
`max_price_change_h1_pct`, `max_price_change_m5_pct`, `min_txns_m5` y `min_volume_h1_usd` (el dex, la
edad, el market cap y el turnover las descartarían siempre), se registran con
`bonding_curve = 1`, su precio se sigue por el token y no por el pool (que
cambia al graduarse) y no cuentan para la lista negra por rug ni por
resultados: casi todos los tokens sin graduar mueren, y echarían a wallets
que en los AMM aciertan. Se descartan las de tokens que se pueden acuñar o
congelar, que quedan registradas con `enviada = 0`. El aviso es por quién compra, no por cómo está el mercado:
del par solo se miran el dex y la moneda contra la que cotiza
(`[dexscreener].dex_ids` y `quote_tokens`) y los filtros propios de
`[wallets]` que se explican más abajo.

Al arrancar solo se toma como punto de partida la última transacción de cada
wallet: no se avisa del historial. El fichero se relee cuando cambia.

Usa el `rpc_url` de `[solana]`. Con 10-15 wallets cada 15 segundos el RPC
público se queda corto; un plan gratuito de Helius o QuickNode alcanza.

Con `min_wallets = 2` (o más) solo se avisa cuando ese número de wallets
seguidas distintas compraron el mismo token dentro de `confluence_minutes`:
la primera compra queda registrada con `enviada = 0` y el aviso sale con la
que completa el número. Las compras recientes se recuperan del registro al
reiniciar, así que un reinicio entre las dos no pierde el aviso.

Hasta el 2026-11-07 había wallets de confianza (un `*` detrás de la
dirección en el fichero), que avisaban solas y se saltaban parte de los
filtros del par. Se quitaron: ahora todas pasan los mismos filtros. Un `*`
que quede en el fichero se ignora.

`cooldown_minutes` deja un aviso por wallet y token en esa ventana. Con 24 h
(1440) no se repiten las wallets que van promediando un token cada pocas
horas: en octubre una mandó 22 avisos del mismo token mientras caía de 4,5M
a 466k de market cap, y las recompras que se quitaban así daban -21% de
mediana a 24 h, frente al -3% de las que quedaban. Se recupera del registro
al reiniciar.

Para no recibir el mismo token una y otra vez, `token_cooldown_minutes` deja
un solo aviso por token en esa ventana; solo se vuelve a avisar, marcado como
🔁 actualización, si se suman `realert_new_wallets` wallets más que en el
último aviso (0 = no reavisar nunca: con los datos, que coincidieran más
wallets en un token no lo hacía mejor, sino peor). `max_tokens_per_hour` silencia las wallets que compran de todo
(bots o degens): ni avisan ni cuentan para la confluencia de las demás, y
con `blacklist` entran en la lista negra.

`max_txs_per_hour` detecta los bots: una wallet con más transacciones que
eso en la última hora (de cualquier tipo, contadas por su firma antes de
leerlas) entra en la lista negra con `blacklist`. Cada transacción
leída es una consulta al RPC, y un bot de trading con cientos por minuto
agota los créditos de Helius en horas.

La lista negra (`blacklist = true`) se guarda en la tabla `lista_negra` de
la base del registro (`alert_log_path`): una fila por wallet con su motivo
y, si es temporal, `hasta` (epoch). Para meter o sacar una a mano:

```
sqlite3 alertas.db "INSERT OR REPLACE INTO lista_negra (wallet, motivo) VALUES ('<dirección>', 'a mano')"
sqlite3 alertas.db "DELETE FROM lista_negra WHERE wallet = '<dirección>'"
```

Se relee en cada vuelta. Las permanentes se quitan del fichero de wallets:
para volver a seguir una hay que borrarla de la tabla y añadirla al
fichero. Un `blacklist_file` de versiones anteriores se pasa a la base al
arrancar y se renombra a `.importado`.

`max_inactive_days` (30 por defecto) manda a la lista negra, con
`blacklist`, las wallets abandonadas: las que llevan más de esos días
sin ninguna transacción. Se mira al empezar a seguirlas (por la hora de su
última transacción) y en cada pasada completa; una sin historial cuenta
desde que se empieza a seguir. No es para siempre: siguen en el fichero de
wallets, no se consultan durante otros `max_inactive_days` y al acabar se
vuelve a mirar su última transacción; si siguen sin operar, vuelven a
entrar por el mismo tiempo.

Con `blacklist`, también entran solas en la lista negra las wallets cuya
compra cae `rug_drop_pct` (90% por defecto) en el seguimiento, y las que con
al menos `loser_min_signals` tokens medidos tienen una mediana de
`loser_max_median_ret_pct` o peor en el checkpoint `loser_checkpoint_minutes`
(30 min por defecto; en 1440, a 24 h, se ven las que a 30 min parecían
buenas y se hundían después). Se quitan del fichero de wallets y, si se
vuelven a añadir, se quitan otra vez. La de rugs solo cuenta las compras
cuyo par pasó los filtros (dex, moneda, edad, cambio 1h, market cap,
actividad, volumen sobre market cap y autoridades): un rug en un par de 3 minutos que nunca se iba a
avisar no dice nada de la wallet. La de resultados las cuenta todas (menos
las de bonding curve): elegir mal tokens es elegir mal aunque no se avisen,
y con los filtros estrictos casi nunca llegaría a tener tokens medidos.

Cada aviso lleva el porcentaje de aciertos de la wallet en el registro: de
la primera compra de cada token (sin las de bonding curve), cuántas llegaron
a +`hit_pct` (8% por defecto) sin haber caído antes más de
`hit_max_drop_pct` (15%), es decir, las que con ese objetivo y ese stop se
habrían cerrado ganando. Se mira en todas las muestras del seguimiento, en
orden (columna `camino_precio`). Una compra que todavía no llegó al objetivo
ni cayó hasta el stop no cuenta hasta que acaba su seguimiento (a las 24 h);
entonces cuenta como fallo. Las compras registradas antes del 2026-11-07
no guardaban el orden de los precios: se rellenan a mano (las que no se
pueden resolver no cuentan) con
`path_backfill.py` (ver [Rellenar los aciertos de antes](#rellenar-los-aciertos-de-antes)).
Mientras la wallet no tenga ninguna medida, el aviso dice "sin datos
todavía" en vez del porcentaje.

Con `bot_volume_min_txns_m5` (0 = apagado) el aviso lleva un ⚠️ de volumen
de bots cuando en los últimos 5 minutos hubo al menos esas transacciones y,
o bien su media es de menos de `bot_volume_max_usd_per_tx` USD, o bien las
compras están a menos de `bot_volume_buy_margin_pct` puntos del 50%: un bot
comprando y vendiendo céntimos para inflar la actividad, sin compradores
reales detrás. Solo advierte, no descarta: el 2026-11-07, como filtro, habría
quitado 16 de 694 avisos con resultados apenas peores que el resto.

Con `ranking_hour` (hora local, -1 = apagado) llega cada día un ranking de
las wallets seguidas con al menos 5 tokens medidos a 1 h, por la mediana de
sus tokens a 1 h, 4 h y 24 h, de mejor a peor, para ver cuáles sobran. Con los datos de
octubre el pasado de una wallet predice: ordenándolas por su mediana hasta
el 19, el mejor tercio dio después +3% a 1 h y el peor -9%; a 24 h, -28%
frente a -65%.

`min_pair_age_minutes`, `max_price_change_h1_pct` y `min_market_cap_usd`
descartan las compras en pares recién creados, que ya subieron mucho en la
última hora o de market cap pequeño: con los primeros datos eran las que
casi siempre perdían. `min_txns_m5` y `min_volume_h1_usd` descartan los
tokens muertos, con pocas transacciones en los últimos 5 minutos o poco
volumen en la última hora. Vienen a 0: desde el 2026-11-01 están
desactivados, porque con la edad y el turnover ya filtrados lo que quitaban
(casi la mitad de los avisos) no daba peor que lo que quedaba. Son tokens
de poca liquidez, donde el retorno medido aguanta menos tamaño.
`max_price_change_m5_pct` descarta los que ya subieron mucho en los
últimos 5 minutos, también en bonding curve: es llegar tarde a la vela.
`max_turnover_h1` descarta los que mueven en la última hora más de esa
fracción de su market cap (0.1 = 10%): los están bombeando o ya están
saliendo, y en octubre el 64% de los que pasaban del 5% cayó más de 50% en
los días siguientes, frente al 28% del resto. Quedan en el registro con su
motivo.

## Registro de compras

`alertas.db` (configurable con `[radar].alert_log_path`) es una base SQLite
con una tabla `alertas`: una fila por compra de una wallet seguida
(`tipo = 'wallet'`) con los datos de mercado del momento y cómo le fue
después:

- `ret_5m_pct`, `ret_15m_pct`, `ret_30m_pct`: cambio de precio respecto del
  aviso. El precio se consulta en cada vuelta del seguimiento, así que cada
  columna usa la primera muestra desde ese minuto (hasta 5 minutos más
  tarde; si no hay muestra en ese margen, por ejemplo tras un reinicio,
  queda en NULL).
- `ret_60m_pct`, `ret_240m_pct`, `ret_1440m_pct`: lo mismo a +1, +4 y +24 h.
  Pasados los 30 minutos el precio solo se consulta cuando se abre la
  ventana de cada uno, con un margen de un cuarto del plazo (+24 h acepta
  una muestra hasta +30 h).
- `mejor_ret_30m_pct`, `peor_ret_30m_pct`: el mejor y el peor precio visto
  en las muestras de los primeros 30 minutos. De acá sale la lista negra
  por rug; la de resultados usa el checkpoint de `loser_checkpoint_minutes`.
- `camino_precio`: JSON con lo peor visto en todo el seguimiento (`peor`) y,
  en orden, cada nuevo máximo con lo peor visto antes de llegar a él
  (`picos`: `[[máximo, peor antes], ...]`). De acá salen los aciertos del
  aviso. NULL en las filas de antes del 2026-11-07 hasta pasar
  `path_backfill.py`; las que rellena llevan `fuente` (`registro`,
  `geckoterminal` o `sin resolver`).
- `enviada = 0` son compras descartadas, con `motivo_descarte`: sirven para
  comprobar si los filtros tiran señales buenas.
- `pasa_filtros_par = 1` si el par pasó los filtros: se avisó, o se descartó
  solo por confluencia, cooldown o wallet hiperactiva. En las filas de
  versiones anteriores se rellena al arrancar con los filtros de ese momento.
- `wallet`, `wallet_etiqueta`, `sol_gastado`, `stable_gastado`,
  `entrada_nueva`, `wallets_confluencia` y `tx`: quién compró, cuánto, si ya
  tenía el token y cuántas otras wallets seguidas habían entrado antes.
- `txns_m5`, `aceleracion_volumen`, `compras_m5_pct`, `moneda_par` y
  `edad_par_min`: actividad del momento, contra qué cotiza el par y cuánto
  llevaba vivo al avisar.
- `comision_transferencia_bps`: comisión de transferencia del token en
  puntos básicos (300 = 3%), 0 si no tiene y NULL si no se pudo consultar o
  `check_token_authorities` está apagado.

Las bases creadas por versiones anteriores conservan las filas y columnas
del radar de trending (`tipo` 'alerta' o 'prealerta', `score`, velas...):
no se tocan, y las filas nuevas las dejan vacías.

Se puede abrir con `sqlite3 alertas.db` o con cualquier visor de SQLite
(p. ej. DB Browser for SQLite) mientras corre. Por ejemplo, el retorno
mediano de cada wallet:

```sql
SELECT wallet_etiqueta, COUNT(*) AS n,
       ROUND(AVG(ret_30m_pct), 1) AS ret_30m,
       ROUND(AVG(mejor_ret_30m_pct), 1) AS mejor
FROM alertas WHERE tipo = 'wallet'
GROUP BY wallet ORDER BY ret_30m DESC;
```

### Actividad de las wallets

La tabla `actividad_wallet` cuenta, por wallet, día (hora local) y tipo,
lo que hace cada wallet seguida, también lo que no llega a `alertas`:
`compra` (`min_sol` o más), `compra pequeña`, `compra con stable`,
`bonding curve`, `venta`, `otra`, y las que el WebSocket descarta sin leer
(`fallida`, `transferencia`). Guarda `n`, el SOL gastado (`sol`) y la hora
de la última (`ultima`). Son contadores y no una fila por transacción (las
wallets seguidas hacían más de 28.000 a la semana): unos pocos MB al año.
Se vuelcan cada 5 minutos y al parar; un corte pierde como mucho ese rato.
No cuesta créditos: el bot ya lee esas transacciones. La tabla se crea en el
primer volcado; las bases de antes no cambian en nada más.

## Requisitos

- Python 3.11 o superior (usa `tomllib` de la librería estándar).
- Un RPC de Solana con API key (Helius, QuickNode...): el público corta
  enseguida.
- Un bot de Telegram (token de [@BotFather](https://t.me/BotFather)) y el
  `chat_id` del chat/grupo/canal donde se enviarán los avisos.

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuración

Copiá `config.example.toml` a `config.toml` y completá al menos
`telegram.bot_token` y `telegram.chat_id`:

```bash
cp config.example.toml config.toml
```

`config.toml` está en `.gitignore` porque contiene credenciales reales —
nunca lo subas al repositorio.

Secciones disponibles:

- `[dexscreener]` — `chain_id`, `quote_tokens` (contra qué monedas debe
  cotizar el par) y `dex_ids` (en qué dex debe estar el pool: pumpswap,
  raydium...).
- `[radar]` — `alert_log_path` (la base SQLite de resultados) y `verbose`.
- `[solana]` — `rpc_url`, que usan el seguimiento de wallets y
  `check_token_authorities`: comprueba en la blockchain que nadie pueda
  acuñar más tokens (mintAuthority) ni congelar los tuyos (freezeAuthority).
  Si el RPC no responde la señal sale igual y queda el aviso en el log.
- `[wallets]` — seguimiento de wallets: `file` con las direcciones,
  intervalo, `min_sol` (gasto mínimo para contar como compra), cooldown por
  wallet y token, ventana de confluencia, `min_wallets` (cuántas wallets
  tienen que coincidir en un token para avisar), un aviso por token
  (`token_cooldown_minutes`, `realert_new_wallets`), tope de actividad
  (`max_tokens_per_hour`), bots (`max_txs_per_hour`), abandonadas (`max_inactive_days`), lista negra (`blacklist`, `rug_drop_pct`,
  `loser_min_signals`, `loser_max_median_ret_pct`, `loser_checkpoint_minutes`), ranking diario
  (`ranking_hour`), aciertos del aviso (`hit_pct`, `hit_max_drop_pct`), advertencia de volumen
  de bots (`bot_volume_min_txns_m5`, `bot_volume_max_usd_per_tx`,
  `bot_volume_buy_margin_pct`) y filtros del par
  (`min_pair_age_minutes`, `max_price_change_h1_pct`, `max_price_change_m5_pct`,
  `min_market_cap_usd`, `min_txns_m5`, `min_volume_h1_usd`, `max_turnover_h1`) y avisos en bonding
  curve (`bonding_curve`).
- `[telegram]` — `bot_token`, `chat_id` y opcionalmente `message_thread_id`
  si querés mandar los avisos a un tema (topic) concreto dentro de un
  grupo con "Temas" activados.

Un `config.toml` de una versión anterior sigue funcionando: las secciones y
claves del radar de trending que ya no existen solo se avisan en el log.

## Uso

```bash
python run.py --config config.toml
```

El proceso corre indefinidamente hasta que se lo interrumpa (Ctrl+C).

Cada hora deja en el log un resumen ([sigpump/summary.py](sigpump/summary.py)):
compras vistas, avisos enviados, descartes agrupados por motivo (la clave del
filtro que los tiró, `min_wallets`, `token ya avisado`...), compras sin pool
o repetidas en cooldown, wallets seguidas y castigadas, estado del WebSocket
y llamadas al RPC desde el resumen anterior:

```
Resumen de 1 h: 42 compras de wallets seguidas, 5 avisos, 30 descartadas (dex_ids 12, min_txns_m5 8, ...), 4 sin pool, 3 repetidas en cooldown | 48 wallets (2 castigadas), WebSocket conectado | 3120 llamadas al RPC
```

### Revisar las wallets en la cadena

La base solo guarda las compras que llegan al radar: las de menos de
`min_sol` no dejan rastro y las de bonding curve (sin `bonding_curve`) solo
salen en el log. Para saber por qué una wallet no avisa,
[sigpump/wallet_audit.py](sigpump/wallet_audit.py) lee en el RPC sus últimas
transacciones y las clasifica: compras de `min_sol` o más, pequeñas, con
stablecoin o en bonding curve (por el programa de la transacción, no por
DexScreener), ventas y otras. Cruza las compras con `alertas.db` y da un
diagnóstico por wallet (inactiva, solo vende, solo compras pequeñas, solo
bonding curve, compra pero no está en la base, ok). No modifica ni
`wallets.txt` ni la base.

```bash
python wallet_audit.py                       # todas, últimas 50 tx de 30 días
python wallet_audit.py --wallet 5Q54 7Q54    # solo esas (dirección o su principio)
python wallet_audit.py --base --days 7       # sin RPC: lo que contó el bot
```

Con `--base` no gasta nada: el mismo informe sale de la tabla
`actividad_wallet` (ver [Actividad de las wallets](#actividad-de-las-wallets)),
que solo tiene datos desde que el bot corre con esta versión. Ahí las
transacciones se cuentan todas, no solo las últimas `--txs`.

Gasta créditos: por wallet, una página de firmas (1 crédito) y sus últimas
`--txs` transacciones (10 créditos por cada 100 con Helius, 1 por
transacción con `--method rpc`). Las wallets sin transacciones en `--days`
días no pasan de la página de firmas.

### Rellenar los aciertos de antes

[sigpump/path_backfill.py](sigpump/path_backfill.py) rehace `camino_precio`
de las primeras compras de antes del 2026-11-07 (sin las de bonding curve),
para que cuenten en los aciertos del aviso. Con el mejor y el peor de los
primeros 30 minutos y los checkpoints de 1 h, 4 h y 24 h ya se sabe el
resultado, salvo cuando en esa media hora se tocaron el objetivo y el stop
(789 de 2629): para esas pide velas de 1 minuto del pool a GeckoTerminal
(API pública, sin créditos del RPC; una llamada cada 6 s, unos 80 min) y
mira qué llegó antes. Si las dos caen en el mismo minuto cuenta el stop
primero. Si las velas del pool no llegan a las dos marcas (o GeckoTerminal no
tiene velas de ese pool) prueba con otros dos pools del mismo token (`pool`
en el JSON dice cuál); si tampoco, se queda con el camino del registro, que
pone el peor antes del mejor, y sale como `sin resolver`: esas no cuentan
en los aciertos (no se sabe si acertaron). Con `--reintentar` se vuelven a
pasar, y solo esas se sobrescriben.

Los caminos de `registro` valen para el `hit_pct` y el `hit_max_drop_pct`
con los que se rellenaron: si se cambian, hay que borrarlos y volver a
pasar. Nunca pisa un camino del seguimiento en directo.

```bash
python path_backfill.py --cache velas.json              # solo el informe
python path_backfill.py --cache velas.json --escribir   # lo guarda en la base
python path_backfill.py --cache velas.json --escribir --reintentar   # otra vez las sin resolver
```

Para deshacerlo:
`UPDATE alertas SET camino_precio = NULL WHERE json_extract(camino_precio, '$.fuente') IS NOT NULL`.

## Tests

Los tests usan solo `unittest` de la stdlib (no hace falta instalar nada
más) y no tocan la red: la sesión HTTP, el RPC y el bot de Telegram son
dobles.

```bash
python -m unittest discover -s tests -t .
```

## Estructura del proyecto

```
run.py                    CLI: parsea argumentos y arranca el seguimiento
sigpump/
  config.py                Carga y validación de config.toml
  radar.py                  Orquesta el ciclo compra -> filtros -> aviso -> registro
  filters.py                Filtros del par y a qué compras se aplica cada uno
  cooldowns.py              Cooldowns, confluencia y actividad de las compras recientes
  wallets.py                 Detección de las compras de las wallets (RPC y WebSocket)
  wallet_list.py            Fichero de wallets, lista negra y castigos temporales
  summary.py                Resumen periódico en el log
  screener.py                 Cliente HTTP async de DexScreener
  solana.py                   Cliente del RPC y comprobación de mint/freeze authority
  telegram.py                 Formateo y envío de los avisos por Telegram
  tracker.py                  Registro SQLite de las compras y su resultado
  blacklist.py                Lista negra de wallets (tabla lista_negra)
  activity.py                 Actividad por wallet y día (tabla actividad_wallet)
  buyers.py                   Herramienta aparte: quién compró un token
  wallet_audit.py             Herramienta aparte: qué hace en la cadena cada wallet
  path_backfill.py            Herramienta aparte: aciertos de las compras de antes del 2026-11-07
  util.py                     Conversión defensiva y métricas de los campos de la API
buyers.py                 CLI de sigpump/buyers.py (gasta créditos del RPC)
wallet_audit.py           CLI de sigpump/wallet_audit.py (gasta créditos del RPC)
path_backfill.py          CLI de sigpump/path_backfill.py (GeckoTerminal, sin créditos)
tests/                    Tests (stdlib unittest, sin red)
```
