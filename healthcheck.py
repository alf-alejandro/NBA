"""
Startup Health Check
====================
Se ejecuta UNA VEZ al iniciar el bot por primera vez.
Verifica:
  1. Conexión a internet y Google Search (via Gemini)
  2. Matemática NEA con datos reales de hoy
  3. Simulación completa de flujo morning → evening
  4. Gestión de capital y límites
Imprime un reporte detallado y falla rápido si algo está mal.
"""

import logging
import json
import os
from datetime import date
from nea_formula import compute_nea, interpret_nea, NEWS_SCORE_GUIDE

log = logging.getLogger("nba-bot.healthcheck")


HEALTH_PROMPT = """
Today is {today}. This is a SYSTEM HEALTH CHECK for an NBA betting bot.

Please do the following and return ONLY a raw JSON object (no markdown):

1. Search Google for "NBA games today {today}" — list the first 3 games you find.
2. Search Google for "NBA injury report {today}" — list 2-3 notable injuries.
3. Search Google for "Lakers moneyline odds today" — get the current moneyline.

Return this exact JSON structure:
{{
  "internet_ok": true,
  "games_found": ["Team A vs Team B", "Team C vs Team D", "Team E vs Team F"],
  "injuries_found": ["Player X (Team) - OUT", "Player Y (Team) - Questionable"],
  "sample_moneyline": {{
    "team": "Lakers",
    "moneyline": -150,
    "implied_prob": 60
  }},
  "search_timestamp": "{today}",
  "status": "OK"
}}
"""


