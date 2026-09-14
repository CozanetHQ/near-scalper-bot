"""
v7_ablation.py — V5 sizing vs V7 sizing under REAL concurrency.

Same audited machinery as multislot_ablation.py (2026-09-12): identical
signals, exits, cooldown, daily loss limit, DD wall + recovery sizing. The
ONLY difference between arms is the sizing/slot architecture:

  Arm A (V5 live):  8 fixed global slots; per-slot notional = balance * 0.85/8 * 10x
                    (balance-fraction compounding); NEAR cap 3; BE buffer 0.19%;
                    no correlation cap.
  Arm B (V7 owner): FIXED $33 clips (dollar P&L, no per-trade compounding);
                    N_SLOTS = max(1, floor(balance / 3.30)) capped 30;
                    MARGIN_BUDGET 0.99 (clip margin 3.3 at base 10x);
                    NEAR cap 1; BE buffer 0.27%; CORR_DIR_CAP 2 (max 2
                    same-direction BTC/ETH/SOL clips); recovery clips $8.25.

Deliberately NOT modeled (disclosed): leverage re-tiering (5x/15x/20x zones —
both arms use base 10x), the EV-after-costs gate, 4-min tick cadence (bars are
evaluated every minute), Engine 2 scores. Entry locked sizing in force at
entry (recovery), per the live engine.

Run from repo root:  python3 lab/v7_ablation.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import numpy as np
import pandas as pd

from ablation import (load_pair, compute_indicators, build_15m, compute_15m_regime,
                      MIN_TP_DIST_FRAC, ER_LOOKBACK, SPIKE_RANGE_MULT)

FEE_RATE = 0.0006
SLIP_ASSUMED_PCT = 0.0001
ATR_MIN = 0.0008
HARD_SL_FRAC = 0.06
BE_TRIGGER_FRAC = 0.5
COOLDOWN_MS = 90_000
DAILY_LOSS_LIMIT = 0.12
DD_WALL = 0.25
RECOVERY_SIZE_FRAC = 0.25
START_BALANCE = 10.0
MAX_AGE_MS = 48 * 60 * 60_000
MAE_WALL = {"NEARUSDT": 0.025}
MAE_WALL_DEFAULT = 0.04
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT"]
CORR_CLUSTER = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Arm A (V5) constants
A_MAX_POSITIONS = 8
A_NOTIONAL_MULT = 0.85 / 8 * 10
A_PAIR_CAP = {"NEARUSDT": 3}
A_PAIR_CAP_DEFAULT = 8
A_BE_BUFFER = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005   # 0.0019

# Arm B (V7) constants
B_FIXED_NOTIONAL = 33.0
B_SLOT_COST = 3.30
B_MARGIN_BUDGET = 0.99
B_SLOT_CEIL = 30
B_PAIR_CAP = {"NEARUSDT": 1}
B_PAIR_CAP_DEFAULT = 8
B_BE_BUFFER = 0.0027
B_CORR_DIR_CAP = 2
B_BASE_LEV = 10


def prep(pair):
    raw = load_pair(pair)
    df = compute_indicators(raw)
    c15 = compute_15m_regime(build_15m(df))
    closed15_idx = [(i + 1) // 15 - 1 for i in range(len(df))]
    labels = [None] * len(df)
    trend_flags = c15["trend"].tolist()
    for i, ci in enumerate(closed15_idx):
        if ci >= ER_LOOKBACK and ci < len(trend_flags):
            labels[i] = "TREND" if trend_flags[ci] else "CHOP"
    df["regime"] = labels
    return df


def run_arm(arm):
    v7 = (arm == "B")
    frames = {p: prep(p) for p in PAIRS}
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}

    balance, peak = START_BALANCE, START_BALANCE
    day_start, cur_day, riskoff_day, trip_count = START_BALANCE, None, None, 0
    last_entry_ts = None
    positions, trades, eq_chain = [], [], [balance]

    def be_buffer():
        return B_BE_BUFFER if v7 else A_BE_BUFFER

    def clip_usd(sizing):
        if not v7:
            return 0.0
        return max(B_FIXED_NOTIONAL * sizing, 1.0)

    for t in all_ts:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day, day_start = day, balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day
            trip_count += 1

        # exits
        for pos in list(positions):
            p = pos["pair"]
            i = ts_index[p].get(t)
            if i is None:
                continue
            row = frames[p].iloc[i]
            e, d = pos["entry_price"], pos["direction"]
            if d == 1:
                adverse = (e - row["low"]) / e
                favorable = (row["high"] - e) / e
            else:
                adverse = (row["high"] - e) / e
                favorable = (e - row["low"]) / e
            pos["mfe"] = max(pos["mfe"], favorable)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            exit_type, exit_frac = None, None
            if adverse >= wall:
                exit_type, exit_frac = "MAE_KILL", -wall
            elif adverse >= HARD_SL_FRAC:
                exit_type, exit_frac = "HARD_SL", -HARD_SL_FRAC
            else:
                if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                    pos["be_armed"] = True
                if pos["be_armed"] and favorable <= be_buffer():
                    exit_type, exit_frac = "BE_STOP", be_buffer()
                elif favorable >= pos["tp_frac"]:
                    exit_type, exit_frac = "TP", pos["tp_frac"]
                elif (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                    exit_type, exit_frac = "MAX_AGE", (row["close"] - e) / e * d
            if exit_type is None:
                continue
            net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
            sizing = pos.get("sizing", 1.0)
            if v7:
                balance += net_frac * pos["clip_usd"] * sizing
            else:
                balance *= (1 + net_frac * A_NOTIONAL_MULT * sizing)
            peak = max(peak, balance)
            eq_chain.append(balance)
            positions.remove(pos)
            trades.append({"pair": p, "ts": str(t), "net_frac": net_frac,
                           "exit_type": exit_type, "balance_after": balance,
                           "hold_min": (t - pos["entry_ts"]).total_seconds() / 60,
                           "slots_at_entry": pos["slots_at_entry"],
                           "regime": pos["regime"]})

        # entries
        for p in PAIRS:
            if riskoff_day is not None:
                break
            if last_entry_ts is not None and (t - last_entry_ts).total_seconds() * 1000 < COOLDOWN_MS:
                break
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            if v7:
                n_slots = max(1, min(int(balance // B_SLOT_COST), B_SLOT_CEIL))
                if len(positions) >= n_slots:
                    break
                clip = clip_usd(sizing)
                clip_margin = clip / B_BASE_LEV
                used_margin = sum(q["margin"] for q in positions)
                if used_margin + clip_margin > balance * B_MARGIN_BUDGET:
                    break
                pair_cap = B_PAIR_CAP.get(p, B_PAIR_CAP_DEFAULT)
                pair_open = sum(1 for q in positions if q["pair"] == p)
                if pair_open >= pair_cap:
                    continue
            else:
                if len(positions) >= A_MAX_POSITIONS:
                    break
                pair_open = sum(1 for q in positions if q["pair"] == p)
                if pair_open >= A_PAIR_CAP.get(p, A_PAIR_CAP_DEFAULT):
                    continue
            i = ts_index[p].get(t)
            if i is None or i < 30:
                continue
            row = frames[p].iloc[i]
            atr = row["atr14"]
            if pd.isna(atr) or atr <= 0 or atr < ATR_MIN:
                continue
            ema_f, ema_s, price, color = row["ema9"], row["ema21"], row["close"], row["color"]
            momentum = "bull" if ema_f > ema_s else "bear"
            want = None
            if momentum == "bull" and color == "bear" and price > ema_s:
                want = "long"
            elif momentum == "bear" and color == "bull" and price < ema_s:
                want = "short"
            if want is None:
                continue
            if (row["high"] - row["low"]) > SPIKE_RANGE_MULT * atr:
                continue
            if v7 and p in CORR_CLUSTER and B_CORR_DIR_CAP > 0:
                same_dir = sum(1 for q in positions
                               if q["pair"] in CORR_CLUSTER and q["direction"] == (1 if want == "long" else -1))
                if same_dir >= B_CORR_DIR_CAP:
                    continue
            direction = 1 if want == "long" else -1
            tp_frac = max(1.6 * atr / price, MIN_TP_DIST_FRAC)
            pos = {"pair": p, "entry_price": price, "direction": direction,
                   "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False,
                   "entry_ts": t, "regime": row["regime"], "sizing": sizing,
                   "slots_at_entry": len(positions) + 1}
            if v7:
                pos["clip_usd"] = clip
                pos["margin"] = clip_margin
            positions.append(pos)
            last_entry_ts = t

    for pos in positions:
        p = pos["pair"]
        lastrow = frames[p].iloc[-1]
        e, d = pos["entry_price"], pos["direction"]
        exit_frac = (lastrow["close"] - e) / e * d
        net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
        if v7:
            balance += net_frac * pos["clip_usd"] * pos.get("sizing", 1.0)
        else:
            balance *= (1 + net_frac * A_NOTIONAL_MULT * pos.get("sizing", 1.0))
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": p, "ts": str(frames[p]["ts"].iloc[-1]), "net_frac": net_frac,
                       "exit_type": "EOD", "balance_after": balance, "hold_min": 0,
                       "slots_at_entry": pos["slots_at_entry"], "regime": pos["regime"]})

    run_peak, maxdd = eq_chain[0], 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    return pd.DataFrame(trades), balance, peak, trip_count, maxdd


def report(label, df, balance, peak, trips, maxdd):
    n = len(df)
    wr = (df["net_frac"] > 0).mean()
    exp = df["net_frac"].mean() * 100
    print(f"\n=== {label} ===")
    print(f"trades={n}  win_rate={wr:.1%}  expectancy={exp:+.4f}%/trade")
    print(f"final balance=${balance:.3f} (start $10)  peak=${peak:.3f}  daily trips={trips}  maxDD={maxdd:.1%}")
    print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]).round(5))
    print("by pair:")
    print(df.groupby("pair")["net_frac"].agg(["count", "mean", "sum"]).round(5))
    return df


def boot_diff(a, b, n_boot=10_000, seed=13):
    rng = np.random.default_rng(seed)
    x, y = a["net_frac"].to_numpy(), b["net_frac"].to_numpy()
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        ax = rng.choice(x, len(x), replace=True)
        by = rng.choice(y, len(y), replace=True)
        diffs[i] = ax.mean() - by.mean()
    return np.percentile(diffs, [2.5, 50, 97.5])


if __name__ == "__main__":
    dfa, bala, peaka, tripsa, dda = run_arm("A")
    dfb, balb, peakb, tripsb, ddb = run_arm("B")
    report("ARM A — V5 sizing (8 fixed slots, balance-fraction)", dfa, bala, peaka, tripsa, dda)
    report("ARM B — V7 sizing ($33 clips, balance-scaled slots, corr cap, NEAR 1)", dfb, balb, peakb, tripsb, ddb)
    lo, med, hi = boot_diff(dfa, dfb)
    print(f"\nBootstrap on per-trade expectancy difference A-B (10k resamples, seed 13):")
    print(f"  mean diff {med*100:+.4f}%/trade  95% CI [{lo*100:+.4f}%, {hi*100:+.4f}%]")
    ea, eb = dfa['net_frac'].mean(), dfb['net_frac'].mean()
    print(f"  Arm A {ea*100:+.4f}%/trade vs Arm B {eb*100:+.4f}%/trade")
    dfa.to_csv("lab/v7_armA_trades.csv", index=False)
    dfb.to_csv("lab/v7_armB_trades.csv", index=False)
    print("\ntrades saved: lab/v7_armA_trades.csv, lab/v7_armB_trades.csv")
