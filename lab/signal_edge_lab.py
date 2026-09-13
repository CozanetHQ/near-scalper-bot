"""
signal_edge_lab.py — REPLACEMENT SIGNAL LAB v1 (owner spec 2026-09-13/14).

NOT "which indicator has the highest win rate" — the question is: which market-
state features, measured at entry time, change the MFE/MAE probability
distribution enough to produce positive expectancy after realistic costs?

Universe: every raw baseline signal bar across ALL 5 pairs (no cooldown/slot
suppression — clean conditioning, no concurrency contamination). 30 days of 1m.

Per signal we record 19 features known at entry:
  LOCATION   position in 24h range, VWAP(8h) deviation, distance to 24h high/low (ATR units)
  LIQUIDITY  liquidity sweep below/above 1h extremes, failed breakout, equal-level touches
  STRUCTURE  displacement body, MSS with displacement, compression, TREND/CHOP regime
  VOLATILITY ATR ratio vs 1h, ATR percentile 24h
  MOMENTUM   5-bar impulse (ATR units), 15-bar ROC (ATR units), same-color run length (exhaustion)
  MICROSTR   volume surge vs 1h median, signal-bar wick dominance
  TIME       Asia/London/Overlap/NY/Late bucket

Outcome per signal (forward 240 bars from signal close):
  MFE/MAE (side-adjusted), minutes-to-MFE, and a TP/SL grid expectancy:
  tp ∈ {0.6, 1.0, 1.6} × ATR, sl ∈ {1.0, 1.5, 2.0} × ATR, first-touch wins,
  fees 0.06%/side + 0.01% slip/side. 1.6/2.0 is the near-live combo.

Decision gates for a candidate feature-conditioned entry (locked a priori):
  pooled bootstrap CI lower bound of expectancy > 0 AND n >= 100 AND
  >= 2 of 3 time-folds individually positive.
"""
import json
import numpy as np
import pandas as pd

from ablation import load_pair, compute_indicators, build_15m, compute_15m_regime, EMA_FAST, EMA_SLOW
from multislot_ablation import PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN, ER_LOOKBACK

FEE_COST = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT
FWD = 240
TP_MULTS = (0.6, 1.0, 1.6)
SL_MULTS = (1.0, 1.5, 2.0)


