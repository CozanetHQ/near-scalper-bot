"""
ablation.py — decompose v6's improvement: chop-gate alone vs wall-TP alone vs both.
Reuses backtest_v6.py's simulate_pair unmodified, but adds a use_wall_tp toggle so
each of the two changes can be measured in isolation. This was NOT in the attached
dossier — added independently to answer "where does the improvement actually come from."
"""
import json
import numpy as np
import pandas as pd
from regime import ER_TREND_THRESHOLD, ER_LOOKBACK, SPIKE_RANGE_MULT
from wall_tp import dynamic_tp_distance, MIN_TP_DIST_FRAC

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
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(ATR_PERIOD).mean()
    df["color"] = np.where(df["close"] >= df["open"], "bull", "bear")
    return df


def build_15m(df):
    grp = df.groupby(df.index // 15)
    return grp.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                   close=("close", "last"), ts=("ts", "first")).reset_index(drop=True)


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


def simulate_pair(pair, df, c15, gate_chop: bool, use_wall_tp: bool):
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
            i += 1; continue
        ema_f, ema_s = row["ema9"], row["ema21"]
        price, color = row["close"], row["color"]
        momentum = "bull" if ema_f > ema_s else "bear"
        want = None
        if momentum == "bull" and color == "bear" and price > ema_s:
            want = "long"
        elif momentum == "bear" and color == "bull" and price < ema_s:
            want = "short"
        if want is None:
            i += 1; continue
        forming_range = row["high"] - row["low"]
        if forming_range > SPIKE_RANGE_MULT * atr:
            i += 1; continue
        c15_idx = closed15_count[i] - 1
        regime_label, er_val = "unknown", None
        if c15_idx >= ER_LOOKBACK:
            trend_flag = c15.iloc[c15_idx]["trend"]
            er_val = c15.iloc[c15_idx]["er"]
            regime_label = "TREND" if trend_flag else "CHOP"
        if gate_chop and regime_label == "CHOP":
            i += 1; continue
        direction = 1 if want == "long" else -1
        entry_price = price
        if use_wall_tp:
            lo = max(0, (c15_idx if c15_idx >= 0 else 0) - 40)
            hi = (c15_idx if c15_idx >= 0 else 0) + 1
            candle_window = c15_records[lo:hi]
            wall_info = dynamic_tp_distance(entry_price, direction, atr, candle_window)
            tp_dist_frac = wall_info["tp_dist_frac"]
            wall_mode = wall_info["mode"]
        else:
            tp_dist_frac = max(1.6 * atr / entry_price, MIN_TP_DIST_FRAC)
            wall_mode = "fixed_1.6atr"
        mfe_frac = 0.0; be_armed = False
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
                exit_type, exit_frac = "MAE_KILL", -mae_wall; break
            if adverse >= HARD_SL_FRAC:
                exit_type, exit_frac = "HARD_SL", -HARD_SL_FRAC; break
            if not be_armed and mfe_frac >= BE_TRIGGER_FRAC * tp_dist_frac:
                be_armed = True
            if be_armed:
                fee_buffer = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005
                if favorable <= fee_buffer:
                    exit_type, exit_frac = "BE_STOP", fee_buffer; break
            if favorable >= tp_dist_frac:
                exit_type, exit_frac = "TP", tp_dist_frac; break
            if hold_bars >= MAX_POS_AGE_BARS:
                exit_type = "MAX_AGE"
                exit_frac = (df.iloc[j]["close"] - entry_price) / entry_price * direction
                break
            j += 1
        else:
            exit_type = "EOD"
            exit_frac = (df.iloc[n - 1]["close"] - entry_price) / entry_price * direction
        net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
        trades.append({"pair": pair, "regime": regime_label, "wall_mode": wall_mode,
                       "exit_type": exit_type, "net_frac": net_frac, "hold_bars": hold_bars})
        i = j + 1
    return trades


if __name__ == "__main__":
    variants = {
        "A_baseline (no gate, fixed TP)": dict(gate_chop=False, use_wall_tp=False),
        "B_chop_gate_only (gate, fixed TP)": dict(gate_chop=True, use_wall_tp=False),
        "C_wall_tp_only (no gate, wall TP)": dict(gate_chop=False, use_wall_tp=True),
        "D_full_v6 (gate + wall TP)": dict(gate_chop=True, use_wall_tp=True),
    }
    results = {}
    for label, kw in variants.items():
        all_trades = []
        for pair in PAIRS:
            raw = load_pair(pair)
            c15 = build_15m(raw)
            c15 = compute_15m_regime(c15)
            all_trades += simulate_pair(pair, raw, c15, **kw)
        df = pd.DataFrame(all_trades)
        results[label] = df
        n = len(df)
        exp = df["net_frac"].mean() * 100
        tot = df["net_frac"].sum() * 100
        wr = (df["net_frac"] > 0).mean() * 100
        print(f"{label:38s} n={n:5d}  WR={wr:5.1f}%  expectancy={exp:+.4f}%/trade  total={tot:+8.2f}%")

    print("\n--- Decomposition ---")
    a = results["A_baseline (no gate, fixed TP)"]["net_frac"].mean() * 100
    b = results["B_chop_gate_only (gate, fixed TP)"]["net_frac"].mean() * 100
    c = results["C_wall_tp_only (no gate, wall TP)"]["net_frac"].mean() * 100
    d = results["D_full_v6 (gate + wall TP)"]["net_frac"].mean() * 100
    print(f"Chop gate alone moves expectancy:      {a:+.4f}% -> {b:+.4f}%  (delta {b-a:+.4f})")
    print(f"Wall TP alone moves expectancy:        {a:+.4f}% -> {c:+.4f}%  (delta {c-a:+.4f})")
    print(f"Both together (reported 'v6') moves:   {a:+.4f}% -> {d:+.4f}%  (delta {d-a:+.4f})")
    print(f"Sum of isolated deltas: {(b-a)+(c-a):+.4f}  vs actual combined delta: {d-a:+.4f}  (interaction effect: {(d-a) - ((b-a)+(c-a)):+.4f})")
