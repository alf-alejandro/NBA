"""
NBA Edge Alpha Bot
==================
Flujo completo:

  ARRANQUE → Health Check
           → Si está FUERA de horario en PRIMER arranque → apuesta de prueba única
           → Scheduler loop: duerme y despierta solo

  MORNING  (9–11am ET): analiza juegos + NEA + apuestas
  EVENING  (9–11pm ET): resuelve apuestas con retry horario hasta tener resultados

Env vars:
  GEMINI_API_KEY    → requerida
  GAMMA_API_KEY     → opcional (simulación por defecto)
  SIMULATE          → "true" (default) | "false"
  DATA_DIR          → directorio persistente, ej. /data  (default: directorio actual)
  DASHBOARD_PORT    → puerto del dashboard (default: 8080)
  FORCE_MODE        → "morning" | "evening" | "healthcheck"  (debug override)
"""

import os
import json
import logging
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from analyzer import GeminiAnalyzer
from portfolio import Portfolio
from polymarket import PolymarketClient
from nea_formula import compute_nea, interpret_nea
from healthcheck import run_health_check
from dashboard_server import start_dashboard

# ── Data directory (persistente en Railway via Volume montado en /data) ───────
DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORTFOLIO_FILE  = str(DATA_DIR / "portfolio.json")
HEALTH_FLAG     = str(DATA_DIR / ".health_ok")
FIRST_RUN_FLAG  = str(DATA_DIR / ".first_run_done")
LOG_FILE        = str(DATA_DIR / "bot.log")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("nba-bot")

# ── Config ────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL   = 20.00
MAX_BET_PCT       = 0.33
MAX_TOTAL_EXPOSED = 0.33
ET                = ZoneInfo("America/New_York")

MORNING_HOUR_START = 9
MORNING_HOUR_END   = 11
EVENING_HOUR_START = 21
EVENING_HOUR_END   = 23

# Max reintentos nocturnos esperando resultados (cada 1h)
MAX_EVENING_RETRIES = 6


# ── Helpers de tiempo ─────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(tz=ET)

def seconds_until(target_hour: int, target_minute: int = 0) -> float:
    now = now_et()
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"

def is_in_window(start_h: int, end_h: int) -> bool:
    h = now_et().hour
    return start_h <= h < end_h

def determine_current_window() -> str:
    force = os.environ.get("FORCE_MODE", "").lower()
    if force in ("morning", "evening", "healthcheck"):
        return force
    hour = now_et().hour
    if MORNING_HOUR_START <= hour < MORNING_HOUR_END:
        return "morning"
    elif EVENING_HOUR_START <= hour < EVENING_HOUR_END:
        return "evening"
    elif hour < MORNING_HOUR_START:
        return "sleep_until_morning"
    elif MORNING_HOUR_END <= hour < EVENING_HOUR_START:
        return "sleep_until_evening"
    else:
        return "sleep_until_morning"

