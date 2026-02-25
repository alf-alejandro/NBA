"""
╔══════════════════════════════════════════════════════════════╗
║          NBA EDGE ALPHA BOT  v3.0                           ║
║  Detecta oportunidades + gestiona posiciones simuladas       ║
║  Deploy: Railway  |  Dashboard: puerto 8080                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import logging
import threading
import requests
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

# ── Cargar .env ────────────────────────────────────────────────────────────────
def _cargar_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_cargar_env()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nba-bot")

# ── Configuración desde variables de entorno ───────────────────────────────────
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GAMMA_API         = os.environ.get("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API          = os.environ.get("CLOB_API", "https://clob.polymarket.com")
NBA_SERIES_ID     = int(os.environ.get("NBA_SERIES_ID", "10345"))
NEA_UMBRAL          = float(os.environ.get("NEA_UMBRAL", "10.0"))       # puntos mínimos NEA para señal COMPRAR
VALOR_REAL_MINIMO   = float(os.environ.get("VALOR_REAL_MINIMO", "0.40")) # filtro: valor real debe ser > 0.40
TAKE_PROFIT_PRECIO  = float(os.environ.get("TAKE_PROFIT_PRECIO", "0.42"))# precio fijo de salida TP (42¢)
# Stop Loss = valor real de la posición (calculado al abrir) — no hay delta fijo
MONITOR_INTERVAL    = int(os.environ.get("MONITOR_INTERVAL", "3600"))    # segundos entre actualizaciones (default 1h)
CAPITAL_TOTAL       = float(os.environ.get("CAPITAL_TOTAL", "100.0"))    # capital simulado en USD
RIESGO_POR_TRADE    = float(os.environ.get("RIESGO_POR_TRADE", "0.01"))  # 1% del capital por posición
GEMINI_MODEL        = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
DATA_DIR            = os.environ.get("DATA_DIR", "/data")
ET = ZoneInfo("America/New_York")

# ── Persistencia ───────────────────────────────────────────────────────────────
os.makedirs(DATA_DIR, exist_ok=True)
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
SCAN_LOG_FILE  = os.path.join(DATA_DIR, "scan_log.json")
STATE_FILE     = os.path.join(DATA_DIR, "state.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Lock para acceso concurrente a archivos ────────────────────────────────────
_file_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCIA
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    with _file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)


def load_positions() -> list[dict]:
    return load_json(POSITIONS_FILE, [])


def save_positions(positions: list[dict]):
    save_json(POSITIONS_FILE, positions)


def load_scan_log() -> list[dict]:
    return load_json(SCAN_LOG_FILE, [])


def append_scan_log(entry: dict):
    log_data = load_scan_log()
    log_data.append(entry)
    # Guardar solo los últimos 50 scans
    save_json(SCAN_LOG_FILE, log_data[-50:])


def load_state() -> dict:
    return load_json(STATE_FILE, {"last_scan": None, "manual_triggered": False})


def save_state(state: dict):
    save_json(STATE_FILE, state)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — POLYMARKET
# ══════════════════════════════════════════════════════════════════════════════

def obtener_partidos_hoy() -> list[dict]:
    hoy = date.today().strftime("%Y-%m-%d")
    resp = SESSION.get(
        f"{GAMMA_API}/events",
        params={
            "series_id": NBA_SERIES_ID, "tag_id": 100639,
            "active": "true", "closed": "false",
            "limit": 50, "order": "startTime", "ascending": "true",
        }, timeout=15
    )
    resp.raise_for_status()
    todos = resp.json()
    return [e for e in todos if e.get("eventDate") == hoy]


def clasificar_mercado(pregunta: str) -> str | None:
    p, pl = pregunta.strip(), pregunta.lower()
    excluir = [
        "points o/u", "rebounds o/u", "assists o/u", "steals o/u",
        "blocks o/u", "turnovers o/u", "3-pointer", "field goal", "free throw",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "first half", "second half", "halftime", "triple double", "double double",
        "will there be", "lead at any", "margin of victory", "largest lead",
    ]
    if any(ex in pl for ex in excluir): return None
    if p.startswith("Spread:"):         return "📐 Spread"
    if ": O/U" in p:                    return "🎯 Total O/U"
    if "vs." in pl and ":" not in p:    return "💰 Moneyline"
    return None


def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:   return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except: return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:   return json.loads(raw) if isinstance(raw, str) else raw
    except: return []


def precio_clob(token_id: str) -> tuple[str, float | None]:
    try:
        r = SESSION.get(f"{CLOB_API}/midpoint",
                        params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        mid = r.json().get("mid")
        return token_id, float(mid) if mid is not None else None
    except Exception:
        return token_id, None


def obtener_precios_paralelo(token_ids: list[str]) -> dict[str, float]:
    resultado = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        futuros = {pool.submit(precio_clob, tid): tid for tid in token_ids}
        for f in as_completed(futuros):
            tid, precio = f.result()
            if precio is not None:
                resultado[tid] = precio
    return resultado


def construir_estructura(partidos: list[dict]) -> list[dict]:
    estructura = []
    for evento in partidos:
        candidatos = []
        for m in evento.get("markets", []):
            tipo = clasificar_mercado(m.get("question", ""))
            if not tipo: continue
            token_ids = extraer_token_ids(m)
            if not token_ids: continue
            candidatos.append({
                "tipo":      tipo,
                "pregunta":  m.get("question", ""),
                "volumen":   float(m.get("volume", 0) or 0),
                "token_ids": token_ids,
                "outcomes":  extraer_outcomes(m),
            })
        seleccionados = {}
        for c in sorted(candidatos, key=lambda x: x["volumen"], reverse=True):
            if c["tipo"] not in seleccionados:
                seleccionados[c["tipo"]] = c
            if len(seleccionados) == 3: break
        estructura.append({"evento": evento, "mercados": seleccionados})
    return estructura


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — GEMINI
# ══════════════════════════════════════════════════════════════════════════════

def analizar_partido_con_gemini(equipo_local: str, equipo_visitante: str,
                                 linea_ml_local: float) -> dict:
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY no configurada, usando valores por defecto")
        return _valores_defecto(linea_ml_local)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""Eres un analista experto de apuestas deportivas NBA.
Necesito que analices el partido de HOY: {equipo_visitante} (visitante) @ {equipo_local} (local).

Usando búsqueda web, responde EXACTAMENTE en este formato JSON (sin markdown):

{{
  "p_vegas": <número 0-100, probabilidad implícita del equipo LOCAL según casas de apuestas hoy>,
  "n_local": <número -100 a 100, factor noticias equipo local>,
  "n_visitante": <número -100 a 100, factor noticias equipo visitante>,
  "r_local": <número 0-100, racha equipo local últimos 5 partidos>,
  "r_visitante": <número 0-100, racha equipo visitante últimos 5 partidos>,
  "resumen": "<2 oraciones: estado actual, lesiones importantes y contexto>"
}}

Busca: odds actuales DraftKings/FanDuel, lesiones confirmadas, últimos 5 resultados de cada equipo.
Responde SOLO el JSON."""

    try:
        respuesta_texto = ""
        for chunk in client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                tools=[types.Tool(googleSearch=types.GoogleSearch())],
            ),
        ):
            if chunk.text:
                respuesta_texto += chunk.text

        respuesta_texto = re.sub(r"```json|```", "", respuesta_texto).strip()
        match = re.search(r"\{.*\}", respuesta_texto, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "p_vegas":     float(data.get("p_vegas", 50)),
                "n_local":     float(data.get("n_local", 0)),
                "n_visitante": float(data.get("n_visitante", 0)),
                "r_local":     float(data.get("r_local", 50)),
                "r_visitante": float(data.get("r_visitante", 50)),
                "resumen":     data.get("resumen", "Sin información disponible."),
            }
    except Exception as e:
        log.error(f"Error Gemini: {e}")

    return _valores_defecto(linea_ml_local)


