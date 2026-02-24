"""
NBA Edge Alpha — Dashboard Server
===================================
Lee PORTFOLIO_FILE y LOG_FILE de variables de entorno
para que apunte siempre al DATA_DIR correcto.
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime, date
from flask import Flask, jsonify, Response

app = Flask(__name__)

PORT = int(os.environ.get("DASHBOARD_PORT", 8080))


def get_portfolio_file() -> Path:
    return Path(os.environ.get("PORTFOLIO_FILE", "portfolio.json"))

def get_log_file() -> Path:
    return Path(os.environ.get("LOG_FILE", "bot.log"))


def read_portfolio() -> dict:
    p = get_portfolio_file()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"capital": 20.0, "initial": 20.0, "total_pnl": 0.0, "bets": []}


def read_log(lines: int = 150) -> list:
    p = get_log_file()
    if not p.exists():
        return ["Bot log not found yet — waiting for first session..."]
    try:
        all_lines = p.read_text(errors="replace").splitlines()
        return all_lines[-lines:]
    except Exception as e:
        return [f"Error reading log: {e}"]


def bot_status() -> str:
    flag = get_portfolio_file().parent / ".health_ok"
    if not flag.exists():
        return "STARTING"
    lines = read_log(8)
    for line in reversed(lines):
        l = line.lower()
        if "sleeping" in l or "still sleeping" in l:
            return "SLEEPING"
        if "morning session" in l or "first boot" in l:
            return "MORNING"
        if "evening session" in l:
            return "EVENING"
        if "health check" in l:
            return "HEALTHCHECK"
        if "retrying in 1 hour" in l:
            return "WAITING_RESULTS"
    return "IDLE"


@app.route("/api/state")
def api_state():
    data    = read_portfolio()
    bets    = data.get("bets", [])
    capital = data.get("capital", 20.0)
    initial = data.get("initial", 20.0)
    pnl     = data.get("total_pnl", 0.0)
    roi     = ((capital - initial) / initial * 100) if initial else 0

    resolved = [b for b in bets if b.get("status") == "RESOLVED"]
    open_b   = [b for b in bets if b.get("status") == "OPEN"]
    wins     = [b for b in resolved if (b.get("pnl") or 0) > 0]
    losses   = [b for b in resolved if (b.get("pnl") or 0) <= 0]
    win_rate = (len(wins) / len(resolved) * 100) if resolved else 0

    # PnL histórico acumulativo por fecha
    from collections import defaultdict
    cumulative  = 0.0
    daily       = {}
    for b in sorted(resolved, key=lambda x: x.get("date", "")):
        cumulative += b.get("pnl", 0)
        daily[b["date"]] = round(cumulative, 4)
    pnl_history = [{"date": d, "pnl": v} for d, v in sorted(daily.items())]

    today_str   = str(date.today())
    todays_bets = [b for b in bets if b.get("date") == today_str]

    return jsonify({
        "status"      : bot_status(),
        "timestamp"   : datetime.now().isoformat(),
        "capital"     : round(capital, 4),
        "initial"     : initial,
        "total_pnl"   : round(pnl, 4),
        "roi_pct"     : round(roi, 2),
        "win_rate"    : round(win_rate, 1),
        "total_bets"  : len(bets),
        "open_bets"   : len(open_b),
        "wins"        : len(wins),
        "losses"      : len(losses),
        "exposure_pct": round((sum(b["amount_usd"] for b in open_b) / capital * 100) if capital else 0, 1),
        "pnl_history" : pnl_history,
        "todays_bets" : todays_bets,
        "all_bets"    : list(reversed(bets[-50:])),
    })


@app.route("/api/log")
def api_log():
    return jsonify({"lines": read_log(150)})


@app.route("/")
def index():
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return Response(html_path.read_text(), mimetype="text/html")
    return Response("<h1>Dashboard HTML not found</h1>", mimetype="text/html")


def _serve():
    try:
        from waitress import serve as waitress_serve
        print(f"📊  Dashboard on http://0.0.0.0:{PORT} (waitress)")
        waitress_serve(app, host="0.0.0.0", port=PORT, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def start_dashboard():
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    _serve()
