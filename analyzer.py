"""
GeminiAnalyzer
==============
Uses Gemini 2.5 Pro (with Google Search grounding) to:
  1. Morning → find today's NBA games, injury reports, Vegas odds, Polymarket prices
  2. Evening → find final scores and resolve open bets
"""

import json
import logging
import re
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
Today is {today}. Use Google Search to find the following for ALL NBA games scheduled TODAY:

For each game return a JSON array where each element has these exact fields:
{{
  "home": "Team Name",
  "away": "Team Name",
  "bet_on": "Team Name (the favorite or value pick)",
  "market_id": "polymarket_market_id_or_SIMULATED",
  "poly_price": <integer 0-100, Polymarket Yes price in cents>,
  "vegas_prob": <integer 0-100, implied win probability from Vegas moneyline>,
  "news_score": <integer -40 to 20, injury impact score for the bet_on team>,
  "home_away_factor": <5 if bet_on is home team, -5 if visitor>,
  "streak_pct": <integer 0-100, win % in last 5 games for bet_on team>,
  "news_summary": "Brief explanation of key injuries or news",
  "rationale": "1-2 sentence explanation of why this is or isn't a value bet"
}}

NEWS SCORE GUIDE (for the team you are betting ON):
  Star player OUT unexpectedly:        -35
  Two starters OUT:                    -20
  Star OUT (already known):            -15
  Key player questionable:              -8
  No significant news:                   0
  Starter confirmed back from injury:  +15
  Opponent star player OUT:            +25

Search for:
1. Today's NBA schedule
2. Official NBA injury reports (nba.com or ESPN)
3. Vegas moneylines (use implied probability formula: if -110, prob = 110/210 = 52.4%)
4. Polymarket NBA markets (search "Polymarket NBA {today}")
5. Each team's last 5 game results

Return ONLY the raw JSON array. No explanation, no markdown, no extra text.
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
        self.model  = "gemini-3.1-pro-preview"

    def _call(self, prompt: str) -> str:
        """Call Gemini with Google Search grounding and HIGH thinking."""
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)],
            )
        ]
        tools = [types.Tool(googleSearch=types.GoogleSearch())]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
            tools=tools,
        )

        full_response = ""
        for chunk in self.client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=config,
        ):
            if chunk.text:
                full_response += chunk.text

        return full_response.strip()

    # ── Morning ───────────────────────────────────────────────────────────────
    def morning_analysis(self) -> str:
        prompt = MORNING_PROMPT_TEMPLATE.format(today=str(date.today()))
        log.info("Calling Gemini for morning analysis...")
        return self._call(prompt)

    def parse_games(self, raw: str) -> list[dict]:
        """Extract JSON array from Gemini response."""
        try:
            # Strip markdown fences if model included them anyway
            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            # Find JSON array
            start = cleaned.find("[")
            end   = cleaned.rfind("]") + 1
            if start == -1 or end == 0:
                log.warning("No JSON array found in morning response.")
                return []
            games = json.loads(cleaned[start:end])
            log.info("Parsed %d games from Gemini response.", len(games))
            return games
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
