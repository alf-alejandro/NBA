"""
NBA Edge Alpha (NEA) Formula
=============================
News-Aggressive version (as designed with Gemini):

  NEA = P_poly - [(0.35 * P_vegas) + (0.50 * N) + (0.10 * V) + (0.05 * R)]

Where:
  P_poly  → Polymarket "Yes" price (0–100 scale)
  P_vegas → Implied probability from moneyline odds (0–100 scale)
  N       → News/Injury Impact Factor (-40 to +20, normalized to 0-100)
  V       → Home/Away factor (+5 home, -5 away)
  R       → Win % in last 5 games (0–100)

Signal thresholds:
  NEA < -5  → BUY  (market underpricing the team)
  -5 to 5   → NEUTRAL
  NEA > 5   → AVOID/SELL
"""

from typing import TypedDict


class NEAResult(TypedDict):
    nea_score  : float
    action     : str          # "BUY" | "NEUTRAL" | "AVOID"
    confidence : str          # "HIGH" | "MEDIUM" | "LOW"
    breakdown  : dict


# ── Normalization helpers ──────────────────────────────────────────────────────

def normalize_news_score(raw_n: float) -> float:
    """
    Map raw news impact (-40 to +20) into a 0-100 scale so it's comparable
    to the other probability inputs.
      raw -40  →  0   (star OUT, huge negative)
      raw   0  →  50  (no news)
      raw +20  → 100  (star confirmed IN after doubt)
    """
    # Clamp to expected range
    clamped = max(-40.0, min(20.0, float(raw_n)))
    return (clamped + 40) / 60 * 100


def normalize_streak(win_pct: float) -> float:
    """Win % in last 5 games, already 0-100. Clamp for safety."""
    return max(0.0, min(100.0, float(win_pct)))


# ── Core formula ──────────────────────────────────────────────────────────────

def compute_nea(
    p_poly  : float,   # 0–100
    p_vegas : float,   # 0–100
    n       : float,   # raw news score: -40 to +20
    v       : float,   # +5 home / -5 away
    r       : float,   # 0–100 streak win pct
) -> float:
    """
    Returns the NBA Edge Alpha score.
    Negative → BUY opportunity (poly underpriced vs real prob).
    Positive → AVOID (poly overpriced).
    """
    n_norm = normalize_news_score(n)
    r_norm = normalize_streak(r)

    real_prob = (
        0.35 * p_vegas
      + 0.50 * n_norm
      + 0.10 * v        # v is already a small delta; treat as additive offset
      + 0.05 * r_norm
    )

    return round(p_poly - real_prob, 3)


# ── Interpretation ────────────────────────────────────────────────────────────

def interpret_nea(nea: float) -> NEAResult:
    if nea < -10:
        action, confidence = "BUY", "HIGH"
    elif nea < -5:
        action, confidence = "BUY", "MEDIUM"
    elif nea <= 5:
        action, confidence = "NEUTRAL", "LOW"
    elif nea <= 10:
        action, confidence = "AVOID", "MEDIUM"
    else:
        action, confidence = "AVOID", "HIGH"

    return NEAResult(
        nea_score  = nea,
        action     = action,
        confidence = confidence,
        breakdown  = {},
    )


# ── News score reference table ────────────────────────────────────────────────
NEWS_SCORE_GUIDE = {
    "star_out_unexpected"       : -35,   # Embiid, Giannis, Tatum surprise OUT
    "two_starters_out"          : -20,
    "star_out_expected"         : -15,   # already priced in
    "key_player_questionable"   : -8,
    "no_news"                   :  0,
    "starter_back_from_injury"  : +15,
    "star_confirmed_in"         : +20,
    "opponent_star_out"         : +25,   # inverse: benefits the bet team
}