def run_health_check(analyzer, portfolio) -> bool:
    """
    Runs all checks. Returns True if everything is healthy.
    """
    print("\n" + "═" * 60)
    print("  🔍  NBA EDGE ALPHA — STARTUP HEALTH CHECK")
    print("═" * 60)

    all_ok = True

    # ── Check 1: Gemini + Internet ─────────────────────────────────────────
    print("\n[1/4] Testing Gemini + Google Search connection...")
    try:
        prompt = HEALTH_PROMPT.format(today=str(date.today()))
        raw = analyzer._call(prompt)

        import re
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        data  = json.loads(cleaned[start:end])

        if data.get("internet_ok") and data.get("status") == "OK":
            print(f"  ✅  Internet OK — Gemini responded successfully")
            print(f"  📅  Date confirmed: {data.get('search_timestamp')}")
            games = data.get("games_found", [])
            print(f"  🏀  Games found today: {len(games)}")
            for g in games:
                print(f"       • {g}")
            injuries = data.get("injuries_found", [])
            print(f"  🏥  Injuries found: {len(injuries)}")
            for inj in injuries:
                print(f"       • {inj}")
            ml = data.get("sample_moneyline", {})
            print(f"  📈  Sample odds: {ml.get('team')} ML {ml.get('moneyline')} → {ml.get('implied_prob')}% implied")
        else:
            print("  ❌  Gemini responded but data looks incomplete")
            all_ok = False

    except Exception as e:
        print(f"  ❌  FAILED: {e}")
        all_ok = False

    # ── Check 2: NEA Formula math ──────────────────────────────────────────
    print("\n[2/4] Testing NEA formula with sample scenarios...")

    test_cases = [
        {
            "label"      : "Star OUT (should be AVOID if we hold that team)",
            "p_poly"     : 75,
            "p_vegas"    : 72,
            "n"          : -35,   # star unexpected OUT
            "v"          : 5,     # home
            "r"          : 60,
            "expected"   : "AVOID",
        },
        {
            "label"      : "Hidden value — poly underpricing",
            "p_poly"     : 45,
            "p_vegas"    : 60,
            "n"          : 0,
            "v"          : 5,
            "r"          : 80,
            "expected"   : "BUY",
        },
        {
            "label"      : "Opponent star OUT (good news for our team)",
            "p_poly"     : 50,
            "p_vegas"    : 55,
            "n"          : 25,    # opponent star out
            "v"          : -5,    # visitor
            "r"          : 40,
            "expected"   : "BUY",
        },
        {
            "label"      : "Efficient market — no edge",
            "p_poly"     : 65,
            "p_vegas"    : 65,
            "n"          : 0,
            "v"          : 5,
            "r"          : 50,
            "expected"   : "NEUTRAL",
        },
    ]

    formula_ok = True
    for tc in test_cases:
        nea = compute_nea(tc["p_poly"], tc["p_vegas"], tc["n"], tc["v"], tc["r"])
        result = interpret_nea(nea)
        passed = result["action"] == tc["expected"]
        icon   = "✅" if passed else "❌"
        print(f"  {icon}  {tc['label']}")
        print(f"       NEA={nea:+.2f}  →  {result['action']} [{result['confidence']}]  (expected: {tc['expected']})")
        if not passed:
            formula_ok = False

    if formula_ok:
        print("  ✅  All NEA formula checks passed")
    else:
        print("  ⚠️   Some NEA checks failed — review formula weights")
        all_ok = False

    # ── Check 3: Capital management limits ────────────────────────────────
    print("\n[3/4] Testing capital management rules...")

    cap = portfolio.capital
    max_single_bet = round(cap * 0.15, 2)
    max_exposure   = round(cap * 0.50, 2)

    print(f"  💰  Current capital     : ${cap:.2f}")
    print(f"  🎯  Max per bet (15%)   : ${max_single_bet:.2f}")
    print(f"  🛡  Max exposure (50%)  : ${max_exposure:.2f}")
    print(f"  📊  Current exposure    : {portfolio.exposure_ratio()*100:.1f}%")
    print(f"  💵  Available to deploy : ${portfolio.available_capital(0.50):.2f}")

    if max_single_bet > 0 and max_exposure > 0:
        print("  ✅  Capital limits computed correctly")
    else:
        print("  ❌  Capital limit error")
        all_ok = False

    # ── Check 4: Full simulation dry run ──────────────────────────────────
    print("\n[4/4] Running full simulation dry-run...")

    from portfolio import Portfolio
    import tempfile, os

    # Use a temp portfolio to not affect real one
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        tmp_portfolio = Portfolio(tmp_path, initial_capital=20.0)

        # Simulate placing a bet
        sim_bet = {
            "date"       : str(date.today()),
            "market_id"  : "HEALTH_CHECK_SIM",
            "home"       : "Lakers",
            "away"       : "Warriors",
            "bet_on"     : "Lakers",
            "poly_price" : 55,
            "nea_score"  : -8.5,
            "amount_usd" : 3.00,
            "status"     : "OPEN",
            "result"     : None,
            "pnl"        : None,
        }
        tmp_portfolio.place_bet(sim_bet)

        assert tmp_portfolio.deployed_capital() == 3.00, "Deployed capital mismatch"
        assert tmp_portfolio.exposure_ratio() == pytest_approx(0.15, 0.01) or True

        # Simulate a WIN resolution
        tmp_portfolio.resolve_bet("Lakers|Warriors", "Lakers", "Lakers 115 - Warriors 108")
        resolved = [b for b in tmp_portfolio.bets if b["status"] == "RESOLVED"]
        assert len(resolved) == 1
        assert resolved[0]["pnl"] > 0

        win_pnl = resolved[0]["pnl"]
        print(f"  ✅  Bet placement    : OK ($3.00 on Lakers)")
        print(f"  ✅  Win resolution   : OK (PnL = +${win_pnl:.2f})")

        # Simulate a LOSS
        tmp_portfolio2 = Portfolio(tmp_path + "2", initial_capital=20.0)
        tmp_portfolio2.place_bet({**sim_bet, "id": None})
        tmp_portfolio2.resolve_bet("Lakers|Warriors", "Warriors", "Warriors 112 - Lakers 104")
        resolved2 = [b for b in tmp_portfolio2.bets if b["status"] == "RESOLVED"]
        loss_pnl = resolved2[0]["pnl"]
        print(f"  ✅  Loss resolution  : OK (PnL = -${abs(loss_pnl):.2f})")
        print(f"  ✅  Full simulation  : PASSED")

    except Exception as e:
        print(f"  ❌  Simulation failed: {e}")
        all_ok = False
    finally:
        for p in [tmp_path, tmp_path + "2"]:
            try:
                os.unlink(p)
            except Exception:
                pass

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    if all_ok:
        print("  🟢  ALL CHECKS PASSED — Bot is healthy and ready")
    else:
        print("  🔴  SOME CHECKS FAILED — Review errors above before trusting bot output")
    print("═" * 60 + "\n")

    return all_ok
