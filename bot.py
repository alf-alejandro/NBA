"""
NBA Edge Alpha Bot
==================
Flujo completo:

  ARRANQUE → Health Check (busca internet, hace matemática, simula flujo)
           → Se duerme hasta la próxima ventana de trabajo
           → MORNING: analiza juegos, computa NEA, coloca apuestas simuladas
           → Se duerme hasta la noche
           → EVENING: resuelve apuestas, actualiza PnL
           → Se duerme hasta el día siguiente

Env vars:
  GEMINI_API_KEY   → requerida
  GAMMA_API_KEY    → opcional (simulación por defecto)
  SIMULATE         → "true" (default) | "false"
  FORCE_MODE       → "healthcheck" | "morning" | "evening"  (override para debug)
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("nba-bot")

# ── Config ────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL    = 20.00
MAX_BET_PCT        = 0.15
MAX_TOTAL_EXPOSED  = 0.50
DATA_FILE          = "portfolio.json"
HEALTH_FLAG_FILE   = ".health_ok"
ET                 = ZoneInfo("America/New_York")

# Ventanas horarias ET
MORNING_HOUR_START = 9
MORNING_HOUR_END   = 11
EVENING_HOUR_START = 21
EVENING_HOUR_END   = 23


# ── Time helpers ──────────────────────────────────────────────────────────────

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
    interval = 1800  # log every 30 min
    elapsed  = 0.0
    while elapsed < seconds:
        chunk = min(interval, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        remaining = seconds - elapsed
        if remaining > 60:
            log.info("⏳  %s remaining until %s", format_duration(remaining), label)
    log.info("⏰  Waking up for %s", label)


# ── Morning session ───────────────────────────────────────────────────────────

def run_morning(portfolio: Portfolio, analyzer: GeminiAnalyzer, poly: PolymarketClient):
    log.info("=" * 58)
    log.info("  🌅  MORNING SESSION — %s  %s ET", date.today(), now_et().strftime("%H:%M"))
    log.info("=" * 58)

    if portfolio.exposure_ratio() >= MAX_TOTAL_EXPOSED:
        log.warning("⛔  Exposure %.0f%% ≥ 50%%. No new bets today.", portfolio.exposure_ratio() * 100)
        return

    log.info("🔍  Fetching today's NBA games, injuries and odds via Gemini...")
    raw_analysis = analyzer.morning_analysis()
    log.info("📄  Raw analysis received (%d chars)", len(raw_analysis))

    games = analyzer.parse_games(raw_analysis)
    log.info("🏀  %d games identified", len(games))

    if not games:
        log.warning("No games parsed. Check Gemini output in bot.log.")
        return

    bets_placed = 0
    for game in games:
        home = game.get("home", "?")
        away = game.get("away", "?")
        log.info("--- %s vs %s ---", home, away)

        nea_score = compute_nea(
            p_poly  = game.get("poly_price", 50),
            p_vegas = game.get("vegas_prob",  50),
            n       = game.get("news_score",   0),
            v       = game.get("home_away_factor", 0),
            r       = game.get("streak_pct",  50),
        )
        signal = interpret_nea(nea_score)
        log.info("  NEA = %+.2f → %s [%s]", nea_score, signal["action"], signal["confidence"])
        log.info("  📰  %s", game.get("news_summary", "No news summary"))
        log.info("  💡  %s", game.get("rationale", "No rationale"))

        if signal["action"] != "BUY":
            log.info("  ⏭   No edge — skipping")
            continue

        available  = portfolio.available_capital(MAX_TOTAL_EXPOSED)
        bet_amount = min(portfolio.capital * MAX_BET_PCT, available)
        bet_amount = round(bet_amount, 2)

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
        }
        portfolio.place_bet(bet)
        poly.place_order(
            market_id  = bet["market_id"],
            side       = "buy",
            amount_usd = bet_amount,
            price      = bet["poly_price"] / 100.0,
        )
        bets_placed += 1
        log.info("  ✅  BET: $%.2f on %s @ %d¢  (NEA=%+.1f, confidence=%s)",
                 bet_amount, bet["bet_on"], bet["poly_price"], nea_score, signal["confidence"])

    log.info("Morning done. %d bets placed.", bets_placed)
    portfolio.save()
    portfolio.print_summary()


# ── Evening session ───────────────────────────────────────────────────────────

def run_evening(portfolio: Portfolio, analyzer: GeminiAnalyzer, poly: PolymarketClient):
    log.info("=" * 58)
    log.info("  🌙  EVENING SESSION — %s  %s ET", date.today(), now_et().strftime("%H:%M"))
    log.info("=" * 58)

    open_bets = portfolio.open_bets_today()
    if not open_bets:
        log.info("No open bets to resolve tonight.")
        portfolio.print_summary()
        return

    log.info("🔎  Resolving %d open bet(s)...", len(open_bets))
    raw_results  = analyzer.evening_resolution(open_bets)
    resolved_map = analyzer.parse_results(raw_results)

    for key, outcome in resolved_map.items():
        if outcome.get("status") in ("FINAL",):
            portfolio.resolve_bet(key, outcome["winner"], outcome.get("final_score", ""))
        else:
            log.warning("Game %s status: %s — not resolved yet", key, outcome.get("status"))

    portfolio.save()
    portfolio.print_summary()
    log.info("Evening done.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    gamma_key  = os.environ.get("GAMMA_API_KEY", "")

    if not gemini_key:
        raise EnvironmentError("❌  GEMINI_API_KEY not set.")

    portfolio = Portfolio(DATA_FILE, INITIAL_CAPITAL)
    start_dashboard()
    log.info("📊  Dashboard running on port %s", os.environ.get("DASHBOARD_PORT","8080"))
    analyzer  = GeminiAnalyzer(gemini_key)
    poly      = PolymarketClient(gamma_key)

    # ── HEALTH CHECK (primera vez o FORCE_MODE=healthcheck) ──────────────
    health_flag = Path(HEALTH_FLAG_FILE)
    force_mode  = os.environ.get("FORCE_MODE", "").lower()

    if not health_flag.exists() or force_mode == "healthcheck":
        log.info("🔬  First run detected — running startup health check...")
        healthy = run_health_check(analyzer, portfolio)
        if healthy:
            health_flag.write_text(str(datetime.now()))
            log.info("✅  Health check passed. Flag written.")
        else:
            log.error("❌  Health check FAILED. Review errors above.")
    else:
        log.info("✅  Health flag found (%s) — skipping full check", health_flag.read_text().strip())

    # ── MAIN SCHEDULER LOOP ───────────────────────────────────────────────
    log.info("🤖  Entering scheduler loop. (Ctrl+C to stop)")

    while True:
        window = determine_current_window()
        log.info("📍  Current window: %s  (%s ET)", window, now_et().strftime("%H:%M:%S"))

        if window == "morning":
            run_morning(portfolio, analyzer, poly)
            secs = seconds_until(EVENING_HOUR_START)
            sleep_with_countdown(secs, "evening session")

        elif window == "evening":
            run_evening(portfolio, analyzer, poly)
            secs = seconds_until(MORNING_HOUR_START)
            sleep_with_countdown(secs, "morning session")

        elif window == "sleep_until_morning":
            secs = seconds_until(MORNING_HOUR_START)
            sleep_with_countdown(secs, "morning session")

        elif window == "sleep_until_evening":
            secs = seconds_until(EVENING_HOUR_START)
            sleep_with_countdown(secs, "evening session")

        else:
            log.warning("Unknown window: %s. Sleeping 60s.", window)
            time.sleep(60)


if __name__ == "__main__":
    main()
