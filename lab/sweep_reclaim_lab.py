"""
sweep_reclaim_lab.py — SWEEP-RECLAIM premise test (owner spec 2026-09-13, audit §20).

Research tree: EMA-pullback FAILED (§19) -> this is the next premise. Protection
70/55 stays frozen and untouched.

Liquidity levels: prior 60-bar extremes (1h on 1m data).
- SELL-SIDE sweep: low[i] < min(low[i-60..i-1])   (stops under the low get hit)
- BUY-SIDE sweep:  high[i] > max(high[i-60..i-1]) (stops above the high get hit)

Variants (independent event sets — later confirmations do NOT replace earlier
definitions; all five are reported side by side so selectivity creep is visible):
  A  sweep only                  event = breach bar close
  B  sweep + same-bar reclaim    close[i] back inside the level
  C  sweep + 2-bar reclaim        first close back inside within 2 bars
  D  C + displacement confirm     a bar within 3 bars after reclaim with body
                                  >= 1.3xATR in reclaim direction; event = that bar
  E  C + MSS confirm              close beyond the opposite 20-bar swing within
                                  5 bars after reclaim; event = that bar

Direction hypotheses, tested independently for every variant:
  REVERSAL: sell-side swept -> LONG, buy-side swept -> SHORT (order-flow reversal)
  CONTINUATION: sell-side swept -> SHORT, buy-side swept -> LONG (breakdown runs)

Outcomes (identical to §19 for comparability): entry = event-bar close; forward
240 bars; TP/SL grid tp in {0.6,1.0,1.6}xATR14 x sl in {1.0,1.5,2.0}xATR14,
first touch wins, else window-close exit; taker costs 0.06%+0.01% slip per side
(0.14% RT) on every event. Maker auxiliary = same + 0.08% RT rebate adjustment.
Also recorded: signed 30/60/240-bar returns (does price actually travel?).

Acceptance gate (locked a priori, per variant x hypothesis):
  pooled bootstrap 95% CI lo > 0 on some grid combo, n_collapsed >= 300 pooled,
  point exp > 0 in >= 4/5 pairs on that combo, >= 2/3 folds positive,
  freq >= ~10 events/pair/month, no single pair responsible (drop-max-pair exp
  still > 0). Live-stack PF>1 simulation only for survivors (stage 2).
"""
import json
import numpy as np
import pandas as pd

from ablation import load_pair, compute_indicators
from multislot_ablation import PAIRS, ATR_MIN

FEE_COST = 0.0014          # taker RT
MAKER_ADJ = 0.0008         # maker RT would be ~0.06%: add back 0.08%
FWD = 240
TPS = (0.6, 1.0, 1.6)
SLS = (1.0, 1.5, 2.0)
T0 = pd.Timestamp("2026-08-12 12:13:00+00:00")
CUTS = (pd.Timestamp("2026-08-22 12:13:00+00:00"),
        pd.Timestamp("2026-09-01 12:13:00+00:00"))


def grid_outcomes(e, fwd_h, fwd_l, fav, adv, atr_frac):
    out = {}
    for tm in TPS:
        for sm in SLS:
            tp_f, sl_f = tm * atr_frac, sm * atr_frac
            hit = np.where(fav >= tp_f)[0]
            stop = np.where(adv >= sl_f)[0]
            h1 = hit[0] if len(hit) else 10 ** 9
            s1 = stop[0] if len(stop) else 10 ** 9
            if h1 < s1:
                o = tp_f - FEE_COST
            elif s1 < h1:
                o = -sl_f - FEE_COST
            else:
                o = fav[-1] - FEE_COST
            out[f"t{tm}_s{sm}"] = float(o)
    return out


