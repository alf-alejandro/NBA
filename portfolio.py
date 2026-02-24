"""
Portfolio Manager
=================
Tracks capital, open bets, resolved bets, and PnL.
Persists to DATA_DIR/portfolio.json (path passed in constructor).
"""

import json
import logging
import uuid
from pathlib import Path
from datetime import date
from typing import Optional

log = logging.getLogger("nba-bot.portfolio")


class Portfolio:
    def __init__(self, filepath: str, initial_capital: float = 20.0):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._load(initial_capital)

    def _load(self, initial_capital: float):
        if self.filepath.exists():
            with open(self.filepath) as f:
                data = json.load(f)
            self.capital   = data["capital"]
            self.initial   = data["initial"]
            self.bets      = data["bets"]
            self.total_pnl = data["total_pnl"]
            log.info("Portfolio loaded from %s — Capital: $%.2f | PnL: $%.2f",
                     self.filepath, self.capital, self.total_pnl)
        else:
            self.capital   = initial_capital
            self.initial   = initial_capital
            self.bets      = []
            self.total_pnl = 0.0
            log.info("New portfolio created at %s — Capital: $%.2f", self.filepath, self.capital)
            self.save()

    def save(self):
        data = {
            "capital"   : round(self.capital,   4),
            "initial"   : self.initial,
            "total_pnl" : round(self.total_pnl, 4),
            "bets"      : self.bets,
        }
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=2)

    def deployed_capital(self) -> float:
        return sum(b["amount_usd"] for b in self.bets if b["status"] == "OPEN")

    def free_capital(self) -> float:
        return self.capital - self.deployed_capital()

    def exposure_ratio(self) -> float:
        return self.deployed_capital() / self.capital if self.capital else 1.0

    def available_capital(self, max_total_exposed: float = 0.50) -> float:
        max_deployable     = self.capital * max_total_exposed
        currently_deployed = self.deployed_capital()
        return max(0.0, max_deployable - currently_deployed)

    def place_bet(self, bet: dict):
        if "id" not in bet or not bet["id"]:
            bet["id"] = str(uuid.uuid4())[:8]
        self.bets.append(bet)
        log.info("Bet %s recorded: $%.2f on %s", bet["id"], bet["amount_usd"], bet["bet_on"])

    def open_bets_today(self) -> list[dict]:
        today = str(date.today())
        return [b for b in self.bets if b["status"] == "OPEN" and b.get("date") == today]

    def open_bets_all(self) -> list[dict]:
        """All open bets regardless of date (for multi-day resolution)."""
        return [b for b in self.bets if b["status"] == "OPEN"]

    def resolve_bet(self, bet_key: str, winner: str, final_score: str):
        """bet_key = 'home|away'"""
        home, away = bet_key.split("|", 1)
        for bet in self.bets:
            if bet["status"] != "OPEN":
                continue
            if bet["home"] == home and bet["away"] == away:
                bet["status"]      = "RESOLVED"
                bet["result"]      = winner
                bet["final_score"] = final_score

                won   = (winner == bet["bet_on"])
                price = max(0.01, bet.get("poly_price", 50) / 100.0)

                if won:
                    profit = bet["amount_usd"] * (1.0 / price - 1)
                    bet["pnl"]      = round(profit, 4)
                    self.capital   += profit
                    self.total_pnl += profit
                    log.info("✅  WIN  bet %s: +$%.2f  (PnL total: $%.2f)",
                             bet["id"], profit, self.total_pnl)
                else:
                    loss = -bet["amount_usd"]
                    bet["pnl"]      = round(loss, 4)
                    self.capital   += loss
                    self.total_pnl += loss
                    log.info("❌  LOSS bet %s: -$%.2f  (PnL total: $%.2f)",
                             bet["id"], bet["amount_usd"], self.total_pnl)
                return

        log.warning("Could not find open bet for key: %s", bet_key)

    def print_summary(self):
        resolved  = [b for b in self.bets if b["status"] == "RESOLVED"]
        wins      = sum(1 for b in resolved if (b.get("pnl") or 0) > 0)
        open_count= sum(1 for b in self.bets if b["status"] == "OPEN")
        win_rate  = (wins / len(resolved) * 100) if resolved else 0
        roi       = ((self.capital - self.initial) / self.initial * 100) if self.initial else 0

        log.info("═" * 55)
        log.info("  📊  PORTFOLIO SUMMARY")
        log.info("  Starting capital : $%.2f", self.initial)
        log.info("  Current capital  : $%.2f", self.capital)
        log.info("  Total PnL        : %+.2f$", self.total_pnl)
        log.info("  ROI              : %+.1f%%", roi)
        log.info("  Total bets       : %d  (open: %d)", len(self.bets), open_count)
        log.info("  Win rate         : %.1f%%  (%d/%d)", win_rate, wins, len(resolved))
        log.info("  Exposure         : %.1f%%", self.exposure_ratio() * 100)
        log.info("═" * 55)
