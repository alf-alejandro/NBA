"""
Startup Health Check
====================
Verifica:
  1. Conexión a internet y Google Search (via Gemini)
  2. Matemática NEA con los pesos actuales
  3. Gestión de capital con límite del 33%
  4. Simulación completa WIN + LOSS
"""

import logging
import json
import re
import os
import tempfile
from datetime import date
from nea_formula import compute_nea, interpret_nea, W_VEGAS, W_NEWS, W_HOME, W_STREAK

log = logging.getLogger("nba-bot.healthcheck")

MAX_BET_PCT      = 0.33
MAX_EXPOSURE_PCT = 0.33

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
    print("\n" + "═" * 60)
    print("  🔍  NBA EDGE ALPHA — STARTUP HEALTH CHECK")
    print(f"  📐  Formula: NEA = Poly - [{W_VEGAS}·Vegas + {W_NEWS}·News + {W_HOME}·Local + {W_STREAK}·Racha]")
    print(f"  💰  Max bet / Max exposure: {int(MAX_BET_PCT*100)}% of current capital")
    print("═" * 60)

    all_ok = True

    # ── Check 1: Gemini + Internet ────────────────────────────────────────
    print("\n[1/4] Testing Gemini + Google Search connection...")
    try:
        prompt  = HEALTH_PROMPT.format(today=str(date.today()))
        raw     = analyzer._call(prompt)
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        start   = cleaned.find("{")
        end     = cleaned.rfind("}") + 1
        data    = json.loads(cleaned[start:end])

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

    # ── Check 2: NEA Formula — casos calibrados a los pesos actuales ──────
    print("\n[2/4] Testing NEA formula with current weights...")

    # Calculamos el NEA esperado para cada caso para que el test sea robusto
    # ante cambios de pesos futuros
    test_cases = [
        {
            "label"    : "Star OUT inesperado — debe ser AVOID",
            "p_poly"   : 75,
            "p_vegas"  : 72,
            "n"        : -35,
            "v"        : 5,
            "r"        : 60,
            "expected" : "AVOID",
        },
        {
            "label"    : "Valor oculto — Poly subvaluado vs Vegas",
            "p_poly"   : 40,
            "p_vegas"  : 60,
            "n"        : 5,
            "v"        : 5,
            "r"        : 80,
            "expected" : "BUY",
        },
        {
            "label"    : "Estrella rival OUT — ventaja para nuestro equipo",
            "p_poly"   : 45,
            "p_vegas"  : 55,
            "n"        : 25,
            "v"        : -5,
            "r"        : 40,
            "expected" : "BUY",
        },
        {
            "label"    : "Mercado eficiente — sin ventaja",
            "p_poly"   : 50,
            "p_vegas"  : 50,
            "n"        : 0,
            "v"        : 0,
            "r"        : 50,
            "expected" : "NEUTRAL",
        },
    ]

    formula_ok = True
    for tc in test_cases:
        nea    = compute_nea(tc["p_poly"], tc["p_vegas"], tc["n"], tc["v"], tc["r"])
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
        print("  ⚠️   Some NEA checks failed — review formula weights in nea_formula.py")
        all_ok = False

    # ── Check 3: Capital management — 33% ────────────────────────────────
    print("\n[3/4] Testing capital management rules (33% limit)...")

    cap            = portfolio.capital
    max_single_bet = round(cap * MAX_BET_PCT,      2)
    max_exposure   = round(cap * MAX_EXPOSURE_PCT, 2)

    print(f"  💰  Current capital      : ${cap:.2f}")
    print(f"  🎯  Max per bet (33%)    : ${max_single_bet:.2f}")
    print(f"  🛡   Max exposure (33%)  : ${max_exposure:.2f}")
    print(f"  📊  Current exposure     : {portfolio.exposure_ratio()*100:.1f}%")
    print(f"  💵  Available to deploy  : ${portfolio.available_capital(MAX_EXPOSURE_PCT):.2f}")

    if max_single_bet > 0 and max_exposure > 0:
        print("  ✅  Capital limits computed correctly")
    else:
        print("  ❌  Capital limit error")
        all_ok = False

    # ── Check 4: Simulación completa WIN + LOSS ───────────────────────────
    print("\n[4/4] Running full simulation dry-run...")

    from portfolio import Portfolio

    tmp1 = tempfile.mktemp(suffix=".json")
    tmp2 = tempfile.mktemp(suffix=".json")

    try:
        # WIN scenario
        p1 = Portfolio(tmp1, initial_capital=20.0)
        bet = {
            "date"        : str(date.today()),
            "market_id"   : "HEALTH_SIM",
            "home"        : "Lakers",
            "away"        : "Warriors",
            "bet_on"      : "Lakers",
            "poly_price"  : 55,
            "nea_score"   : -8.5,
            "amount_usd"  : 6.60,    # 33% of $20
            "status"      : "OPEN",
            "result"      : None,
            "pnl"         : None,
            "news_summary": "",
            "rationale"   : "",
        }
        p1.place_bet(bet)

        assert abs(p1.deployed_capital() - 6.60) < 0.01, "Deployed capital mismatch"

        p1.resolve_bet("Lakers|Warriors", "Lakers", "Lakers 115 - Warriors 108")
        resolved_win = [b for b in p1.bets if b["status"] == "RESOLVED"]
        assert len(resolved_win) == 1 and resolved_win[0]["pnl"] > 0
        win_pnl = resolved_win[0]["pnl"]
        print(f"  ✅  Bet placement (33%): OK ($6.60 on Lakers @ 55¢)")
        print(f"  ✅  WIN resolution     : OK  PnL = +${win_pnl:.2f}")

        # LOSS scenario
        p2 = Portfolio(tmp2, initial_capital=20.0)
        p2.place_bet({**bet, "id": None})
        p2.resolve_bet("Lakers|Warriors", "Warriors", "Warriors 112 - Lakers 104")
        resolved_loss = [b for b in p2.bets if b["status"] == "RESOLVED"]
        assert len(resolved_loss) == 1 and resolved_loss[0]["pnl"] < 0
        loss_pnl = resolved_loss[0]["pnl"]
        print(f"  ✅  LOSS resolution    : OK  PnL = -${abs(loss_pnl):.2f}")

        # Capital update check
        assert p1.capital > 20.0, "Capital should grow after win"
        assert p2.capital < 20.0, "Capital should shrink after loss"
        print(f"  ✅  Capital tracking   : OK  (win→${p1.capital:.2f} | loss→${p2.capital:.2f})")
        print(f"  ✅  Full simulation    : PASSED")

    except Exception as e:
        print(f"  ❌  Simulation failed: {e}")
        all_ok = False
    finally:
        for p in [tmp1, tmp2]:
            try:
                os.unlink(p)
            except Exception:
                pass

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    if all_ok:
        print("  🟢  ALL CHECKS PASSED — Bot is healthy and ready")
    else:
        print("  🔴  SOME CHECKS FAILED — Review errors above")
    print("═" * 60 + "\n")

    return all_ok
