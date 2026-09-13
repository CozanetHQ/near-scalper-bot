"""
mss_compression_lab.py — premises #3 and #4 of the replacement-signal tree
(owner tree, 2026-09-13; audit §21).

#3 MSS-DISPLACEMENT CONTINUATION
  Event: bar with body >= 1.3xATR14 that CLOSES beyond the prior 20-bar swing
  extreme (close > rolling-20 high -> up-MSS; close < rolling-20 low -> down-MSS).
  M1: enter CONTINUATION at the MSS bar close.
  M2: enter at the first RETEST within 15 bars (price touches the broken level
      again; entry = retest bar close).
  (REVERSAL direction also recorded for completeness — against the break.)

#4 COMPRESSION BREAKOUT
  Compression: ATR14/ATR1h < 0.8 AND 30-bar range < 2xATR14.
  Event: first close beyond the prior 30-bar extreme within 60 bars of
  compression onset; enter BREAKOUT direction at that close.
  (Reversal recorded for completeness.)

Same methodology as §19/§20: 240-bar forward, TP/SL grid {0.6,1.0,1.6}xATR x
{1.0,1.5,2.0}xATR first-touch, window-close censor, 0.14% RT taker costs,
run-collapse >=60 bars per (pair,premise,variant,dir), folds = calendar thirds.
Gates: CI_lo>0, n>=300, >=4/5 pairs+, >=2/3 folds+, freq>=10/pair/month,
drop-max-pair exp>0. Maker economics reported as auxiliary only.
"""
import numpy as np
import pandas as pd

from ablation import load_pair, compute_indicators
from multislot_ablation import PAIRS, ATR_MIN

FEE_COST = 0.0014
FWD = 240
TPS = (0.6, 1.0, 1.6)
SLS = (1.0, 1.5, 2.0)
T0 = pd.Timestamp("2026-08-12 12:13:00+00:00")


def outcome_row(pair, i, direction, C, H, L, A, n):
    e = C[i]
    if i + 1 + FWD >= n or np.isnan(A[i]) or A[i] < ATR_MIN * e:
        return None
    atr_frac = A[i] / e
    if direction == 1:
        fav = (H[i + 1:i + 1 + FWD] - e) / e
        adv = (e - L[i + 1:i + 1 + FWD]) / e
    else:
        fav = (e - L[i + 1:i + 1 + FWD]) / e
        adv = (H[i + 1:i + 1 + FWD] - e) / e
    if len(fav) < FWD:
        return None
    row = {"pair": pair, "i": i, "dir": direction, "atr_frac": atr_frac,
           "mfe": float(fav.max()), "mae": float(adv.max()),
           "ret30": (C[min(i + 30, n - 1)] - e) / e * direction,
           "ret240": (C[min(i + 240, n - 1)] - e) / e * direction}
    for tm in TPS:
        for sm in SLS:
            tp_f, sl_f = tm * atr_frac, sm * atr_frac
            hit = np.where(fav >= tp_f)[0]
            stop = np.where(adv >= sl_f)[0]
            h1 = hit[0] if len(hit) else 10 ** 9
            s1 = stop[0] if len(stop) else 10 ** 9
            o = (tp_f - FEE_COST) if h1 < s1 else ((-sl_f - FEE_COST) if s1 < h1 else fav[-1] - FEE_COST)
            row[f"t{tm}_s{sm}"] = float(o)
    return row


def pair_events(pair):
    df = compute_indicators(load_pair(pair))
    H = df["high"].to_numpy(float); L = df["low"].to_numpy(float)
    C = df["close"].to_numpy(float); O = df["open"].to_numpy(float)
    A = df["atr14"].to_numpy(float)
    n = len(df)
    sw_hi = df["high"].rolling(20).max().shift(1).to_numpy(float)
    sw_lo = df["low"].rolling(20).min().shift(1).to_numpy(float)
    hi30 = df["high"].rolling(30).max().shift(1).to_numpy(float)
    lo30 = df["low"].rolling(30).min().shift(1).to_numpy(float)
    atr1h = df["atr14"].rolling(60).mean().to_numpy(float)
    rng30 = (hi30 - lo30)
    events = []

    def emit(premise, variant, i, direction):
        r = outcome_row(pair, i, direction, C, H, L, A, n)
        if r:
            r.update({"premise": premise, "variant": variant})
            events.append(r)

    for i in range(60, n - FWD - 70):
        if np.isnan(A[i]) or np.isnan(sw_hi[i]):
            continue
        body = abs(C[i] - O[i])
        # ── #3 MSS displacement ──
        if body >= 1.3 * A[i]:
            if C[i] > sw_hi[i]:
                emit("MSS", "M1_break_close", i, +1)
                emit("MSS", "M1_break_close", i, -1)      # reversal, recorded
                # M2 retest: first touch back to sw_hi within 15 bars
                for j in range(i + 1, min(i + 16, n - FWD - 5)):
                    if L[j] <= sw_hi[i]:
                        emit("MSS", "M2_retest", j, +1)
                        emit("MSS", "M2_retest", j, -1)
                        break
            elif C[i] < sw_lo[i]:
                emit("MSS", "M1_break_close", i, -1)
                emit("MSS", "M1_break_close", i, +1)
                for j in range(i + 1, min(i + 16, n - FWD - 5)):
                    if H[j] >= sw_lo[i]:
                        emit("MSS", "M2_retest", j, -1)
                        emit("MSS", "M2_retest", j, +1)
                        break
        # ── #4 compression breakout ──
        if (not np.isnan(atr1h[i]) and atr1h[i] > 0 and A[i] / atr1h[i] < 0.8
                and not np.isnan(rng30[i]) and rng30[i] < 2.0 * A[i]):
            for j in range(i + 1, min(i + 61, n - FWD - 5)):
                if np.isnan(hi30[j]) or np.isnan(lo30[j]):
                    break
                if C[j] > hi30[j]:
                    emit("COMP", "breakout", j, +1)
                    emit("COMP", "breakout", j, -1)
                    break
                if C[j] < lo30[j]:
                    emit("COMP", "breakout", j, -1)
                    emit("COMP", "breakout", j, +1)
                    break
    return events


if __name__ == "__main__":
    rows = []
    for p in PAIRS:
        r = pair_events(p)
        rows += r
        print(f"{p}: {len(r)} event-records", flush=True)
    df = pd.DataFrame(rows)
    df["ts"] = T0 + pd.to_timedelta(df["i"], unit="m")
    df.to_csv("mss_comp_raw.csv", index=False)
    print(f"TOTAL: {len(df)} event-records; saved mss_comp_raw.csv", flush=True)