def _valores_defecto(linea_ml_local: float) -> dict:
    return {
        "p_vegas":     linea_ml_local * 100,
        "n_local":     0.0,
        "n_visitante": 0.0,
        "r_local":     50.0,
        "r_visitante": 50.0,
        "resumen":     "Análisis no disponible (sin API key de Gemini).",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — FÓRMULA NEA
# ══════════════════════════════════════════════════════════════════════════════

def calcular_nea(p_poly: float, p_vegas: float, n: float,
                 v: float, r: float) -> float:
    valor_real = 0.45 * p_vegas + 0.40 * n + 0.10 * v + 0.05 * r
    return p_poly - valor_real


def extraer_equipos(titulo: str) -> tuple[str, str]:
    partes = titulo.split(" vs. ")
    if len(partes) == 2:
        return partes[0].strip(), partes[1].strip()
    return titulo, titulo


def hora_et(st: str) -> str:
    try:
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        return (dt - timedelta(hours=5)).strftime("%I:%M %p ET")
    except: return st


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 4 — SCAN Y OPORTUNIDADES
# ══════════════════════════════════════════════════════════════════════════════

def ejecutar_scan() -> list[dict]:
    """Corre el scan completo y retorna lista de oportunidades."""
    log.info("🔍 Iniciando scan NBA Edge Alpha...")
    todas_oportunidades = []

    try:
        partidos = obtener_partidos_hoy()
    except Exception as e:
        log.error(f"Error obteniendo partidos: {e}")
        return []

    if not partidos:
        log.info("Sin partidos para hoy.")
        return []

    log.info(f"✅ {len(partidos)} partido(s) encontrado(s)")
    estructura = construir_estructura(partidos)

    all_tokens = list({
        tid
        for item in estructura
        for m in item["mercados"].values()
        for tid in m["token_ids"]
    })
    precios = obtener_precios_paralelo(all_tokens)
    log.info(f"💹 {len(precios)}/{len(all_tokens)} precios obtenidos")

    for item in estructura:
        titulo = item["evento"].get("title", "?")
        equipo_visit, equipo_local = extraer_equipos(titulo)
        hora = hora_et(item["evento"].get("startTime", ""))

        ml = item["mercados"].get("💰 Moneyline")
        p_local_clob = 0.5
        if ml:
            for outcome, tid in zip(ml["outcomes"], ml["token_ids"]):
                if outcome == equipo_local and tid in precios:
                    p_local_clob = precios[tid]
                    break

        log.info(f"🤖 Analizando: {titulo}...")
        analisis = analizar_partido_con_gemini(equipo_local, equipo_visit, p_local_clob)

        if not ml:
            continue

        for outcome, token_id in zip(ml["outcomes"], ml["token_ids"]):
            precio_poly = precios.get(token_id)
            if precio_poly is None:
                continue

            p_poly_pct = precio_poly * 100
            es_local = (outcome == equipo_local)
            v_factor = 5.0 if es_local else -5.0

            if es_local:
                p_vegas = analisis["p_vegas"]
                n       = analisis["n_local"]
                r       = analisis["r_local"]
            else:
                p_vegas = 100 - analisis["p_vegas"]
                n       = analisis["n_visitante"]
                r       = analisis["r_visitante"]

            n_norm = (n + 100) / 2
            nea    = calcular_nea(p_poly_pct, p_vegas, n_norm, v_factor, r)
            valor_real = 0.45 * p_vegas + 0.40 * n_norm + 0.10 * v_factor + 0.05 * r

            if abs(nea) >= NEA_UMBRAL:
                accion = "COMPRAR" if nea <= -NEA_UMBRAL else "EVITAR"
                op = {
                    "partido":     titulo,
                    "equipo":      outcome,
                    "es_local":    es_local,
                    "p_poly":      round(p_poly_pct, 2),
                    "valor_real":  round(valor_real, 2),
                    "nea":         round(nea, 2),
                    "accion":      accion,
                    "hora":        hora,
                    "token_id":    token_id,
                    "resumen":     analisis["resumen"],
                    "scanned_at":  datetime.now(ET).isoformat(),
                }
                todas_oportunidades.append(op)

    todas_oportunidades.sort(key=lambda x: abs(x["nea"]), reverse=True)
    log.info(f"🎯 Scan completado: {len(todas_oportunidades)} oportunidades encontradas")

    # Log del scan
    append_scan_log({
        "ts":            datetime.now(ET).isoformat(),
        "partidos":      len(partidos),
        "oportunidades": len(todas_oportunidades),
        "resultados":    todas_oportunidades,
    })

    return todas_oportunidades


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 5 — GESTIÓN DE POSICIONES
# ══════════════════════════════════════════════════════════════════════════════

def abrir_posicion(oportunidad: dict) -> dict | None:
    """
    Simula apertura de posición con las siguientes reglas:
      - Solo señales COMPRAR
      - Valor real debe ser > VALOR_REAL_MINIMO (0.40 por defecto)
      - Tamaño: 1% del capital total (CAPITAL_TOTAL * RIESGO_POR_TRADE)
      - Take Profit: precio fijo TAKE_PROFIT_PRECIO (0.42 por defecto)
      - Stop Loss: precio sube hasta valor_real (el mercado te da la razón → salís)
    """
    if oportunidad["accion"] != "COMPRAR":
        return None

    # Filtro: valor real mínimo 0.40
    valor_real_decimal = oportunidad["valor_real"] / 100
    if valor_real_decimal <= VALOR_REAL_MINIMO:
        log.info(f"⏭ SKIP {oportunidad['equipo']}: valor real {valor_real_decimal:.2f} ≤ {VALOR_REAL_MINIMO} (muy bajo)")
        return None

    positions = load_positions()

    # Evitar duplicados
    for p in positions:
        if p["token_id"] == oportunidad["token_id"] and p["status"] == "OPEN":
            log.info(f"Posición ya abierta para {oportunidad['equipo']}")
            return p

    precio_entrada = oportunidad["p_poly"] / 100
    monto_usd      = round(CAPITAL_TOTAL * RIESGO_POR_TRADE, 2)  # $1.00

    position = {
        "id":             f"pos_{datetime.now(ET).strftime('%Y%m%d%H%M%S')}_{oportunidad['token_id'][:8]}",
        "partido":        oportunidad["partido"],
        "equipo":         oportunidad["equipo"],
        "token_id":       oportunidad["token_id"],
        "precio_entrada": round(precio_entrada, 4),
        "precio_actual":  round(precio_entrada, 4),
        "valor_real":     round(valor_real_decimal, 4),
        "nea_entrada":    oportunidad["nea"],
        "take_profit":    round(TAKE_PROFIT_PRECIO, 4),         # TP fijo: 0.42
        "stop_loss":      round(precio_entrada * 0.50, 4),    # SL = 50% del precio de entrada
        "monto_usd":      monto_usd,                          # $1.00 (1% de $100)
        "hora_partido":   oportunidad["hora"],
        "status":         "OPEN",
        "opened_at":      datetime.now(ET).isoformat(),
        "closed_at":      None,
        "close_reason":   None,
        "pnl_usd":        0.0,
        "pnl_pct":        0.0,
        "price_history":  [
            {"ts": datetime.now(ET).isoformat(), "price": precio_entrada}
        ],
    }

    positions.append(position)
    save_positions(positions)
    log.info(
        f"✅ POSICIÓN ABIERTA: {position['equipo']} | "
        f"Entrada: {precio_entrada:.2%} | "
        f"TP: {TAKE_PROFIT_PRECIO:.2%} | "
        f"SL (−50%): {precio_entrada * 0.50:.2%} | "
        f"Monto: ${monto_usd}"
    )
    return position


def actualizar_posiciones():
    """
    Actualiza precios de posiciones abiertas y ejecuta:
      - Take Profit: precio_actual >= 0.42 (TAKE_PROFIT_PRECIO)
      - Stop Loss:   precio_actual >= valor_real (el mercado reconoció el valor → salís)
    """
    positions = load_positions()
    abiertas = [p for p in positions if p["status"] == "OPEN"]

    if not abiertas:
        log.info("Sin posiciones abiertas para monitorear.")
        return

    token_ids = [p["token_id"] for p in abiertas]
    precios = obtener_precios_paralelo(token_ids)

    modificado = False
    for pos in positions:
        if pos["status"] != "OPEN":
            continue

        precio_actual = precios.get(pos["token_id"])
        if precio_actual is None:
            log.warning(f"Sin precio para token {pos['token_id']}")
            continue

        pos["precio_actual"] = round(precio_actual, 4)

        # PnL en % y en USD
        pnl_pct = (precio_actual - pos["precio_entrada"]) / pos["precio_entrada"] * 100
        pnl_usd = pos.get("monto_usd", 1.0) * (precio_actual - pos["precio_entrada"]) / pos["precio_entrada"]
        pos["pnl_pct"] = round(pnl_pct, 2)
        pos["pnl_usd"] = round(pnl_usd, 4)

        # Historial de precios (máx 48 puntos)
        pos.setdefault("price_history", []).append({
            "ts":    datetime.now(ET).isoformat(),
            "price": precio_actual,
        })
        pos["price_history"] = pos["price_history"][-48:]

        modificado = True

        # ── Take Profit: precio sube hasta 0.42 ──────────────────────────────
        if precio_actual >= pos["take_profit"]:
            pos["status"]       = "CLOSED"
            pos["closed_at"]    = datetime.now(ET).isoformat()
            pos["close_reason"] = "TAKE_PROFIT"
            log.info(
                f"🎯 TAKE PROFIT: {pos['equipo']} | "
                f"{pos['precio_entrada']:.2%} → {precio_actual:.2%} | "
                f"PnL: {pos['pnl_pct']:+.2f}% (${pos['pnl_usd']:+.4f})"
            )

        # ── Stop Loss: precio cae al 50% del precio de entrada ──────────────
        elif precio_actual <= pos["stop_loss"]:
            pos["status"]       = "CLOSED"
            pos["closed_at"]    = datetime.now(ET).isoformat()
            pos["close_reason"] = "STOP_LOSS"
            log.info(
                f"🛑 STOP LOSS (−50% entrada): {pos['equipo']} | "
                f"{pos['precio_entrada']:.2%} → {precio_actual:.2%} | "
                f"PnL: {pos['pnl_pct']:+.2f}% (${pos['pnl_usd']:+.4f})"
            )

    if modificado:
        save_positions(positions)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 6 — SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def ciclo_scan_y_posiciones():
    """Ejecuta scan + abre posiciones para oportunidades COMPRAR."""
    oportunidades = ejecutar_scan()

    for op in oportunidades:
        if op["accion"] == "COMPRAR":
            abrir_posicion(op)

    state = load_state()
    state["last_scan"]        = datetime.now(ET).isoformat()
    state["manual_triggered"] = False
    save_state(state)


def ciclo_monitoreo():
    """Actualiza precios de posiciones abiertas."""
    log.info("📡 Actualizando precios de posiciones abiertas...")
    actualizar_posiciones()


def _thread_scheduler():
    """Hilo scheduler: scan a las 9AM ET, monitoreo cada MONITOR_INTERVAL segundos."""
    import time

    last_scan_date = None

    while True:
        now_et = datetime.now(ET)
        state  = load_state()

        # Verificar si se solicitó scan manual
        if state.get("manual_triggered"):
            log.info("🖐 Scan manual solicitado")
            ciclo_scan_y_posiciones()
            last_scan_date = now_et.date()

        # Scan automático a las 9:00 AM ET
        elif now_et.hour == 9 and now_et.minute < 2:
            if last_scan_date != now_et.date():
                log.info("⏰ Scan automático 9AM ET")
                ciclo_scan_y_posiciones()
                last_scan_date = now_et.date()

        # Monitoreo de posiciones cada MONITOR_INTERVAL
        ciclo_monitoreo()

        time.sleep(MONITOR_INTERVAL)


def iniciar_scheduler():
    t = threading.Thread(target=_thread_scheduler, daemon=True)
    t.start()
    log.info(f"🚀 Scheduler iniciado | Scan: 9AM ET | Monitoreo: cada {MONITOR_INTERVAL}s")


# ══════════════════════════════════════════════════════════════════════════════
# API para el dashboard
# ══════════════════════════════════════════════════════════════════════════════

def trigger_manual_scan():
    """Llama desde el dashboard para forzar un scan."""
    state = load_state()
    state["manual_triggered"] = True
    save_state(state)
    log.info("🖐 Scan manual marcado en estado")


def get_dashboard_data() -> dict:
    positions = load_positions()
    scan_log  = load_scan_log()
    state     = load_state()

    abiertas = [p for p in positions if p["status"] == "OPEN"]
    cerradas  = [p for p in positions if p["status"] == "CLOSED"]

    tp_count  = sum(1 for p in cerradas if p.get("close_reason") == "TAKE_PROFIT")
    sl_count  = sum(1 for p in cerradas if p.get("close_reason") == "STOP_LOSS")

    # PnL total en USD
    pnl_total_usd = sum(p.get("pnl_usd", 0) for p in cerradas)
    capital_actual = round(CAPITAL_TOTAL + pnl_total_usd, 4)

    last_scan_ops = []
    if scan_log:
        last_scan_ops = scan_log[-1].get("resultados", [])

    return {
        "ts":               datetime.now(ET).isoformat(),
        "last_scan":        state.get("last_scan"),
        "positions_open":   abiertas,
        "positions_closed": cerradas[-20:],
        "stats": {
            "total_open":    len(abiertas),
            "total_closed":  len(cerradas),
            "take_profits":  tp_count,
            "stop_losses":   sl_count,
            "win_rate":      round(tp_count / max(len(cerradas), 1) * 100, 1),
            "pnl_total_usd": round(pnl_total_usd, 4),
            "capital_actual": capital_actual,
        },
        "last_scan_ops": last_scan_ops,
        "config": {
            "nea_umbral":          NEA_UMBRAL,
            "valor_real_minimo":   VALOR_REAL_MINIMO,
            "take_profit_precio":  TAKE_PROFIT_PRECIO,
            "monitor_interval":    MONITOR_INTERVAL,
            "capital_total":       CAPITAL_TOTAL,
            "riesgo_por_trade":    RIESGO_POR_TRADE,
            "monto_por_trade_usd": round(CAPITAL_TOTAL * RIESGO_POR_TRADE, 2),
        },
    }


if __name__ == "__main__":
    # Modo standalone (sin dashboard)
    ciclo_scan_y_posiciones()
