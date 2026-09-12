"""
backtest_v6.py — walk-forward comparison. Transcribed verbatim from the attached
v6 dossier appendix, for independent verification. Run from lab_verify/ with the
data_<PAIR>_30d.json files present in the working directory (symlinked in).
"""
from __future__ import annotations
import json, math
import numpy as np
import pandas as pd

from regime import efficiency_ratio, ER_TREND_THRESHOLD, ER_LOOKBACK, SPIKE_RANGE_MULT
from wall_tp import dynamic_tp_distance, MIN_TP_DIST_FRAC
from kelly import size_multiplier

FEE_RATE = 0.0006
SLIP_ASSUMED_PCT = 0.0001
ATR_PERIOD = 14
ATR_MIN = 0.0008
EMA_FAST, EMA_SLOW = 9, 21
HARD_SL_FRAC = 0.06
BE_TRIGGER_FRAC = 0.5
MAX_POS_AGE_BARS = 48 * 60
MAE_WALL = {"NEARUSDT": 0.025}
MAE_WALL_DEFAULT = 0.04

PAIRS = ["NEARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def load_pair(pair):
    with open(f"data_{pair}_30d.json") as f:
        raw = json.load(f)
    df = pd.DataFrame(raw)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def compute_indicators(df):
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(ATR_PERIOD).mean()
    df["color"] = np.where(df["close"] >= df["open"], "bull", "bear")
    return df


def build_15m(df):
    grp = df.groupby(df.index // 15)
    c15 = grp.agg(open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last"),
                  ts=("ts", "first")).reset_index(drop=True)
    return c15


def compute_15m_regime(c15):
    closes = c15["close"].tolist()
    er = [None] * len(closes)
    for i in range(ER_LOOKBACK, len(closes)):
        window = closes[i - ER_LOOKBACK:i + 1]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[j] - window[j - 1]) for j in range(1, len(window)))
        er[i] = (net / path) if path > 0 else 0.0
    c15 = c15.copy()
    c15["er"] = er
    c15["trend"] = c15["er"].apply(lambda x: (x is not None) and x >= ER_TREND_THRESHOLD)
    return c15


def simulate_pair(pair, df, c15, gate_chop: bool):
    df = compute_indicators(df)
    n = len(df)
    closed15_count = [(i + 1) // 15 for i in range(n)]
    c15_records = c15.to_dict("records")
    mae_wall = MAE_WALL.get(pair, MAE_WALL_DEFAULT)

    trades = []
    i = 30
    while i < n - 2:
        row = df.iloc[i]
        atr = row["atr14"]
        if pd.isna(atr) or atr <= 0 or atr < ATR_MIN:
            i += 1
            continue
        ema_f, ema_s = row["ema9"], row["ema21"]
        price, color = row["close"], row["color"]
        momentum = "bull" if ema_f > ema_s else "bear"
        want = None
        if momentum == "bull" and color == "bear" and price > ema_s:
            want = "long"
        elif momentum == "bear" and color == "bull" and price < ema_s:
            want = "short"
        if want is None:
            i += 1
            continue

        forming_range = row["high"] - row["low"]
        if forming_range > SPIKE_RANGE_MULT * atr:
            i += 1
            continue

        c15_idx = closed15_count[i] - 1
        regime_label, er_val = "unknown", None
        if c15_idx >= ER_LOOKBACK:
            trend_flag = c15.iloc[c15_idx]["trend"]
            er_val = c15.iloc[c15_idx]["er"]
            regime_label = "TREND" if trend_flag else "CHOP"
        if gate_chop and regime_label == "CHOP":
            i += 1
            continue

        direction = 1 if want == "long" else -1
        entry_price = price
        lo = max(0, (c15_idx if c15_idx >= 0 else 0) - 40)
        hi = (c15_idx if c15_idx >= 0 else 0) + 1
        candle_window = c15_records[lo:hi]
        wall_info = dynamic_tp_distance(entry_price, direction, atr, candle_window)
        tp_dist_frac = wall_info["tp_dist_frac"]

        mfe_frac = 0.0
        be_armed = False
        exit_type, exit_frac, hold_bars = None, None, 0
        j = i + 1
        while j < n:
            hold_bars += 1
            h, l = df.iloc[j]["high"], df.iloc[j]["low"]
            if direction == 1:
                adverse = (entry_price - l) / entry_price
                favorable = (h - entry_price) / entry_price
            else:
                adverse = (h - entry_price) / entry_price
                favorable = (entry_price - l) / entry_price
            mfe_frac = max(mfe_frac, favorable)

            if adverse >= mae_wall:
                exit_type, exit_frac = "MAE_KILL", -mae_wall
                break
            if adverse >= HARD_SL_FRAC:
                exit_type, exit_frac = "HARD_SL", -HARD_SL_FRAC
                break
            if not be_armed and mfe_frac >= BE_TRIGGER_FRAC * tp_dist_frac:
                be_armed = True
            if be_armed:
                fee_buffer = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005
                if favorable <= fee_buffer:
                    exit_type, exit_frac = "BE_STOP", fee_buffer
                    break
            if favorable >= tp_dist_frac:
                exit_type, exit_frac = "TP", tp_dist_frac
                break
            if hold_bars >= MAX_POS_AGE_BARS:
                exit_type = "MAX_AGE"
                exit_frac = (df.iloc[j]["close"] - entry_price) / entry_price * direction
                break
            j += 1
        else:
            exit_type = "EOD"
            exit_frac = (df.iloc[n - 1]["close"] - entry_price) / entry_price * direction

        net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
        trades.append({
            "pair": pair, "regime": regime_label, "er": er_val,
            "wall_mode": wall_info["mode"], "tp_dist_frac": tp_dist_frac,
            "exit_type": exit_type, "net_frac": net_frac, "hold_bars": hold_bars,
            "ts": str(df.iloc[i]["ts"]),
        })
        i = j + 1

    return trades


def summarize(trades, label):
    if not trades:
        print(f"{label}: no trades")
        return None
    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df["net_frac"] > 0]
    losses = df[df["net_frac"] <= 0]
    expectancy = df["net_frac"].mean()
    print(f"\n=== {label} ===")
    print(f"trades={n} win_rate={len(wins)/n:.1%} expectancy={expectancy*100:.4f}% /trade "
          f"total={df['net_frac'].sum()*100:.3f}%")
    print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]))
    if "regime" in df:
        print(df.groupby("regime")["net_frac"].agg(["count", "mean", "sum"]))
    return df


