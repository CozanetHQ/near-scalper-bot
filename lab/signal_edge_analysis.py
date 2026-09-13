"""
signal_edge_analysis.py — conditional edge analysis on the 66k-signal universe.

Q: which market-state features at entry change MFE/MAE distributions enough to
produce positive expectancy after fees?

Method:
- Run-collapse: consecutive same-side signal bars are one event (keep first,
  then require >=60-bar gap) — overlapping 1m signals would inflate n and
  shrink CIs dishonestly.
- Headline expectancy: TP=1.6xATR / SL=2.0xATR first-touch, minus 0.14% RT
  costs (taker) — near-live combo. Grid {0.6,1.0,1.6}x{1.0,1.5,2.0} also stored.
- Per feature: bucket -> n, p_tp, expectancy, MFE/MAE means, bootstrap CI lo.
- Candidate gate rule (locked a priori): pooled CI_lo > 0 AND n >= 100 AND
  >=2 of 3 time-folds individually positive.
"""
import json
import numpy as np
import pandas as pd

FEE_COST = 0.0006 * 2 + 0.0001 * 2
FOLDS = {"f1": ("2026-08-12", "2026-08-22"), "f2": ("2026-08-22", "2026-09-01"),
         "f3": ("2026-09-01", "2026-09-12")}

df = pd.read_csv("signal_edge_universe.csv")
# reconstruct timestamps for folds: row has pair + i (bar index); 1m bars from
# 2026-08-12 12:13 UTC — recompute from i offset per pair (frames start same T0)
T0 = pd.Timestamp("2026-08-12 12:13:00+00:00")
df["ts"] = T0 + pd.to_timedelta(df["i"], unit="m")
df = df.sort_values(["pair", "i"]).reset_index(drop=True)

# run-collapse: per pair+side keep signals >=60 bars after the last kept one
keep = []
last = {}
for _, r in df.iterrows():
    k = (r["pair"], r["side"])
    if k not in last or r["i"] - last[k] >= 60:
        keep.append(True)
        last[k] = r["i"]
    else:
        keep.append(False)
df = df[keep].reset_index(drop=True)
print(f"after run-collapse (>=60-bar gap): {len(df)} independent signal events")
for p, g in df.groupby("pair"):
    print(f"  {p}: {len(g)}")

EXP = "exp_t1.6_s2.0"
base_exp = df[EXP].mean()
print(f"\nUNCONDITIONAL expectancy (all signals, tp1.6/sl2.0, taker costs): {base_exp*100:+.3f}%/trade  n={len(df)}")

def boot_lo(x, n_boot=5000, seed=17):
    if len(x) < 10:
        return float("nan")
    rng = np.random.default_rng(seed)
    m = rng.choice(x, (n_boot, len(x))).mean(axis=1)
    return float(np.percentile(m, 5))

def fold_pos(sub):
    pos = 0
    for k, (a, b) in FOLDS.items():
        s = sub[(sub["ts"] >= a) & (sub["ts"] < b)]
        if len(s) >= 20 and s[EXP].mean() > 0:
            pos += 1
    return pos

def table(sub, by, label):
    print(f"\n=== {label} ===")
    print(f"{'bucket':>24s} {'n':>5s} {'p_tp':>6s} {'exp%':>7s} {'CIlo%':>7s} {'mfe%':>6s} {'mae%':>6s} {'folds':>5s}")
    rows = []
    for b, g in sub.groupby(by, dropna=False):
        if len(g) < 40:
            continue
        e = g[EXP].mean()
        rows.append((e, str(b), g))
    for e, b, g in sorted(rows, key=lambda x: -x[0]):
        ci = boot_lo(g[EXP].to_numpy())
        ptp = (g[EXP] > 0).mean()
        print(f"{b:>24s} {len(g):5d} {ptp*100:5.0f}% {e*100:+7.3f} {ci*100:+7.3f} "
              f"{g['mfe'].mean()*100:6.2f} {g['mae'].mean()*100:6.2f} {fold_pos(g)}/3")

# ── per-feature conditional tables ──
df["loc_q"] = pd.qcut(df["loc_range_pct"], 4, labels=["Q1 range-lo", "Q2", "Q3", "Q4 range-hi"])
df["vwap_q"] = pd.qcut(df["vwap_dev_atr"], 4, labels=["deep disc", "disc", "prem", "deep prem"])
df["disp_q"] = pd.qcut(df["displacement"], 3, labels=["small body", "mid", "large body"])
df["imp_q"] = pd.qcut(df["impulse5_atr"], 4, labels=["rev5 strong", "flat", "with-trend", "with-trend strong"])
df["vs_q"] = pd.qcut(df["vol_surge"], 3, labels=["low vol", "mid vol", "vol surge"])
df["wick_q"] = pd.qcut(df["wick_bias"], 3, labels=["lower wick", "neutral", "upper wick"])
df["disthi_q"] = pd.qcut(df["dist_hi_atr"], 4, labels=["AT high", "near hi", "mid", "far from hi"])

