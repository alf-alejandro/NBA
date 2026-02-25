"""
GeminiAnalyzer
==============
Uses Gemini 3.1 Pro (with Google Search grounding) to:
  1. Morning → find today's NBA games, injury reports, Vegas odds, Polymarket prices
  2. Evening → find final scores and resolve open bets
"""

import json
import logging
import re
import time
from typing import Any
from datetime import date

from google import genai
from google.genai import types

log = logging.getLogger("nba-bot.analyzer")

# ── System prompt (safety + capital mgmt framing) ────────────────────────────
SYSTEM_PROMPT = """
You are NBA Edge Alpha, an expert sports betting analyst AI assistant.
Your role is to analyze NBA games and provide structured data for a disciplined
betting simulation bot.

CRITICAL RULES you must always follow:
1. CAPITAL SAFETY FIRST: Never recommend risking more than 15% of bankroll per bet.
   Never recommend having more than 50% of bankroll exposed simultaneously.
2. OBJECTIVITY: Base all analysis on verifiable, current data (injury reports,
   Vegas moneylines, recent team performance). Never guess or fabricate data.
3. STRUCTURED OUTPUT: Always respond in valid JSON so the bot can parse your output.
   Do not include markdown fences (```json) — return raw JSON only.
4. CONSERVATIVE BIAS: When data is uncertain or conflicting, score N=0 (neutral).
   It is better to miss a bet than to take a bad one.
5. SEARCH BEFORE ANSWERING: Always use Google Search to get today's real data.
   Do not rely on training data for injury reports or current odds.
"""

MORNING_PROMPT_TEMPLATE = """
Today is {today}. Current time: {time_et} ET.
Target date for bets: {target_date}.

Your job: find ALL NBA games scheduled for {target_date} that have NOT started yet.

STEP 1 — Search these queries to get the full schedule:
  - "NBA games {target_date}"
  - "NBA schedule {target_date}"
  List EVERY game found, there should be between 5 and 15 games on most days.

STEP 2 — For EACH game on the {target_date} schedule, search:
  - "NBA {away} vs {home} odds {target_date}" → Vegas moneyline
  - "NBA injury report {target_date}" → injury status for both teams
  - "{away} {home} last 5 games" → recent form
  - "Polymarket NBA {home} win {target_date}" → Polymarket market price

STEP 3 — Apply filters:
  - EXCLUDE games that have already started or finished (tip-off before {time_et} ET if date is today)
  - EXCLUDE games where you cannot find ANY Vegas moneyline odds
  - EXCLUDE games where you cannot find a Polymarket market price
  - For poly_price: use the REAL Polymarket "Yes" price in cents (e.g. 60 means 60 cents = 60%)
  - Do NOT substitute poly_price with vegas_prob

IMPORTANT: Search ALL games on the slate, not just the featured matchup.
Most NBA days have 8-12 games. Find them all, then filter.

Return a JSON array — one entry per qualifying game:
{{
  "home": "Team Name",
  "away": "Team Name",
  "game_date": "{target_date}",
  "tip_off_et": "HH:MM",
  "bet_on": "Team Name (Vegas favorite)",
  "market_id": "SIMULATED",
  "poly_price": <integer 1-99, real Polymarket price>,
  "vegas_prob": <integer 1-99, implied prob from moneyline>,
  "news_score": <integer -40 to 20, 0 if no news>,
  "home_away_factor": <5 if bet_on is home, -5 if visitor>,
  "streak_pct": <integer 0-100>,
  "news_summary": "Key injuries or NO INJURY NEWS",
  "rationale": "1-2 sentences"
}}

Vegas implied probability:
  Favorite -150 → 150/250 = 60%
  Underdog +130 → 100/230 = 43%

Return ONLY the raw JSON array. No markdown. If none qualify: []
"""

EVENING_PROMPT_TEMPLATE = """
Today is {today}. Use Google Search to find the FINAL SCORES for these NBA games:

{bets_json}

For each bet (identified by home + away teams), return a JSON object:
{{
  "resolutions": [
    {{
      "home": "Team Name",
      "away": "Team Name",
      "winner": "Team Name (the actual winner)",
      "home_score": <integer>,
      "away_score": <integer>,
      "final_score": "Home 110 - Away 105",
      "status": "FINAL"
    }}
  ]
}}

If a game has not finished yet, set "status": "POSTPONED" or "IN_PROGRESS".
Return ONLY raw JSON. No markdown, no extra text.
"""


