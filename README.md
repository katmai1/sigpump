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
   ([sigpump/solana.py](sigpump/solana.py)).
3. Se aplican los filtros y cooldowns de `[wallets]`.
4. Si pasa, se avisa por Telegram ([sigpump/telegram.py](sigpump/telegram.py))
   y se registra en `alert_log_path` ([sigpump/tracker.py](sigpump/tracker.py))
   con el retorno a +5, +15 y +30 minutos. Las descartadas también se
   registran, con su motivo.

## Seguimiento de wallets

Una compra se detecta por los saldos de la wallet antes y después de la
transacción: sube el de un token mientras gasta SOL (al menos `min_sol`) o
USDC/USDT. No depende del DEX ni del agregador por el que se haga, y recibir
un token sin pagar (airdrops de spam) no cuenta. El aviso indica si es una
entrada nueva o amplía una posición, cuánto pagó y qué otras wallets
seguidas entraron en el mismo token en los últimos `confluence_minutes`.

Se ignoran las compras de tokens que todavía están en la bonding curve de un
launchpad (pump.fun, Meteora DBC, LaunchLab, Moonshot) y se descartan las de
tokens que se pueden acuñar o congelar; estas últimas quedan registradas con
`enviada = 0`. El aviso es por quién compra, no por cómo está el mercado:
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
reiniciar, así que un reinicio entre las dos no pierde el aviso. Una wallet
marcada de confianza (un `*` detrás de la dirección en el fichero) avisa sola,
aunque no llegue a `min_wallets`.

Para no recibir el mismo token una y otra vez, `token_cooldown_minutes` deja
un solo aviso por token en esa ventana; solo se vuelve a avisar, marcado como
🔁 actualización, si se suman `realert_new_wallets` wallets más que en el
último aviso. `max_tokens_per_hour` silencia las wallets que compran de todo
(bots o degens): ni avisan ni cuentan para la confluencia de las demás, y
con `blacklist_file` entran en la lista negra.

`max_txs_per_hour` detecta los bots: una wallet con más transacciones que
eso en la última hora (de cualquier tipo, contadas por su firma antes de
leerlas) entra en la lista negra con `blacklist_file`. Cada transacción
leída es una consulta al RPC, y un bot de trading con cientos por minuto
agota los créditos de Helius en horas.

Con `blacklist_file`, también entran solas en la lista negra las wallets cuya
compra cae `rug_drop_pct` (90% por defecto) en el seguimiento, y las que con
al menos `loser_min_signals` tokens medidos tienen una mediana a 30 min de
`loser_max_median_ret_pct` o peor. Se quitan del fichero de wallets y, si se
vuelven a añadir, se quitan otra vez.

`min_pair_age_minutes`, `max_price_change_h1_pct` y `min_market_cap_usd`
descartan las compras en pares recién creados, que ya subieron mucho en la
última hora o de market cap pequeño: con los primeros datos eran las que
casi siempre perdían. Quedan en el registro con su motivo.

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
- `mejor_ret_30m_pct`, `peor_ret_30m_pct`: el mejor y el peor precio visto
  en esas muestras. De acá salen la lista negra por rug y por resultados.
- `enviada = 0` son compras descartadas, con `motivo_descarte`: sirven para
  comprobar si los filtros tiran señales buenas.
- `wallet`, `wallet_etiqueta`, `sol_gastado`, `stable_gastado`,
  `entrada_nueva`, `wallets_confluencia` y `tx`: quién compró, cuánto, si ya
  tenía el token y cuántas otras wallets seguidas habían entrado antes.
- `txns_m5`, `aceleracion_volumen`, `compras_m5_pct`, `moneda_par` y
  `edad_par_min`: actividad del momento, contra qué cotiza el par y cuánto
  llevaba vivo al avisar.

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
  (`max_tokens_per_hour`), bots (`max_txs_per_hour`), lista negra (`blacklist_file`, `rug_drop_pct`,
  `loser_min_signals`, `loser_max_median_ret_pct`) y filtros del par
  (`min_pair_age_minutes`, `max_price_change_h1_pct`, `min_market_cap_usd`).
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
  wallets.py                 Detección de las compras de las wallets (RPC y WebSocket)
  screener.py                 Cliente HTTP async de DexScreener
  solana.py                   Comprobación de mint/freeze authority
  telegram.py                 Formateo y envío de los avisos por Telegram
  tracker.py                  Registro SQLite de las compras y su resultado
  util.py                     Conversión defensiva y métricas de los campos de la API
tests/                    Tests (stdlib unittest, sin red)
```
