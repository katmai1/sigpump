# sigpump

Radar de solo-lectura para memecoins trending en [DexScreener](https://dexscreener.com/).
No ejecuta trades: descubre candidatos, los puntúa según métricas de mercado
y avisa por Telegram cuando cruzan un umbral configurable.

## Cómo funciona

DexScreener no expone públicamente el ranking exacto de su página
"trending" (ese cálculo es interno). `sigpump` arma su propia señal
combinando boosts pagos + perfiles de token recientes como candidatos, y
los puntúa con métricas de mercado reales (volumen, momentum de precio,
liquidez), usando la API pública de DexScreener (sin API key):

- `GET /token-boosts/latest/v1` — tokens con promoción paga reciente
- `GET /token-boosts/top/v1` — tokens con más boosts activos
- `GET /token-profiles/latest/v1` — perfiles de token nuevos/actualizados
- `GET /latest/dex/tokens/{addrs}` — datos de mercado (volumen, liquidez,
  cambios de precio) para hasta 30 direcciones por llamada

El bucle principal ([sigpump/radar.py](sigpump/radar.py)) repite cada
`poll_interval_seconds`:

1. Descubre candidatos (boosts + perfiles nuevos).
2. Trae sus datos de mercado y filtra por liquidez/volumen mínimos.
3. Calcula un score 0-100 ([sigpump/config.py](sigpump/config.py)).
4. Si supera `score_alert_threshold` y no está en cooldown, envía una
   alerta por Telegram ([sigpump/telegram.py](sigpump/telegram.py)).

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

- `[dexscreener]` — chain a monitorear y mínimos de liquidez/volumen para
  considerar un par.
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

## Estructura del proyecto

```
run.py                    CLI: parsea argumentos y arranca el radar
sigpump/
  config.py                Carga de config.toml + lógica de scoring
  screener.py               Cliente HTTP async de la API de DexScreener
  radar.py                   Orquesta el ciclo descubrir -> puntuar -> alertar
  telegram.py                 Formateo y envío de alertas por Telegram
```
