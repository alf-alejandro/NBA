"""
NBA Edge Alpha (NEA) Formula
=============================
  NEA = P_poly - [(0.45 * P_vegas) + (0.40 * N) + (0.10 * V) + (0.05 * R)]

Umbrales de señal:
  NEA < -8   → BUY  HIGH
  NEA < -3   → BUY  MEDIUM   ← umbral más sensible para no perder oportunidades
  -3 to 3    → NEUTRAL
  NEA > 3    → AVOID MEDIUM
  NEA > 8    → AVOID HIGH
"""

from typing import TypedDict

class NEAResult(TypedDict):
    nea_score  : float
    action     : str
    confidence : str
    breakdown  : dict

# ── Pesos ─────────────────────────────────────────────────────────────────────
W_VEGAS  = 0.45
W_NEWS   = 0.40
W_HOME   = 0.10
W_STREAK = 0.05

def normalize_news_score(raw_n: float) -> float:
    clamped = max(-40.0, min(20.0, float(raw_n)))
    return (clamped + 40) / 60 * 100

def normalize_streak(win_pct: float) -> float:
    return max(0.0, min(100.0, float(win_pct)))

def compute_nea(p_poly: float, p_vegas: float, n: float, v: float, r: float) -> float:
    """
    NEA = P_poly - [(0.45·Vegas) + (0.40·News) + (0.10·Local) + (0.05·Racha)]
    Negativo → BUY (Poly subvalorado). Positivo → AVOID.
    """
    n_norm = normalize_news_score(n)
    r_norm = normalize_streak(r)
    real_prob = (
        W_VEGAS  * p_vegas
      + W_NEWS   * n_norm
      + W_HOME   * v
      + W_STREAK * r_norm
    )
    return round(p_poly - real_prob, 3)

def compute_nea_breakdown(p_poly: float, p_vegas: float, n: float, v: float, r: float) -> dict:
    """Igual que compute_nea pero devuelve el desglose de cada componente."""
    n_norm    = normalize_news_score(n)
    r_norm    = normalize_streak(r)
    c_vegas   = round(W_VEGAS  * p_vegas, 2)
    c_news    = round(W_NEWS   * n_norm,  2)
    c_home    = round(W_HOME   * v,       2)
    c_streak  = round(W_STREAK * r_norm,  2)
    real_prob = round(c_vegas + c_news + c_home + c_streak, 2)
    nea       = round(p_poly - real_prob, 3)
    return {
        "p_poly"    : p_poly,
        "real_prob" : real_prob,
        "nea"       : nea,
        "vegas_contrib"  : c_vegas,
        "news_contrib"   : c_news,
        "home_contrib"   : c_home,
        "streak_contrib" : c_streak,
        "n_normalized"   : round(n_norm, 1),
        "r_normalized"   : round(r_norm, 1),
    }

def interpret_nea(nea: float) -> NEAResult:
    if nea < -8:
        action, confidence = "BUY", "HIGH"
    elif nea < -3:
        action, confidence = "BUY", "MEDIUM"
    elif nea <= 3:
        action, confidence = "NEUTRAL", "LOW"
    elif nea <= 8:
        action, confidence = "AVOID", "MEDIUM"
    else:
        action, confidence = "AVOID", "HIGH"
    return NEAResult(nea_score=nea, action=action, confidence=confidence, breakdown={})

NEWS_SCORE_GUIDE = {
    "star_out_unexpected"      : -35,
    "two_starters_out"         : -20,
    "star_out_expected"        : -15,
    "key_player_questionable"  :  -8,
    "no_news"                  :   0,
    "starter_back_from_injury" : +15,
    "star_confirmed_in"        : +20,
    "opponent_star_out"        : +25,
}
