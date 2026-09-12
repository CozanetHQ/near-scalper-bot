"""
wall_tp.py — dynamic swing-wall TP sizing. Transcribed verbatim from the attached
v6 dossier appendix, for independent verification.
"""
from __future__ import annotations

MIN_TP_DIST_FRAC = 0.006
WALL_LOOKBACK = 40
RUN_CAP_ATR = 3.0


def find_fractal_walls(candles_15m: list[dict], lookback: int = WALL_LOOKBACK):
    window = candles_15m[-lookback:]
    highs, lows = [], []
    for i in range(1, len(window) - 1):
        if window[i]["high"] > window[i - 1]["high"] and window[i]["high"] > window[i + 1]["high"]:
            highs.append(window[i]["high"])
        if window[i]["low"] < window[i - 1]["low"] and window[i]["low"] < window[i + 1]["low"]:
            lows.append(window[i]["low"])
    return highs, lows


def dynamic_tp_distance(entry_price: float, direction: int, atr_1m: float,
                         candles_15m: list[dict]) -> dict:
    base_tp_dist = 1.6 * atr_1m
    highs, lows = find_fractal_walls(candles_15m)
    if direction == 1:
        opposing = [h for h in highs if h > entry_price]
        wall_dist = (min(opposing) - entry_price) if opposing else None
    else:
        opposing = [l for l in lows if l < entry_price]
        wall_dist = (entry_price - max(opposing)) if opposing else None

    if wall_dist is None or base_tp_dist == 0:
        tp_dist = base_tp_dist
        wall_ratio, mode = None, "no_wall_data"
    else:
        wall_ratio = wall_dist / base_tp_dist
        if wall_ratio >= 1.3:
            tp_dist = min(wall_dist * 0.85, RUN_CAP_ATR * atr_1m)
            mode = "runner"
        elif wall_ratio >= 0.7:
            tp_dist = base_tp_dist
            mode = "baseline"
        else:
            tp_dist = base_tp_dist * 0.7
            mode = "shrunk_wall_ahead"

    tp_dist_frac = tp_dist / entry_price
    floor_frac = MIN_TP_DIST_FRAC
    return {
        "tp_dist_frac": max(tp_dist_frac, floor_frac),
        "wall_ratio": wall_ratio,
        "mode": mode,
    }
