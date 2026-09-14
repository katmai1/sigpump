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
3. Calcula un score 0-100 ([sigpump/config.py](sigpump/config.py)).
4. Los que superan `score_alert_threshold` y no están en cooldown se
   verifican contra GeckoTerminal: el precio tiene que coincidir con el de
   DexScreener, la liquidez real superar el mínimo y las velas de 1 minuto
   de la última hora no pueden mostrar desplomes bruscos.
5. Los que pasan la verificación se alertan por Telegram
   ([sigpump/telegram.py](sigpump/telegram.py)).

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
  `max_price_deviation_pct` y `max_candle_drop_pct` son sus umbrales.
- `[radar]` — intervalo de polling, cooldown entre alertas repetidas del
  mismo token, umbral de score y cuántos candidatos evaluar por pasada.
- `[scoring_weights]` — pesos relativos (deben sumar ~1.0) de cada
  componente del score: volumen 1h, cambio de precio 1h/6h, liquidez y
  si el token tiene boost activo.
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
