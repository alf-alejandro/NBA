# 🏀 NBA Edge Alpha Bot

Bot de predicción NBA para Polymarket usando la fórmula NEA (News-Aggressive).

## Arquitectura

```
bot.py          → Orquestador principal (morning / evening)
analyzer.py     → Gemini 2.5 Pro con Google Search (análisis + resolución)
nea_formula.py  → Fórmula NBA Edge Alpha ponderada
portfolio.py    → Gestión de capital, PnL, historial
polymarket.py   → Cliente Gamma API (modo simulación por defecto)
portfolio.json  → Estado persistente (auto-generado)
```

## Fórmula NEA (News-Aggressive)

```
NEA = P_poly - [(0.35 × P_vegas) + (0.50 × N) + (0.10 × V) + (0.05 × R)]
```

| Variable  | Peso | Descripción                            |
|-----------|------|----------------------------------------|
| P_vegas   | 35%  | Probabilidad implícita (moneyline)     |
| N (News)  | 50%  | Score de lesiones/noticias (-40 a +20) |
| V (Local) | 10%  | +5 local / -5 visitante                |
| R (Racha) | 5%   | % victorias en últimos 5 partidos      |

**Señales:**
- `NEA < -5` → 🚀 **COMPRA** (precio en Poly subvalorado)
- `NEA -5 a 5` → ⚖️ **NEUTRAL** (mercado eficiente)
- `NEA > 5` → ⛔ **EVITAR** (precio sobrevaluado)

## Reglas de Gestión de Capital

- ✅ Capital inicial: **$20 (simulación)**
- ✅ Máximo por apuesta: **15% del capital**
- ✅ Máximo exposición simultánea: **50% del capital**
- ✅ En simulación: nunca se ejecutan órdenes reales

## Setup en Railway

### 1. Variables de entorno (Railway → Variables)

```
GEMINI_API_KEY=tu_clave_aqui
GAMMA_API_KEY=opcional_para_simulacion
SIMULATE=true
```

### 2. Deploy — proceso permanente (no cron)

El bot corre como un proceso único que duerme y despierta solo.

```bash
# Railway CLI
railway up
```

O conecta el repo GitHub en Railway dashboard → New Project → Deploy from GitHub.

> ⚠️ **Importante:** En Railway agrega un **Volume** montado en `/app` para que
> `portfolio.json` y `.health_ok` persistan entre deploys.

### 3. Variables de override para debug

```
FORCE_MODE=morning      # fuerza sesión morning ahora mismo
FORCE_MODE=evening      # fuerza sesión evening ahora mismo
FORCE_MODE=healthcheck  # re-ejecuta el health check
```

## Ejecución local

```bash
pip install -r requirements.txt

# Test morning (análisis + apuestas)
RUN_MODE=morning GEMINI_API_KEY=xxx python bot.py

# Test evening (resolución + PnL)
RUN_MODE=evening GEMINI_API_KEY=xxx python bot.py
```

## Output esperado

```
2026-02-24 13:00:01 [INFO] ▶  Run mode: MORNING
2026-02-24 13:00:01 [INFO] === MORNING SESSION — 2026-02-24 ===
2026-02-24 13:00:05 [INFO] 🏀  8 games identified for today
2026-02-24 13:00:05 [INFO] --- Processing: Lakers vs Warriors ---
2026-02-24 13:00:05 [INFO]   NEA = -12.50 → BUY
2026-02-24 13:00:05 [INFO]   ✅  BET PLACED: $3.00 on Lakers @ 45¢ (NEA=-12.5)
2026-02-24 13:00:05 [INFO] =============================================
2026-02-24 13:00:05 [INFO]   📊  PORTFOLIO SUMMARY
2026-02-24 13:00:05 [INFO]   Current capital  : $17.00
2026-02-24 13:00:05 [INFO]   Total PnL        : +0.00$
2026-02-24 13:00:05 [INFO]   Exposure         : 15.0%
```

## Notas importantes

- **SIMULATE=true** (default): Todas las apuestas son simuladas. Cambia a `false` solo cuando quieras operar real con la Gamma API.
- `portfolio.json` persiste entre ejecuciones. En Railway, usa un **Volume** para que no se borre en cada deploy.
- El modelo usa `gemini-2.5-pro-preview-06-05`. Si falla, cambia a `gemini-2.0-flash` en `analyzer.py`.