table(df, "session", "TIME — session buckets")
table(df, "regime", "STRUCTURE — 15m regime")
table(df, "loc_q", "LOCATION — position in 24h range")
table(df, "vwap_q", "LOCATION — 8h VWAP deviation (ATR units)")
table(df, "sweep_lo", "LIQUIDITY — bullish sweep of 1h low present")
table(df, "sweep_hi", "LIQUIDITY — bearish sweep of 1h high present")
table(df, "failed_bo", "LIQUIDITY — failed breakout present")
table(df, "mss", "STRUCTURE — MSS with displacement in last 5 bars")
table(df, "compression", "VOLATILITY — ATR compression (atr/atr1h < 0.8)")
table(df, "disp_q", "STRUCTURE — signal-bar displacement")
table(df, "imp_q", "MOMENTUM — 5-bar impulse vs trend")
table(df, "roc15_atr", "MOMENTUM — 15-bar ROC (ATR units)")
table(df, "run_len", "MOMENTUM — same-color run length at signal (exhaustion)")
table(df, "vs_q", "MICROSTRUCTURE — volume vs 1h median")
table(df, "wick_q", "MICROSTRUCTURE — wick bias")
table(df, "atr_pctile", "VOLATILITY — ATR 24h percentile")
table(df, "side", "SIDE")
table(df, "pair", "PAIR")

# ── best-of-grid check per strong feature: does ANY tp/sl combo clear CI>0? ──
print("\n=== grid scan on the strongest buckets (all 9 tp/sl combos, CI lo) ===")
strong = [
    ("sweep_lo == True", df[df["sweep_lo"]]),
    ("sweep_hi == True", df[df["sweep_hi"]]),
    ("mss == True", df[df["mss"]]),
    ("session == OVERLAP", df[df["session"] == "OVERLAP"]),
    ("regime == TREND", df[df["regime"] == "TREND"]),
]
for lbl, sub in strong:
    best = None
    for tm in ("0.6", "1.0", "1.6"):
        for sm in ("1.0", "1.5", "2.0"):
            col = f"exp_t{tm}_s{sm}"
            e = sub[col].mean()
            ci = boot_lo(sub[col].to_numpy())
            if best is None or ci > best[0]:
                best = (ci, col, e, len(sub))
    ci, col, e, n = best
    print(f"{lbl:26s} n={n:5d} best={col:14s} exp={e*100:+.3f}% CIlo={ci*100:+.3f}% "
          f"{'CANDIDATE' if ci > 0 and n >= 100 else 'no'}")

# ── 2-way interactions among promising features ──
print("\n=== 2-way interactions ===")
combos = [("session", "regime"), ("sweep_lo", "session"), ("sweep_hi", "session"),
          ("mss", "regime"), ("mss", "session"), ("loc_q", "session"),
          ("imp_q", "regime"), ("compression", "session"), ("vs_q", "regime")]
for a, b in combos:
    for (va, vb), g in df.groupby([a, b], dropna=False):
        if len(g) < 100:
            continue
        e = g[EXP].mean()
        if e > base_exp * 3 and e > 0.0002:
            ci = boot_lo(g[EXP].to_numpy())
            print(f"{a}={va} & {b}={vb}: n={len(g)} exp={e*100:+.3f}% CIlo={ci*100:+.3f}% folds={fold_pos(g)}/3")

# ── per-pair: the same features, per pair (owner: 'this is for every pair') ──
print("\n=== per-pair strongest feature check ===")
for p, g in df.groupby("pair"):
    best = (None, -9, 0, None)
    for f in ("session", "regime", "sweep_lo", "mss", "compression", "side", "loc_q", "vs_q", "wick_q"):
        for b, gg in g.groupby(f, dropna=False):
            if len(gg) < 60:
                continue
            e = gg[EXP].mean()
            if e > best[1]:
                best = (f"{f}={b}", e, len(gg), boot_lo(gg[EXP].to_numpy()))
    print(f"{p}: best bucket {best[0]} n={best[2]} exp={best[1]*100:+.3f}% CIlo={best[3]*100:+.3f}%")

df.to_csv("signal_edge_events.csv", index=False)
print("\nsaved signal_edge_events.csv (run-collapsed universe with features + outcomes)")
