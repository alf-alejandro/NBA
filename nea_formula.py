"""
NBA Edge Alpha (NEA) Formula
=============================
Balanced version:

  NEA = P_poly - [(0.45 * P_vegas) + (0.40 * N) + (0.10 * V) + (0.05 * R)]

Weights:
  P_vegas → 45%  (odds de Vegas son el ancla principal)
  N       → 40%  (noticias/lesiones siguen siendo muy importantes)
  V       → 10%  (ventaja de localía)
  R       → 5%   (racha reciente)
"""

from typing import TypedDict

class NEAResult(TypedDict):
    nea_score  : float
    action     : str
    confidence : str
    breakdown  : dict

# ── Pesos (cambiar aquí si necesitas ajustar) ─────────────────────────────────
W_VEGAS  = 0.45
W_NEWS   = 0.40
W_HOME   = 0.10
W_STREAK = 0.05

def normalize_news_score(raw_n: float) -> float:
    """
    Mapea news score (-40 a +20) → escala 0-100.
      -40  →   0  (estrella OUT, devastador)
        0  →  67  (sin noticias, neutro)
      +20  → 100  (estrella confirmada IN)
    """
    clamped = max(-40.0, min(20.0, float(raw_n)))
    return (clamped + 40) / 60 * 100

def normalize_streak(win_pct: float) -> float:
    return max(0.0, min(100.0, float(win_pct)))

def compute_nea(p_poly: float, p_vegas: float, n: float, v: float, r: float) -> float:
    """
    NEA = P_poly - [(0.45 * P_vegas) + (0.40 * N) + (0.10 * V) + (0.05 * R)]
    Negativo → BUY. Positivo → AVOID.
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
