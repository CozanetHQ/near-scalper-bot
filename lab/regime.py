"""
regime.py — 3-state regime classifier: TREND / CHOP / SPIKE
Transcribed verbatim from the attached v6 dossier appendix, for independent verification.
"""
from __future__ import annotations
import math

SPIKE_RANGE_MULT = 3.0
ER_TREND_THRESHOLD = 0.38
ER_LOOKBACK = 20


def efficiency_ratio(closes: list[float]) -> float:
    if len(closes) < 3:
        return 0.0
    net_move = abs(closes[-1] - closes[0])
    path_len = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path_len == 0:
        return 0.0
    return net_move / path_len


def atr(candles: list[dict], n: int) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(max(1, len(candles) - n), len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def classify_regime(candles_15m: list[dict], forming_1m: dict, atr_1m: float) -> dict:
    forming_range = forming_1m["high"] - forming_1m["low"]
    if atr_1m > 0 and forming_range > SPIKE_RANGE_MULT * atr_1m:
        return {"regime": "SPIKE", "er": None, "direction": 0}
    window = candles_15m[-(ER_LOOKBACK + 1):]
    closes = [c["close"] for c in window]
    er = efficiency_ratio(closes)
    direction = 1 if closes[-1] > closes[0] else (-1 if closes[-1] < closes[0] else 0)
    if er >= ER_TREND_THRESHOLD:
        return {"regime": "TREND", "er": er, "direction": direction}
    return {"regime": "CHOP", "er": er, "direction": direction}
