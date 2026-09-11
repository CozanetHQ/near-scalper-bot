"""Second Engine — advisory scoring, v1 (research/second_engine_spec.md).

Pure functions only. NOT wired into the live pipeline. Every formula here
is a candidate claim to be validated by lab_suite.py before any threshold
is locked into param_registry.json. See the spec doc for full derivations
and the honest disclosure that the "Joint Math Spec" the owner's Lab Suite
doc assumed did not exist prior to this file.
"""

from engine.features import fractal_swings


def structure_quality(closed15m, side, wing=2, lookback=40):
    cs = closed15m[-lookback:]
    hi, lo = fractal_swings(cs, wing=wing)
    if len(hi) < 2 or len(lo) < 2:
        return 0.00
    same, counter = (hi, lo) if side == "long" else (lo, hi)
    # BOS: latest same-direction swing exceeds the prior one
    bos = (same[-1] > same[-2]) if side == "long" else (same[-1] < same[-2])
    if not bos:
        # check CHoCH/MSS even without a fresh same-direction BOS
        pass
    price = cs[-1]["close"]
    # CHoCH: price closed beyond the last counter-direction swing
    choch = (price > counter[-1]) if side == "long" else (price < counter[-1])
    mss = choch and len(counter) >= 2 and (
        (price > counter[-2]) if side == "long" else (price < counter[-2])
    )
    staircase = bos and len(same) >= 3 and (
        (same[-2] > same[-3]) if side == "long" else (same[-2] < same[-3])
    )
    if mss:
        return 1.00
    if choch:
        return 0.70
    if staircase:
        return 0.40
    if bos:
        return 0.15
    return 0.00


def spike_risk(forming_range, atr):
    if not atr or atr <= 0:
        return {"score": 0.0, "bucket": "low"}
    score = max(0.0, min(1.0, (forming_range / atr) / 3.0))
    bucket = "high" if score >= 1.0 else ("medium" if score >= 0.33 else "low")
    return {"score": round(score, 4), "bucket": bucket}


def dist_to_wall(closed15m, side, price, wing=2, lookback=40):
    cs = closed15m[-lookback:]
    hi, lo = fractal_swings(cs, wing=wing)
    if side == "long":
        above = [h for h in hi if h > price]
        return (min(above) - price) if above else None
    below = [l for l in lo if l < price]
    return (price - max(below)) if below else None


def move_potential(closed15m, side, price, tp_dist, spike_bucket, wing=2, lookback=40):
    wall_dist = dist_to_wall(closed15m, side, price, wing=wing, lookback=lookback)
    if wall_dist is None:
        score, ratio = 0.90, None
    else:
        ratio = wall_dist / tp_dist if tp_dist > 0 else None
        if ratio is None:
            score = 0.40
        elif ratio >= 1.3:
            score = 0.90
        elif ratio >= 1.0:
            score = 0.65
        elif ratio >= 0.7:
            score = 0.40
        else:
            score = 0.15
    if spike_bucket == "high":
        score = min(score, 0.20)
    return {"score": round(score, 4), "ratio": ratio}


def overall_score(sq, mp_score, spike_score):
    v = 0.4 * (2 * sq - 1) + 0.4 * (2 * mp_score - 1) + 0.2 * (1 - 2 * spike_score)
    return round(max(-1.0, min(1.0, v)), 4)


def score_signal(closed15m, side, price, atr, tp_dist, forming_range):
    sq = structure_quality(closed15m, side)
    sr = spike_risk(forming_range, atr)
    mp = move_potential(closed15m, side, price, tp_dist, sr["bucket"])
    osc = overall_score(sq, mp["score"], sr["score"])
    return {
        "structure_quality": sq,
        "spike_risk_score": sr["score"], "spike_bucket": sr["bucket"],
        "move_potential": mp["score"], "wall_ratio": mp["ratio"],
        "overall_score": osc,
    }
