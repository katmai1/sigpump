# sigpump

Radar de solo-lectura para memecoins trending en [DexScreener](https://dexscreener.com/).
No ejecuta trades: descubre candidatos, los puntúa según métricas de mercado
y avisa por Telegram cuando cruzan un umbral configurable.

## Cómo funciona

DexScreener no expone públicamente el ranking exacto de su página
"trending" (ese cálculo es interno). `sigpump` arma su propia señal
combinando boosts pagos, pools trending de GeckoTerminal, perfiles de token
recientes, community takeovers y ads como candidatos, y los puntúa con
métricas de mercado reales (volumen, momentum de precio, liquidez), usando
las APIs públicas de DexScreener y GeckoTerminal (sin API key):

- `GET /token-boosts/latest/v1` — tokens con promoción paga reciente
- `GET /token-boosts/top/v1` — tokens con más boosts activos
- `GET /token-profiles/latest/v1` — perfiles de token nuevos/actualizados
- `GET /community-takeovers/latest/v1` — community takeovers recientes
- `GET /ads/latest/v1` — tokens con anuncios recientes
- GeckoTerminal `GET /networks/{network}/trending_pools?page=N` — ranking de
  pools trending por actividad (20 por página, hasta 10 páginas)
- `GET /tokens/v1/{chainId}/{addrs}` — datos de mercado (volumen, liquidez,
  cambios de precio) para hasta 30 direcciones por llamada
- GeckoTerminal `GET /networks/{network}/pools/multi/{pools}` y
  `GET /networks/{network}/pools/{pool}/ohlcv/minute` — precio, liquidez y
  velas de 1 minuto de los pares que están por alertar, para verificarlos

Las fuentes de DexScreener devuelven solo 30 items de todas las chains, así
que por sí solas dan unos pocos candidatos por chain y casi siempre los
mismos; el ranking de GeckoTerminal es el que aporta volumen y rotación.

El bucle principal ([sigpump/radar.py](sigpump/radar.py)) repite cada
`poll_interval_seconds`:

1. Descubre candidatos (boosts + trending + perfiles/takeovers/ads).
2. Trae sus datos de mercado y aplica filtros duros: moneda contra la que
   cotiza el par (`quote_tokens`), liquidez, market cap, edad, volumen y txns
   de 1 hora y de 5 minutos, ratio de ventas (honeypots), tamaño medio de
   trade (wash trading) y subida máxima en 1h.
3. Calcula un score 0-100 ([sigpump/config.py](sigpump/config.py)). Premia
   que el movimiento esté empezando: aceleración del volumen y presión
   compradora de los últimos 5 minutos ([sigpump/signals.py](sigpump/signals.py)),
   y penaliza los tokens que ya subieron mucho en la última hora.
4. Los que superan `score_alert_threshold` y no están en cooldown se
   verifican contra GeckoTerminal: el precio tiene que coincidir con el de
   DexScreener, la liquidez real superar el mínimo y las velas de 1 minuto
   de la última hora no pueden mostrar desplomes bruscos. Con esas velas
   también se descartan los que llegan tarde: precio ya muy por encima del
   mínimo de la hora, o cayendo desde el máximo de los últimos 15 minutos.
5. Antes de avisar se comprueba en la blockchain que el token no se pueda
   acuñar ni congelar ([sigpump/solana.py](sigpump/solana.py)), y los que
   pasan se alertan por Telegram ([sigpump/telegram.py](sigpump/telegram.py)).
6. Cada alerta, y cada descarte por llegar tarde, se registra en
   `alert_log_path` ([sigpump/tracker.py](sigpump/tracker.py)) con los datos
   del momento y el retorno a +5, +15 y +30 minutos.

## Prealertas

Una pasada completa tarda 2-3 minutos (sobre todo por el espaciado que exige
GeckoTerminal) y el score usa ventanas de 5 minutos y 1 hora, así que cuando
sale una alerta el movimiento ya está en marcha. Para ganar margen, el radar
guarda una foto de cada pool en cada consulta: precio, volumen y txns de
5 minutos ([sigpump/watch.py](sigpump/watch.py)). Un segundo bucle consulta
cada `[watch].interval_seconds` solo los tokens vigilados, sin esperar a la
pasada completa. DexScreener refresca sus datos cada ~30 segundos, así que
ese es el mínimo útil.

Se manda una ⚡ PREALERTA cuando un pool que estaba tranquilo arranca frente a
su propia historia de hace 5-20 minutos: precio +4-30% sobre la mediana de
esa base, volumen y txns de 5 minutos multiplicados, mayoría de compras y
precio de 5 minutos subiendo. Se compara con la historia propia y no con la
media de la última hora porque esa media ya incluye la subida cuando esta
lleva unos minutos.

La prealerta pasa los mismos filtros duros que una alerta y, con
`verify_before_alert`, el contraste de precio y liquidez con GeckoTerminal,
pero no la revisión de velas. Se registra con `tipo = 'prealerta'`.

Con el primer día de datos, dos patrones dejaban las señales sin margen:

- **Prealertas repetidas del mismo token**: la primera del día daba de media
  +7,7% a 15 minutos; las siguientes, -0,8%. Por eso `[watch].cooldown_minutes`
  es de 6 horas.
- **Alertas completas después de una prealerta**: llegaban con la subida ya
  hecha y todas cayeron a 15 minutos. Con `[watch].suppress_alert_minutes` no
  se mandan si el token tuvo prealerta en ese plazo; quedan registradas con
  `enviada = 0` y el motivo, para comprobar que el filtro acierta.

Los cooldowns se recuperan del registro al reiniciar el radar.

Con cuatro días de datos (692 señales) se afinó lo siguiente:

- Los arranques que **no seguían** cumpliéndose a los 30-60 segundos daban
  -2,1% a 15 minutos, frente a +4,3% los que sí. De ahí
  `[watch].require_sustained_seconds`, que cuesta ~30 segundos de margen.
- Los pares de **menos de 2 horas** daban -5,3% a 15 minutos y -23,6% en el
  peor momento (`min_pair_age_minutes = 120`), aunque entre 2 y 6 horas eran
  los mejores.
- Los de **menos de $500 de volumen en 5 minutos** eran el peor tramo
  (`min_volume_m5_usd`).

## Seguimiento de wallets

Con `[wallets].enabled = true`, un tercer bucle consulta cada
`interval_seconds` las transacciones nuevas de las wallets listadas en
`[wallets].file` (una por línea, con la etiqueta detrás de `#`) y avisa por
Telegram con un 👛 cuando una compra un token ([sigpump/wallets.py](sigpump/wallets.py)).

Una compra se detecta por los saldos de la wallet antes y después de la
transacción: sube el de un token mientras gasta SOL (al menos `min_sol`) o
USDC/USDT. No depende del DEX ni del agregador por el que se haga, y recibir
un token sin pagar (airdrops de spam) no cuenta. El aviso indica si es una
entrada nueva o amplía una posición, cuánto pagó y qué otras wallets
seguidas entraron en el mismo token en los últimos `confluence_minutes`.

Se ignoran las compras de tokens que todavía están en la bonding curve de un
launchpad (pump.fun, Meteora DBC, LaunchLab, Moonshot) y se descartan las de
tokens que se pueden acuñar o congelar; estas últimas quedan registradas con
`enviada = 0`. No se aplican los filtros duros ni el score: el aviso es por
quién compra, no por cómo está el mercado. El score sí se calcula y se
muestra como referencia.

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
(bots o degens): ni avisan ni cuentan para la confluencia de las demás.

Con `blacklist_file`, las wallets cuya compra cae `rug_drop_pct` (90% por
defecto) en el seguimiento entran solas en la lista negra: se quitan del
fichero de wallets y, si se vuelven a añadir, se quitan otra vez.

Para recibir solo los avisos de wallets, desactivá las alertas
(`[radar].alerts_enabled = false`) y las prealertas (`[watch].enabled = false`).
Con las alertas apagadas y las prealertas encendidas, la pasada completa sigue
corriendo para elegir los tokens vigilados, pero no manda alertas.

## Registro de alertas

`alertas.db` (configurable con `[radar].alert_log_path`) es una base SQLite
con una tabla `alertas`: una fila por alerta con el score, los datos de
mercado del momento y cómo le fue después:

- `ret_5m_pct`, `ret_15m_pct`, `ret_30m_pct`: cambio de precio respecto de la
  alerta. El precio se consulta cada ~30 segundos (una vez por pasada si la
  vigilancia está apagada), así que cada columna usa
  la primera muestra desde ese minuto (hasta 5 minutos más tarde; si no hay
  muestra en ese margen, por ejemplo tras un reinicio, queda en NULL).
- `mejor_ret_30m_pct`, `peor_ret_30m_pct`: el mejor y el peor precio visto
  en esas muestras.
- `enviada = 0` son alertas descartadas por llegar tarde u omitidas por una
  prealerta previa, con `motivo_descarte`: sirven para comprobar si esos
  filtros tiran señales buenas.
- `sube_15m_pct`, `velas_verdes_seguidas`, `tendencia_volumen` y
  `mecha_superior`: cómo venía la subida en las velas cerradas antes de la
  señal. En las prealertas se completan unos segundos después, porque al
  avisar no se piden velas.
- `sostenido_30s`, `sostenido_60s` (prealertas): 1 si el arranque seguía
  cumpliéndose con los datos de ~30 y ~60 segundos después.
- `wallet`, `wallet_etiqueta`, `sol_gastado`, `stable_gastado`,
  `entrada_nueva`, `wallets_confluencia` y `tx` (`tipo = 'wallet'`): quién
  compró, cuánto, si ya tenía el token y cuántas otras wallets seguidas
  habían entrado antes.
- `txns_m5`, `moneda_par` y `edad_par_min`: actividad del momento, contra qué
  cotiza el par y cuánto llevaba vivo al avisar.

Se puede abrir con `sqlite3 alertas.db` o con cualquier visor de SQLite
(p. ej. DB Browser for SQLite) mientras el radar corre. Por ejemplo, el
retorno medio de las alertas enviadas frente a las descartadas:

```sql
SELECT COALESCE(tipo, 'alerta') AS tipo, enviada, COUNT(*) AS n,
       ROUND(AVG(ret_5m_pct), 1) AS ret_5m,
       ROUND(AVG(ret_30m_pct), 1) AS ret_30m,
       ROUND(AVG(mejor_ret_30m_pct), 1) AS mejor
FROM alertas GROUP BY 1, 2;
```

Con unos días de datos se puede ver qué valores de `aceleracion_volumen`,
`cambio_h1_pct` o `sobre_minimo_1h_pct` tenían las alertas que funcionaron
y ajustar pesos y umbrales en consecuencia.

La verificación existe porque DexScreener calcula el precio en USD de cada
par a partir del precio de su quote, y cuando ese cálculo está roto o
manipulado publica precios, volumen, liquidez y subidas absurdas (p. ej.
+440.000% en 1h y $261M de liquidez en un pool con $53k reales) que inflan
el score.

## Requisitos

- Python 3.11 o superior (usa `tomllib` de la librería estándar).
- Un bot de Telegram (token de [@BotFather](https://t.me/BotFather)) y el
  `chat_id` del chat/grupo/canal donde se enviarán las alertas.

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

- `[dexscreener]` — chain a monitorear y filtros duros sobre los datos de
  DexScreener: `quote_tokens` (contra qué monedas debe cotizar el par),
  mínimos de liquidez/volumen/market cap/edad, `min_txns_h1`, `min_txns_m5` y
  `min_volume_m5_usd` (actividad ahora mismo), `min_sell_ratio_h1`
  (honeypots), `max_avg_trade_usd` (wash trading) y `max_price_change_h1_pct`
  (subidas absurdas).
- `[solana]` — `check_token_authorities` comprueba en la blockchain que nadie
  pueda acuñar más tokens (mintAuthority) ni congelar los tuyos
  (freezeAuthority), con el `rpc_url` indicado. Si el RPC no responde la señal
  sale igual y queda el aviso en el log.
- `[geckoterminal]` — `trending_pages`: cuántas páginas del ranking de
  pools trending sumar como candidatos (0 desactiva la fuente).
  `verify_before_alert`: contrastar con GeckoTerminal antes de alertar;
  `max_price_deviation_pct` y `max_candle_drop_pct` son sus umbrales, y
  `max_rise_from_low_pct` y `max_drop_from_recent_high_pct` los de "llega
  tarde".
- `[radar]` — `alerts_enabled` (activa o desactiva las alertas completas),
  intervalo de polling, cooldown entre alertas repetidas del mismo token,
  umbral de score, cuántos candidatos evaluar por pasada y `alert_log_path`
  (la base SQLite de resultados).
- `[watch]` — vigilancia rápida y prealertas (`enabled` las activa o
  desactiva): intervalo, cuántos tokens
  vigilar, umbrales de arranque, `require_sustained_seconds` (cuánto tiene que
  sostenerse el arranque antes de avisar) y cooldown propio.
- `[wallets]` — seguimiento de wallets: `file` con las direcciones,
  intervalo, `min_sol` (gasto mínimo para contar como compra), cooldown por
  wallet y token, ventana de confluencia, `min_wallets` (cuántas wallets
  tienen que coincidir en un token para avisar), un aviso por token
  (`token_cooldown_minutes`, `realert_new_wallets`), tope de actividad
  (`max_tokens_per_hour`) y lista negra (`blacklist_file`, `rug_drop_pct`).
- `[scoring]` — `late_penalty_start_h1_pct` y `late_penalty_end_h1_pct`:
  entre esos dos cambios de 1h el score se reduce linealmente hasta 0.
- `[scoring_weights]` — pesos relativos (deben sumar ~1.0) de cada
  componente del score: volumen 1h, aceleración del volumen, presión
  compradora de 5 min, cambio de precio 1h/6h, liquidez y si el token tiene
  boost activo.
- `[telegram]` — `bot_token`, `chat_id` y opcionalmente `message_thread_id`
  si querés mandar las alertas a un tema (topic) concreto dentro de un
  grupo con "Temas" activados.

## Uso

```bash
python run.py --config config.toml
```

El proceso corre indefinidamente, escaneando cada `poll_interval_seconds`
hasta que se lo interrumpa (Ctrl+C).

## Tests

Los tests usan solo `unittest` de la stdlib (no hace falta instalar nada
más) y no tocan la red: la sesión HTTP y el bot de Telegram son dobles.

```bash
python -m unittest discover -s tests -t .
```

## Estructura del proyecto

```
run.py                    CLI: parsea argumentos y arranca el radar
sigpump/
  config.py                Carga de config.toml + lógica de scoring
  screener.py               Cliente HTTP async de DexScreener y GeckoTerminal
  radar.py                   Orquesta el ciclo descubrir -> puntuar -> alertar
  telegram.py                 Formateo y envío de alertas por Telegram
  wallets.py                  Seguimiento de las compras de wallets
  util.py                      Conversión defensiva de los campos de la API
tests/                    Tests (stdlib unittest, sin red)
```