def pair_events(pair):
    df = compute_indicators(load_pair(pair))
    H = df["high"].to_numpy(float)
    L = df["low"].to_numpy(float)
    C = df["close"].to_numpy(float)
    O = df["open"].to_numpy(float)
    A = df["atr14"].to_numpy(float)
    lo60 = df["low"].rolling(60).min().shift(1).to_numpy(float)
    hi60 = df["high"].rolling(60).max().shift(1).to_numpy(float)
    sw_hi20 = df["high"].rolling(20).max().shift(1).to_numpy(float)  # swing proxy
    sw_lo20 = df["low"].rolling(20).min().shift(1).to_numpy(float)
    n = len(df)
    events = []

    def emit(variant, sweep, i, hyp_dir, note=""):
        # hyp_dir: +1 long, -1 short — the hypothesis direction for this event
        e = C[i]
        if i + 1 + FWD >= n or np.isnan(A[i]) or A[i] < ATR_MIN * e:
            return
        atr_frac = A[i] / e
        if hyp_dir == 1:
            fav = (H[i + 1:i + 1 + FWD] - e) / e
            adv = (e - L[i + 1:i + 1 + FWD]) / e
        else:
            fav = (e - L[i + 1:i + 1 + FWD]) / e
            adv = (H[i + 1:i + 1 + FWD] - e) / e
        if len(fav) < FWD:
            return
        row = {"pair": pair, "i": i, "variant": variant, "sweep": sweep,
               "dir": hyp_dir, "atr_frac": atr_frac,
               "mfe": float(fav.max()), "mae": float(adv.max()),
               "ret30": (C[min(i + 30, n - 1)] - e) / e * hyp_dir,
               "ret60": (C[min(i + 60, n - 1)] - e) / e * hyp_dir,
               "ret240": (C[min(i + 240, n - 1)] - e) / e * hyp_dir}
        row.update(grid_outcomes(e, H, L, fav, adv, atr_frac))
        events.append(row)

    for i in range(60, n - FWD - 6):
        if np.isnan(lo60[i]) or np.isnan(A[i]):
            continue
        # ── sell-side sweep: low pierces prior 60-bar low ──
        if L[i] < lo60[i]:
            emit("A_sweeponly", "sell", i, +1)          # reversal: long
            emit("A_sweeponly", "sell", i, -1)          # continuation: short
            if C[i] > lo60[i]:                            # same-bar reclaim
                emit("B_samebar", "sell", i, +1)
                emit("B_samebar", "sell", i, -1)
                r = i
                rc = C[i]
            elif i + 1 < n and C[i + 1] > lo60[i]:        # 2-bar reclaim
                r, rc = i + 1, C[i + 1]
            else:
                r = None
            if r is not None:
                emit("C_2bar", "sell", r, +1)
                emit("C_2bar", "sell", r, -1)
                # D: displacement confirmation within 3 bars after reclaim
                for j in range(r + 1, min(r + 4, n - FWD - 6)):
                    body = abs(C[j] - O[j])
                    if body >= 1.3 * A[j]:
                        d = 1 if C[j] > O[j] else -1
                        emit("D_displ", "sell", j, d)    # trade the displacement direction
                        break
                # E: MSS confirmation within 5 bars after reclaim
                for j in range(r + 1, min(r + 6, n - FWD - 6)):
                    if C[j] > sw_hi20[j]:                 # structure broken upward
                        emit("E_mss", "sell", j, +1)
                        break
                    if C[j] < sw_lo20[j]:
                        emit("E_mss", "sell", j, -1)
                        break
        # ── buy-side sweep: high pierces prior 60-bar high ──
        if H[i] > hi60[i]:
            emit("A_sweeponly", "buy", i, -1)           # reversal: short
            emit("A_sweeponly", "buy", i, +1)           # continuation: long
            if C[i] < hi60[i]:                            # same-bar reclaim (back inside)
                emit("B_samebar", "buy", i, -1)
                emit("B_samebar", "buy", i, +1)
                r = i
            elif i + 1 < n and C[i + 1] < hi60[i]:
                r = i + 1
            else:
                r = None
            if r is not None:
                emit("C_2bar", "buy", r, -1)
                emit("C_2bar", "buy", r, +1)
                for j in range(r + 1, min(r + 4, n - FWD - 6)):
                    body = abs(C[j] - O[j])
                    if body >= 1.3 * A[j]:
                        d = 1 if C[j] > O[j] else -1
                        emit("D_displ", "buy", j, d)
                        break
                for j in range(r + 1, min(r + 6, n - FWD - 6)):
                    if C[j] < sw_lo20[j]:
                        emit("E_mss", "buy", j, -1)
                        break
                    if C[j] > sw_hi20[j]:
                        emit("E_mss", "buy", j, +1)
                        break
    return events


if __name__ == "__main__":
    rows = []
    for p in PAIRS:
        r = pair_events(p)
        rows += r
        print(f"{p}: {len(r)} event-records (all variants/directions)", flush=True)
    df = pd.DataFrame(rows)
    df["ts"] = T0 + pd.to_timedelta(df["i"], unit="m")
    df["hyp"] = np.where(
        ((df["sweep"] == "sell") & (df["dir"] == 1)) | ((df["sweep"] == "buy") & (df["dir"] == -1)),
        "REVERSAL", "CONTINUATION")
    df.to_csv("sweep_reclaim_raw.csv", index=False)
    print(f"TOTAL event-records: {len(df)}; saved sweep_reclaim_raw.csv", flush=True)
