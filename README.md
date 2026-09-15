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
2. Trae sus datos de mercado y aplica filtros duros: liquidez, volumen,
   market cap, edad, txns mínimas, ratio de ventas (honeypots), tamaño
   medio de trade (wash trading) y subida máxima en 1h.
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
5. Los que pasan la verificación se alertan por Telegram
   ([sigpump/telegram.py](sigpump/telegram.py)).
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
  cumpliéndose con los datos de ~30 y ~60 segundos después. Sirven para
  decidir con datos si conviene exigir que el arranque se sostenga.

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
  DexScreener: mínimos de liquidez/volumen/market cap/edad, `min_txns_h1`,
  `min_sell_ratio_h1` (honeypots), `max_avg_trade_usd` (wash trading) y
  `max_price_change_h1_pct` (subidas absurdas).
- `[geckoterminal]` — `trending_pages`: cuántas páginas del ranking de
  pools trending sumar como candidatos (0 desactiva la fuente).
  `verify_before_alert`: contrastar con GeckoTerminal antes de alertar;
  `max_price_deviation_pct` y `max_candle_drop_pct` son sus umbrales, y
  `max_rise_from_low_pct` y `max_drop_from_recent_high_pct` los de "llega
  tarde".
- `[radar]` — intervalo de polling, cooldown entre alertas repetidas del
  mismo token, umbral de score, cuántos candidatos evaluar por pasada y
  `alert_log_path` (la base SQLite de resultados).
- `[watch]` — vigilancia rápida y prealertas: intervalo, cuántos tokens
  vigilar, umbrales de arranque y cooldown propio.
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
  util.py                      Conversión defensiva de los campos de la API
tests/                    Tests (stdlib unittest, sin red)
```