def features_and_signals(pair):
    df = compute_indicators(load_pair(pair))
    df["atr"] = df["atr14"]
    c15 = compute_15m_regime(build_15m(df))
    labels = [None] * len(df)
    for i in range(len(df)):
        ci = (i + 1) // 15 - 1
        if ci >= ER_LOOKBACK and ci < len(c15):
            labels[i] = "TREND" if c15["trend"].iloc[ci] else "CHOP"
    df["regime"] = labels

    # ── location ──
    df["hi24"] = df["high"].rolling(1440, min_periods=60).max()
    df["lo24"] = df["low"].rolling(1440, min_periods=60).min()
    rng = (df["hi24"] - df["lo24"]).replace(0, np.nan)
    df["loc_range_pct"] = ((df["close"] - df["lo24"]) / rng).clip(0, 1)
    tp_vol = (df["close"] * df["vol"]).rolling(480, min_periods=60).sum()
    v_vol = df["vol"].rolling(480, min_periods=60).sum().replace(0, np.nan)
    vwap = tp_vol / v_vol
    df["vwap_dev_atr"] = (df["close"] - vwap) / df["atr"]
    df["dist_hi_atr"] = (df["hi24"] - df["close"]) / df["atr"]
    df["dist_lo_atr"] = (df["close"] - df["lo24"]) / df["atr"]
    # ── liquidity: sweep of the prior 1h extreme, reclaimed same/next bar ──
    ph60 = df["high"].rolling(60).max().shift(1)
    pl60 = df["low"].rolling(60).min().shift(1)
    swept_lo = (df["low"] < pl60) & (df["close"] > pl60)
    swept_hi = (df["high"] > ph60) & (df["close"] < ph60)
    df["sweep_lo"] = swept_lo.rolling(3).max().astype(bool)   # bullish liquidity grab
    df["sweep_hi"] = swept_hi.rolling(3).max().astype(bool)   # bearish
    df["failed_bo"] = ((df["close"] > ph60) & (df["close"].shift(-1) < ph60)) | \
                      ((df["close"] < pl60) & (df["close"].shift(-1) > pl60))
    # equal-level touches: 24h high/low tested >=2x in last 12h (approx by wick proximity)
    near_hi = ((df["high"] - df["hi24"]).abs() < 0.15 * df["atr"]).rolling(720, min_periods=60).sum()
    near_lo = ((df["low"] - df["lo24"]).abs() < 0.15 * df["atr"]).rolling(720, min_periods=60).sum()
    df["eq_touches"] = np.maximum(near_hi, near_lo)
    # ── structure ──
    body = (df["close"] - df["open"]).abs()
    df["displacement"] = body / df["atr"]
    swing_hi = df["high"].rolling(20).max().shift(1)
    swing_lo = df["low"].rolling(20).min().shift(1)
    mss_up = (df["close"] > swing_hi) & (df["displacement"] >= 1.3)
    mss_dn = (df["close"] < swing_lo) & (df["displacement"] >= 1.3)
    df["mss"] = (mss_up | mss_dn).rolling(5).max().astype(bool)
    atr_1h = df["atr"].rolling(60).mean()
    df["atr_ratio"] = df["atr"] / atr_1h
    df["compression"] = df["atr_ratio"] < 0.8
    atr_rank = df["atr"].rolling(1440, min_periods=240).rank(pct=True)
    df["atr_pctile"] = atr_rank
    # ── momentum ──
    df["impulse5_atr"] = (df["close"] - df["close"].shift(5)) / df["atr"]
    df["roc15_atr"] = (df["close"] - df["close"].shift(15)) / df["atr"]
    up = df["close"] > df["open"]
    run = up.groupby((~up).cumsum()).cumcount() + 1
    df["run_len"] = run.where(up, -run)          # signed: + = green run
    # ── microstructure proxies ──
    vol_med = df["vol"].rolling(60).median()
    df["vol_surge"] = df["vol"] / vol_med.replace(0, np.nan)
    rng_bar = (df["high"] - df["low"]).replace(0, np.nan)
    df["wick_bias"] = ((df["high"] - df[["open", "close"]].max(axis=1)) -
                       (df[["open", "close"]].min(axis=1) - df["low"])) / rng_bar  # + = upper wick
    # ── time ──
    hr = df["ts"].dt.hour
    df["session"] = np.select(
        [hr < 7, hr < 12, hr < 16, hr < 21], ["ASIA", "LONDON", "OVERLAP", "NY"], default="LATE")

    # ── the baseline signal (same definition as the live engine) ──
    momentum = np.where(df["ema9"] > df["ema21"], "bull", "bear")
    color = np.where(df["close"] < df["open"], "bear", "bull")
    long_sig = (momentum == "bull") & (color == "bear") & (df["close"] > df["ema21"])
    short_sig = (momentum == "bear") & (color == "bull") & (df["close"] < df["ema21"])
    ok = (df["atr"] > ATR_MIN) & df["atr"].notna() & df["loc_range_pct"].notna()
    side = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    side = np.where(ok, side, 0)

    H, L, C, A = (df["high"].to_numpy(float), df["low"].to_numpy(float),
                  df["close"].to_numpy(float), df["atr"].to_numpy(float))
    n = len(df)
    rows = []
    i_arr = np.where(side != 0)[0]
    for i in i_arr:
        if i < 60 or i + FWD >= n:
            continue
        s = side[i]
        e = C[i]
        fwd_h = H[i + 1:i + 1 + FWD]
        fwd_l = L[i + 1:i + 1 + FWD]
        if s == 1:
            fav = (fwd_h - e) / e
            adv = (e - fwd_l) / e
        else:
            fav = (e - fwd_l) / e
            adv = (fwd_h - e) / e
        mfe = float(fav.max()); mae = float(adv.max())
        t_mfe = int(fav.argmax()) + 1
        row = {"pair": pair, "i": i, "side": "long" if s == 1 else "short",
               "atr_frac": A[i] / e, "mfe": mfe, "mae": mae, "t_mfe": t_mfe,
               "loc_range_pct": df["loc_range_pct"].iloc[i], "vwap_dev_atr": df["vwap_dev_atr"].iloc[i],
               "dist_hi_atr": df["dist_hi_atr"].iloc[i], "dist_lo_atr": df["dist_lo_atr"].iloc[i],
               "sweep_lo": bool(df["sweep_lo"].iloc[i]), "sweep_hi": bool(df["sweep_hi"].iloc[i]),
               "failed_bo": bool(df["failed_bo"].iloc[i]), "eq_touches": df["eq_touches"].iloc[i],
               "displacement": df["displacement"].iloc[i], "mss": bool(df["mss"].iloc[i]),
               "compression": bool(df["compression"].iloc[i]), "atr_ratio": df["atr_ratio"].iloc[i],
               "atr_pctile": df["atr_pctile"].iloc[i], "impulse5_atr": df["impulse5_atr"].iloc[i],
               "roc15_atr": df["roc15_atr"].iloc[i], "run_len": df["run_len"].iloc[i],
               "vol_surge": df["vol_surge"].iloc[i], "wick_bias": df["wick_bias"].iloc[i],
               "session": df["session"].iloc[i], "regime": df["regime"].iloc[i]}
        # TP/SL grid: first touch wins, else exit at window close
        tpv = (fwd_h if s == 1 else fwd_l)
        slv = (fwd_l if s == 1 else fwd_h)
        fav_arr, adv_arr = (fav, adv)
        for tm in TP_MULTS:
            for sm in SL_MULTS:
                tp_f, sl_f = tm * row["atr_frac"], sm * row["atr_frac"]
                hit = np.where(fav_arr >= tp_f)[0]
                stop = np.where(adv_arr >= sl_f)[0]
                h1 = hit[0] if len(hit) else 10**9
                s1 = stop[0] if len(stop) else 10**9
                if h1 < s1:
                    out = tp_f - FEE_COST
                elif s1 < h1:
                    out = -sl_f - FEE_COST
                else:
                    out = fav_arr[-1] - FEE_COST if s == 1 else (fwd_l[-1] - e) / e * -1 * -1
                    out = ((fwd_h[-1] if s == 1 else fwd_l[-1]) - e) / e * (1 if s == 1 else -1) - FEE_COST
                row[f"exp_t{tm}_s{sm}"] = float(out)
        rows.append(row)
    return rows


def boot_lo(x, n_boot=5000, seed=17):
    if len(x) < 10:
        return float("nan")
    rng = np.random.default_rng(seed)
    m = rng.choice(x, (n_boot, len(x))).mean(axis=1)
    return float(np.percentile(m, 5))   # one-sided lower bound


if __name__ == "__main__":
    all_rows = []
    for p in PAIRS:
        r = features_and_signals(p)
        print(f"{p}: {len(r)} raw signals", flush=True)
        all_rows += r
    df = pd.DataFrame(all_rows)
    df.to_csv("signal_edge_universe.csv", index=False)
    print(f"TOTAL universe: {len(df)} signals across {df['pair'].nunique()} pairs", flush=True)
    json.dump({"n": len(df), "pairs": df["pair"].unique().tolist()},
              open("signal_edge_meta.json", "w"))
    print("saved signal_edge_universe.csv — analysis next", flush=True)
