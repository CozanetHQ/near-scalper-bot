"""Feature Engine — market-math primitives from candle series.

Pure functions only: candle classification, RSI/EMA/ATR indicators, fractal
swing detection (the math_spec §4 swing definitions at 15m granularity).
"""

def candle_color(c):
    return "bull" if c["close"] >= c["open"] else "bear"


def compute_ema(closes, period):
    """Standard EMA over a list of closes (oldest -> newest)."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def compute_atr(candles, period):
    """Simple ATR on CLOSED candles (oldest -> newest). Needs period+1 candles."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    return sum(trs[-period:]) / period


def fractal_swings(candles, wing=2):
    """Fractal swing highs/lows: a level must dominate the `wing` candles on
    each side. Returns (highs, lows) of confirmed swing levels, oldest->newest.
    math_spec §4's swing_high/swing_low at any granularity."""
    if len(candles) < 2 * wing + 1:
        return [], []
    highs, lows = [], []
    for j in range(wing, len(candles) - wing):
        h, l = candles[j]["high"], candles[j]["low"]
        if all(h > candles[j - k]["high"] for k in range(1, wing + 1)) and \
           all(h > candles[j + k]["high"] for k in range(1, wing + 1)):
            highs.append(h)
        if all(l < candles[j - k]["low"] for k in range(1, wing + 1)) and \
           all(l < candles[j + k]["low"] for k in range(1, wing + 1)):
            lows.append(l)
    return highs, lows
