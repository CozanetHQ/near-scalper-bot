"""
kelly.py — fractional-Kelly sizing. Transcribed verbatim from the attached
v6 dossier appendix, for independent verification.
"""
from __future__ import annotations

KELLY_FRACTION = 0.35
KELLY_MULT_MIN = 0.25
KELLY_MULT_MAX = 1.50
MIN_TRADES_FOR_KELLY = 30


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    if avg_loss <= 0 or win_rate <= 0:
        return 0.0
    b = avg_win / avg_loss
    p = win_rate
    q = 1 - p
    f_star = p - q / b
    return max(0.0, f_star)


def size_multiplier(bucket_trades: int, win_rate: float, avg_win: float,
                     avg_loss: float) -> dict:
    if bucket_trades < MIN_TRADES_FOR_KELLY:
        return {"multiplier": 1.0, "f_star": None, "reason": "insufficient_sample"}
    f_star = kelly_fraction(win_rate, avg_win, avg_loss)
    fractional = f_star * KELLY_FRACTION
    flat_ref = 0.85 / 8
    if flat_ref == 0:
        return {"multiplier": 1.0, "f_star": f_star, "reason": "flat_ref_zero"}
    raw_mult = fractional / flat_ref
    clamped = max(KELLY_MULT_MIN, min(KELLY_MULT_MAX, raw_mult))
    return {"multiplier": clamped, "f_star": f_star, "reason": "kelly_scaled"}
