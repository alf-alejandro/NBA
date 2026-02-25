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

Your job: find NBA games that have NOT started yet today (tip-off in the future).
Do NOT include games already in progress or finished.

STEP 1 — Search "NBA schedule {today}" → get all games and tip-off times.
STEP 2 — Filter: keep ONLY games that start AFTER {time_et} ET.
STEP 3 — For each remaining game search:
  - Vegas moneyline odds (ESPN, covers.com, or any sportsbook)
  - NBA injury report (nba.com or ESPN)
  - Last 5 results for both teams
  - Search "Polymarket NBA {today} winner" for market prices

STRICT RULES — if you cannot find real data, use these exact defaults:
  vegas_prob  → REQUIRED, must find real moneyline. If truly unavailable skip the game.
  news_score  → 0 if no injury news found

Do NOT include a game if:
  - It has already started or finished
  - You cannot find a real Vegas moneyline for it
  - You cannot find a real Polymarket price for it (poly_price MUST be a real market price, never estimate or use vegas_prob as substitute)

Return a JSON array. Each element:
{{
  "home": "Team Name",
  "away": "Team Name",
  "tip_off_et": "HH:MM",
  "bet_on": "Team Name (the favorite based on Vegas odds)",
  "market_id": "SIMULATED",
  "poly_price": <integer 1-99, estimate from vegas_prob if Polymarket not found>,
  "vegas_prob": <integer 1-99, IMPLIED probability from real moneyline — REQUIRED>,
  "news_score": <integer -40 to 20>,
  "home_away_factor": <5 if bet_on is home, -5 if visitor>,
  "streak_pct": <integer 0-100, win % last 5 games>,
  "news_summary": "Key injuries or NO INJURY NEWS",
  "rationale": "1-2 sentences based on real data found"
}}

Vegas implied probability formula:
  Favorite -150 → 150/(150+100) = 60%
  Underdog +130 → 100/(130+100) = 43%

Return ONLY the raw JSON array. No markdown, no explanation.
If zero games qualify, return an empty array: []
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
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%H:%M")
        prompt = MORNING_PROMPT_TEMPLATE.format(
            today   = str(date.today()),
            time_et = now_et,
        )
        log.info("Calling Gemini for morning analysis (current ET time: %s)...", now_et)
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