class GeminiAnalyzer:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.model  = "gemini-3-flash-preview"

    def _call(self, prompt: str, max_retries: int = 4) -> str:
        """Call Gemini with Google Search grounding, HIGH thinking, and retry on 429."""
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)],
            )
        ]
        tools  = [types.Tool(googleSearch=types.GoogleSearch())]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
            tools=tools,
        )

        for attempt in range(1, max_retries + 1):
            try:
                full_response = ""
                for chunk in self.client.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=config,
                ):
                    if chunk.text:
                        full_response += chunk.text
                return full_response.strip()

            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit = "429" in str(e) or "quota" in err_str or "resource_exhausted" in err_str
                is_server_err = "500" in str(e) or "503" in str(e) or "unavailable" in err_str

                if (is_rate_limit or is_server_err) and attempt < max_retries:
                    wait = 2 ** attempt * 15   # 30s, 60s, 120s
                    log.warning(
                        "⏳  Gemini %s (attempt %d/%d). Retrying in %ds...",
                        "rate limited" if is_rate_limit else "server error",
                        attempt, max_retries, wait
                    )
                    time.sleep(wait)
                else:
                    log.error("❌  Gemini call failed after %d attempts: %s", attempt, e)
                    raise

        raise RuntimeError("Gemini _call exhausted all retries")

    # ── Morning ───────────────────────────────────────────────────────────────
    def morning_analysis(self) -> str:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now_et    = datetime.now(tz=ZoneInfo("America/New_York"))
        time_str  = now_et.strftime("%H:%M")
        today     = str(now_et.date())
        tomorrow  = str((now_et + timedelta(days=1)).date())
        # Si ya es tarde (>14:00 ET), buscar juegos de mañana
        target    = tomorrow if now_et.hour >= 14 else today
        prompt = MORNING_PROMPT_TEMPLATE.format(
            today       = today,
            tomorrow    = tomorrow,
            target_date = target,
            time_et     = time_str,
        )
        log.info("Calling Gemini — ET: %s | searching games for: %s", time_str, target)
        return self._call(prompt)

    def parse_games(self, raw: str) -> list[dict]:
        """Extract and validate JSON array from Gemini response."""
        try:
            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            start = cleaned.find("[")
            end   = cleaned.rfind("]") + 1
            if start == -1 or end == 0:
                log.warning("No JSON array found in morning response.")
                return []
            games = json.loads(cleaned[start:end])
            log.info("Parsed %d raw games from Gemini.", len(games))

            valid = []
            for g in games:
                home      = g.get("home", "?")
                away      = g.get("away", "?")
                poly      = g.get("poly_price", 0)
                vegas     = g.get("vegas_prob", 0)

                # Rechazar si no hay precio real de Vegas
                if vegas <= 0 or vegas >= 100:
                    log.warning("  ⛔  %s @ %s — vegas_prob inválido (%s), descartado", away, home, vegas)
                    continue

                # Si poly_price es 0 o no existe → descartar, no hay precio real de Polymarket
                if poly <= 0 or poly >= 100:
                    log.warning("  ⛔  %s @ %s — sin precio real de Polymarket (poly_price=%s), descartado",
                                away, home, poly)
                    continue

                valid.append(g)

            log.info("%d games valid after filtering (removed %d without real odds).",
                     len(valid), len(games) - len(valid))
            return valid

        except json.JSONDecodeError as e:
            log.error("Failed to parse games JSON: %s\nRaw: %s", e, raw[:500])
            return []

    # ── Evening ───────────────────────────────────────────────────────────────
    def evening_resolution(self, open_bets: list[dict]) -> str:
        bets_json = json.dumps(
            [{"home": b["home"], "away": b["away"], "bet_on": b["bet_on"]} for b in open_bets],
            indent=2,
        )
        prompt = EVENING_PROMPT_TEMPLATE.format(
            today=str(date.today()),
            bets_json=bets_json,
        )
        log.info("Calling Gemini for evening resolution...")
        return self._call(prompt)

    def parse_results(self, raw: str) -> dict[str, Any]:
        """Returns dict keyed by 'home|away' → outcome."""
        try:
            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            start = cleaned.find("{")
            end   = cleaned.rfind("}") + 1
            data  = json.loads(cleaned[start:end])
            resolutions = data.get("resolutions", [])
            result_map = {}
            for r in resolutions:
                key = f"{r['home']}|{r['away']}"
                result_map[key] = r
            return result_map
        except json.JSONDecodeError as e:
            log.error("Failed to parse results JSON: %s", e)
            return {}