def sleep_with_countdown(seconds: float, label: str):
    log.info("💤  Sleeping %s until %s...", format_duration(seconds), label)
    interval = 1800
    elapsed  = 0.0
    while elapsed < seconds:
        chunk    = min(interval, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        remaining = seconds - elapsed
        if remaining > 60:
            log.info("⏳  %s remaining until %s", format_duration(remaining), label)
    log.info("⏰  Waking up for %s", label)


# ── Morning session ───────────────────────────────────────────────────────────

def run_morning(portfolio: Portfolio, analyzer: GeminiAnalyzer, poly: PolymarketClient,
                label: str = "MORNING SESSION"):
    log.info("=" * 60)
    log.info("  🌅  %s — %s  %s ET", label, date.today(), now_et().strftime("%H:%M"))
    log.info("=" * 60)

    if portfolio.exposure_ratio() >= MAX_TOTAL_EXPOSED:
        log.warning("⛔  Exposure %.0f%% ≥ 33%%. No new bets — 33% capital limit reached.", portfolio.exposure_ratio() * 100)
        return 0

    log.info("🔍  Fetching NBA games, injuries and odds via Gemini...")
    raw_analysis = analyzer.morning_analysis()
    log.info("📄  Analysis received (%d chars)", len(raw_analysis))

    games = analyzer.parse_games(raw_analysis)
    log.info("🏀  %d games identified", len(games))

    if not games:
        log.warning("No games parsed. Gemini may not have found today's schedule.")
        return 0

    bets_placed = 0
    for game in games:
        home = game.get("home", "?")
        away = game.get("away", "?")
        log.info("--- %s vs %s ---", home, away)

        nea_score = compute_nea(
            p_poly  = game.get("poly_price",       50),
            p_vegas = game.get("vegas_prob",        50),
            n       = game.get("news_score",         0),
            v       = game.get("home_away_factor",   0),
            r       = game.get("streak_pct",        50),
        )
        signal = interpret_nea(nea_score)
        log.info("  NEA = %+.2f → %s [%s]", nea_score, signal["action"], signal["confidence"])
        log.info("  📰  %s", game.get("news_summary", "—"))
        log.info("  💡  %s", game.get("rationale",    "—"))

        if signal["action"] != "BUY":
            log.info("  ⏭   No edge — skipping")
            continue

        available  = portfolio.available_capital(MAX_TOTAL_EXPOSED)
        bet_amount = round(min(portfolio.capital * MAX_BET_PCT, available), 2)

        if bet_amount < 0.10:
            log.warning("  ⚠   Bet size too small ($%.2f). Skipping.", bet_amount)
            continue

        bet = {
            "date"       : str(date.today()),
            "market_id"  : game.get("market_id", "SIMULATED"),
            "home"       : home,
            "away"       : away,
            "bet_on"     : game.get("bet_on", home),
            "poly_price" : game.get("poly_price", 50),
            "nea_score"  : round(nea_score, 2),
            "amount_usd" : bet_amount,
            "status"     : "OPEN",
            "result"     : None,
            "pnl"        : None,
            "news_summary": game.get("news_summary", ""),
            "rationale"  : game.get("rationale", ""),
        }
        portfolio.place_bet(bet)
        poly.place_order(
            market_id  = bet["market_id"],
            side       = "buy",
            amount_usd = bet_amount,
            price      = bet["poly_price"] / 100.0,
        )
        bets_placed += 1
        log.info("  ✅  BET: $%.2f on %s @ %d¢  (NEA=%+.1f, %s)",
                 bet_amount, bet["bet_on"], bet["poly_price"], nea_score, signal["confidence"])

    log.info("Session done. %d bets placed.", bets_placed)
    portfolio.save()
    portfolio.print_summary()
    return bets_placed


# ── Evening session con retry horario ─────────────────────────────────────────

def run_evening(portfolio: Portfolio, analyzer: GeminiAnalyzer, poly: PolymarketClient):
    log.info("=" * 60)
    log.info("  🌙  EVENING SESSION — %s  %s ET", date.today(), now_et().strftime("%H:%M"))
    log.info("=" * 60)

    open_bets = portfolio.open_bets_today()
    if not open_bets:
        log.info("No open bets to resolve tonight.")
        portfolio.print_summary()
        return

    for attempt in range(1, MAX_EVENING_RETRIES + 1):
        log.info("🔎  Resolution attempt %d/%d — %d bet(s) pending...",
                 attempt, MAX_EVENING_RETRIES, len(open_bets))

        raw_results  = analyzer.evening_resolution(open_bets)
        resolved_map = analyzer.parse_results(raw_results)

        newly_resolved = 0
        still_pending  = []

        for bet in open_bets:
            key     = f"{bet['home']}|{bet['away']}"
            outcome = resolved_map.get(key)

            if outcome and outcome.get("status") == "FINAL":
                portfolio.resolve_bet(key, outcome["winner"], outcome.get("final_score", ""))
                newly_resolved += 1
            else:
                status = outcome.get("status", "NOT_FOUND") if outcome else "NOT_FOUND"
                log.info("  ⏳  %s — status: %s", key, status)
                still_pending.append(bet)

        portfolio.save()

        if newly_resolved > 0:
            log.info("✅  Resolved %d bet(s) this attempt.", newly_resolved)

        # Actualizar lista de pendientes
        open_bets = still_pending

        if not open_bets:
            log.info("🎉  All bets resolved!")
            break

        if attempt < MAX_EVENING_RETRIES:
            log.info("⏳  %d bet(s) still pending. Retrying in 1 hour...", len(open_bets))
            time.sleep(3600)   # esperar 1 hora
        else:
            log.warning("⚠️   %d bet(s) unresolved after %d attempts. Will retry tomorrow evening.",
                        len(open_bets), MAX_EVENING_RETRIES)

    portfolio.print_summary()
    log.info("Evening done.")


# ── Apuesta de primer arranque (fuera de horario) ─────────────────────────────

def run_first_boot_bet(portfolio: Portfolio, analyzer: GeminiAnalyzer,
                       poly: PolymarketClient, first_run_flag: str):
    """
    Si es el primer arranque y estamos fuera de horario normal,
    hace UNA sesión de análisis y apuesta inmediatamente como prueba real del sistema.
    Solo se ejecuta una vez (controlado por .first_run_done flag).
    """
    flag = Path(first_run_flag)
    if flag.exists():
        log.info("🚀  First-boot bet already executed — skipping.")
        return

    log.info("=" * 60)
    log.info("  🆕  FIRST BOOT — Executing initial bet outside normal hours")
    log.info("=" * 60)
    log.info("  This is a ONE-TIME action to verify the full pipeline.")
    log.info("  After this, the bot will follow normal 9am/9pm ET schedule.")

    bets = run_morning(portfolio, analyzer, poly, label="FIRST BOOT BET")

    if bets > 0:
        log.info("✅  First boot bet(s) placed successfully. Pipeline verified.")
    else:
        log.info("ℹ️   No BUY signals found in first boot analysis. Pipeline ran OK.")

    # Marcar como hecho independientemente del resultado
    flag.write_text(str(datetime.now()))
    log.info("🏁  First-boot flag written to %s", first_run_flag)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    gamma_key  = os.environ.get("GAMMA_API_KEY", "")

    if not gemini_key:
        raise EnvironmentError("❌  GEMINI_API_KEY not set.")

    log.info("📁  Data directory: %s", DATA_DIR)

    # Inyectar DATA_DIR al dashboard server para que lea los archivos correctos
    os.environ["PORTFOLIO_FILE"] = PORTFOLIO_FILE
    os.environ["LOG_FILE"]       = LOG_FILE

    portfolio = Portfolio(PORTFOLIO_FILE, INITIAL_CAPITAL)
    start_dashboard()
    log.info("📊  Dashboard running on port %s", os.environ.get("DASHBOARD_PORT", "8080"))

    analyzer = GeminiAnalyzer(gemini_key)
    poly     = PolymarketClient(gamma_key)

    # ── Health check ──────────────────────────────────────────────────────
    force_mode  = os.environ.get("FORCE_MODE", "").lower()
    health_flag = Path(HEALTH_FLAG)

    if not health_flag.exists() or force_mode == "healthcheck":
        log.info("🔬  Running startup health check...")
        healthy = run_health_check(analyzer, portfolio)
        if healthy:
            health_flag.write_text(str(datetime.now()))
            log.info("✅  Health check passed.")
        else:
            log.error("❌  Health check FAILED — bot continues but check your config.")
    else:
        log.info("✅  Health flag found — skipping check.")

    # ── First boot bet (fuera de horario, solo primera vez) ───────────────
    window = determine_current_window()
    is_off_hours = window in ("sleep_until_morning", "sleep_until_evening")

    if is_off_hours and force_mode not in ("morning", "evening"):
        run_first_boot_bet(portfolio, analyzer, poly, FIRST_RUN_FLAG)

    # ── Scheduler loop ────────────────────────────────────────────────────
    log.info("🤖  Entering scheduler loop. (Ctrl+C to stop)")

    while True:
        window = determine_current_window()
        log.info("📍  Window: %s  (%s ET)", window, now_et().strftime("%H:%M:%S"))

        if window == "morning":
            run_morning(portfolio, analyzer, poly)
            sleep_with_countdown(seconds_until(EVENING_HOUR_START), "evening session")

        elif window == "evening":
            run_evening(portfolio, analyzer, poly)
            sleep_with_countdown(seconds_until(MORNING_HOUR_START), "morning session")

        elif window == "sleep_until_morning":
            sleep_with_countdown(seconds_until(MORNING_HOUR_START), "morning session")

        elif window == "sleep_until_evening":
            sleep_with_countdown(seconds_until(EVENING_HOUR_START), "evening session")

        else:
            log.warning("Unknown window: %s. Sleeping 60s.", window)
            time.sleep(60)


if __name__ == "__main__":
    main()
