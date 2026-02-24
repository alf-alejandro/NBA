"""
NBA Edge Alpha — Dashboard Server
===================================
Servidor Flask ligero que expone:
  GET /           → dashboard HTML
  GET /api/state  → JSON con portfolio completo + log reciente + bot status
  GET /api/log    → últimas N líneas del bot.log

Corre en el mismo proceso que el bot (hilo separado) o como proceso independiente.
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, Response

app = Flask(__name__, static_folder="static")

PORTFOLIO_FILE = os.environ.get("PORTFOLIO_FILE", "portfolio.json")
LOG_FILE       = os.environ.get("LOG_FILE",       "bot.log")
HEALTH_FLAG    = ".health_ok"
PORT           = int(os.environ.get("DASHBOARD_PORT", 8080))


def read_portfolio() -> dict:
    p = Path(PORTFOLIO_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"capital": 20.0, "initial": 20.0, "total_pnl": 0.0, "bets": []}


def read_log(lines: int = 120) -> list[str]:
    p = Path(LOG_FILE)
    if not p.exists():
        return ["Bot log not found yet..."]
    try:
        all_lines = p.read_text(errors="replace").splitlines()
        return all_lines[-lines:]
    except Exception as e:
        return [f"Error reading log: {e}"]


def bot_status() -> str:
    flag = Path(HEALTH_FLAG)
    if not flag.exists():
        return "STARTING"
    log_lines = read_log(5)
    for line in reversed(log_lines):
        l = line.lower()
        if "sleeping" in l or "still sleeping" in l:
            return "SLEEPING"
        if "morning session" in l:
            return "MORNING"
        if "evening session" in l:
            return "EVENING"
        if "health check" in l:
            return "HEALTHCHECK"
    return "IDLE"


@app.route("/api/state")
def api_state():
    data     = read_portfolio()
    bets     = data.get("bets", [])
    capital  = data.get("capital", 20.0)
    initial  = data.get("initial", 20.0)
    pnl      = data.get("total_pnl", 0.0)
    roi      = ((capital - initial) / initial * 100) if initial else 0

    resolved = [b for b in bets if b.get("status") == "RESOLVED"]
    open_b   = [b for b in bets if b.get("status") == "OPEN"]
    wins     = [b for b in resolved if (b.get("pnl") or 0) > 0]
    losses   = [b for b in resolved if (b.get("pnl") or 0) <= 0]
    win_rate = (len(wins) / len(resolved) * 100) if resolved else 0

    # PnL history for chart (cumulative by date)
    from collections import defaultdict
    daily = defaultdict(float)
    cumulative = 0.0
    pnl_history = []
    for b in sorted(resolved, key=lambda x: x.get("date", "")):
        cumulative += b.get("pnl", 0)
        daily[b["date"]] = cumulative
    for d, v in sorted(daily.items()):
        pnl_history.append({"date": d, "pnl": round(v, 4)})

    # Today's bets
    from datetime import date
    today = str(date.today())
    todays_bets = [b for b in bets if b.get("date") == today]

    return jsonify({
        "status"     : bot_status(),
        "timestamp"  : datetime.now().isoformat(),
        "capital"    : round(capital, 4),
        "initial"    : initial,
        "total_pnl"  : round(pnl, 4),
        "roi_pct"    : round(roi, 2),
        "win_rate"   : round(win_rate, 1),
        "total_bets" : len(bets),
        "open_bets"  : len(open_b),
        "wins"       : len(wins),
        "losses"     : len(losses),
        "exposure_pct": round((sum(b["amount_usd"] for b in open_b) / capital * 100) if capital else 0, 1),
        "pnl_history": pnl_history,
        "todays_bets": todays_bets,
        "all_bets"   : list(reversed(bets[-50:])),   # last 50
    })


@app.route("/api/log")
def api_log():
    lines = read_log(150)
    return jsonify({"lines": lines})


@app.route("/")
def index():
    html_path = Path(__file__).parent / "dashboard.html"
    return Response(html_path.read_text(), mimetype="text/html")


def start_dashboard():
    """Start Flask in a daemon thread (called from bot.py)."""
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    )
    t.start()
    return t


if __name__ == "__main__":
    print(f"Starting dashboard on port {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=False)