def bootstrap_ci(net_fracs, n_boot=10000, seed=7):
    rng = np.random.default_rng(seed)
    arr = np.array(net_fracs)
    if len(arr) == 0:
        return None
    means = [rng.choice(arr, size=len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi, np.mean(means)


if __name__ == "__main__":
    all_baseline, all_v6 = [], []
    for pair in PAIRS:
        raw = load_pair(pair)
        c15 = build_15m(raw)
        c15 = compute_15m_regime(c15)
        base_trades = simulate_pair(pair, raw, c15, gate_chop=False)
        v6_trades = simulate_pair(pair, raw, c15, gate_chop=True)
        all_baseline += base_trades
        all_v6 += v6_trades

    base_df = summarize(all_baseline, "BASELINE (no chop gate, fixed 1.6xATR TP)")
    v6_df = summarize(all_v6, "V6 (chop-gated + dynamic wall TP)")

    if base_df is not None and v6_df is not None:
        lo, hi, mean_ = bootstrap_ci(base_df["net_frac"].tolist())
        print(f"\nBASELINE expectancy 95% bootstrap CI: [{lo*100:.4f}%, {hi*100:.4f}%] mean={mean_*100:.4f}%")
        lo, hi, mean_ = bootstrap_ci(v6_df["net_frac"].tolist())
        print(f"V6       expectancy 95% bootstrap CI: [{lo*100:.4f}%, {hi*100:.4f}%] mean={mean_*100:.4f}%")

        print("\n--- Kelly-fraction sizing suggestion (v6 buckets, min 30 trades) ---")
        for (pair, regime), g in v6_df.groupby(["pair", "regime"]):
            wins = g[g["net_frac"] > 0]["net_frac"]
            losses = g[g["net_frac"] <= 0]["net_frac"]
            if len(wins) == 0 or len(losses) == 0:
                continue
            wr = len(wins) / len(g)
            avg_w = wins.mean()
            avg_l = abs(losses.mean())
            res = size_multiplier(len(g), wr, avg_w, avg_l)
            print(f"{pair:10s} {regime:6s} n={len(g):4d} wr={wr:.1%} avg_w={avg_w*100:.3f}% "
                  f"avg_l={avg_l*100:.3f}% -> kelly_mult={res['multiplier']:.2f} ({res['reason']})")

        for label, d in [("BASELINE", base_df), ("V6", v6_df)]:
            d = d.copy()
            d["ts_parsed"] = pd.to_datetime(d["ts"])
            d["fold"] = pd.qcut(d["ts_parsed"].rank(method="first"), 3, labels=["fold1", "fold2", "fold3"])
            print(f"\n--- {label} walk-forward folds ---")
            print(d.groupby("fold")["net_frac"].agg(["count", "mean", "sum"]))

        base_df.to_csv("results_baseline.csv", index=False)
        v6_df.to_csv("results_v6.csv", index=False)
        print("\nSaved results_baseline.csv, results_v6.csv")
