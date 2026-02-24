"""
Polymarket / Gamma API Client
==============================
In SIMULATION mode (no key or SIMULATE=true), all operations are logged but not executed.
In LIVE mode, uses the Gamma API to fetch market data.

Gamma API docs: https://docs.polymarket.com
"""

import os
import logging
import requests
from typing import Optional

log = logging.getLogger("nba-bot.polymarket")

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class PolymarketClient:
    def __init__(self, api_key: str = ""):
        self.api_key    = api_key
        self.simulate   = os.environ.get("SIMULATE", "true").lower() == "true"
        self.session    = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

        mode = "SIMULATION" if self.simulate else "LIVE"
        log.info("Polymarket client initialized (%s mode)", mode)

    # ── Market data ───────────────────────────────────────────────────────────
    def get_markets(self, keyword: str = "NBA", limit: int = 20) -> list[dict]:
        """Fetch active NBA markets from Gamma."""
        if self.simulate:
            log.info("[SIM] Would fetch markets for: %s", keyword)
            return []
        try:
            resp = self.session.get(
                f"{GAMMA_BASE_URL}/markets",
                params={"keyword": keyword, "active": True, "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except requests.RequestException as e:
            log.error("Failed to fetch Polymarket markets: %s", e)
            return []

    def get_market_price(self, market_id: str) -> Optional[float]:
        """Returns current Yes price (0–1) for a market."""
        if self.simulate:
            log.info("[SIM] Would fetch price for market: %s", market_id)
            return None
        try:
            resp = self.session.get(
                f"{GAMMA_BASE_URL}/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("bestBid", 0))
        except requests.RequestException as e:
            log.error("Failed to fetch market price %s: %s", market_id, e)
            return None

    # ── Order placement ──────────────────────────────────────────────────────
    def place_order(
        self,
        market_id: str,
        side: str,          # "buy" | "sell"
        amount_usd: float,
        price: float,       # 0–1
    ) -> dict:
        """Place a limit order. Returns order dict."""
        if self.simulate:
            order = {
                "simulated"  : True,
                "market_id"  : market_id,
                "side"       : side,
                "amount_usd" : amount_usd,
                "price"      : price,
                "status"     : "SIMULATED",
            }
            log.info("[SIM] Order: %s $%.2f on %s @ %.2f", side.upper(), amount_usd, market_id, price)
            return order

        try:
            payload = {
                "marketId"  : market_id,
                "side"      : side,
                "size"      : str(amount_usd),
                "price"     : str(round(price, 4)),
                "orderType" : "limit",
            }
            resp = self.session.post(
                f"{GAMMA_BASE_URL}/order",
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Order placed successfully on %s", market_id)
            return resp.json()
        except requests.RequestException as e:
            log.error("Failed to place order: %s", e)
            return {"error": str(e), "status": "FAILED"}
