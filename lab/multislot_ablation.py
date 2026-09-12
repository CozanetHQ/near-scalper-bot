"""
multislot_ablation.py — does the chop-gate's benefit survive REAL 8-slot concurrency?

Models the live fund-handling stack as faithfully as the 1m data allows:
  - 8 global slots; per-pair position stacking allowed (live behavior),
    NEAR capped at 3 (registry lock 2026-09-12)
  - 90-second global entry cooldown (registry lock)
  - 12% daily loss limit -> risk-off until next UTC day (registry lock, wired)
  - 25% drawdown wall -> 1/4 sizing below it (recovery sizing, registry lock)
  - per-position exits: NEAR MAE wall 2.5% (others 4%), HARD_SL 6%,
    BE_STOP after MFE >= 50% of TP distance, fixed TP = max(1.6xATR, 0.6% price),
    MAX_AGE 48h
  - equity compounds per trade: balance *= (1 + net_frac * 1.0625 * sizing)
    (slot notional = balance * 0.85/8 * 10x)

Arms (identical machinery, one difference):
  A  no regime gate
  B  chop gate: no entry when 15m efficiency-ratio regime == CHOP

Conventions (same as the verified single-slot lab, for comparability):
  entry at signal bar close; exits evaluated from the NEXT bar using candle
  extremes; MAE checked before BE before TP within a bar; BE_STOP exits at
  the fee-buffer level, MAE/HARD_SL at the wall level (level fills, not
  path-accurate intrabar sequencing) — identical to the already-audited runs.

Deliberately NOT modeled (disclosed): the EV-after-costs entry gate (needs
live observed W/L state), the 4-minute tick cadence (bars are evaluated every
minute — slightly more entry opportunities than live), Engine 2 scores.

Run: python3 multislot_ablation.py
"""
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
MAX_POSITIONS = 8
PAIR_CAP = {"NEARUSDT": 3}
PAIR_CAP_DEFAULT = 8
COOLDOWN_MS = 90_000
DAILY_LOSS_LIMIT = 0.12
DD_WALL = 0.25
RECOVERY_SIZE_FRAC = 0.25
NOTIONAL_MULT = 0.85 / 8 * 10  # slot notional as fraction of balance
START_BALANCE = 10.0
MAX_AGE_MS = 48 * 60 * 60_000

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT"]  # fixed eval order
MAE_WALL = {"NEARUSDT": 0.025}
MAE_WALL_DEFAULT = 0.04


def prep(pair):
    raw = load_pair(pair)               # has ts (datetime, utc), o/h/l/c
    df = compute_indicators(raw)
    c15 = compute_15m_regime(build_15m(df))
    # per-1m-bar mapping to the last CLOSED 15m candle index
    closed15_idx = [(i + 1) // 15 - 1 for i in range(len(df))]
    # regime label per 1m bar (None until ER lookback available)
    labels = [None] * len(df)
    trend_flags = c15["trend"].tolist()
    for i, ci in enumerate(closed15_idx):
        if ci >= ER_LOOKBACK and ci < len(trend_flags):
            labels[i] = "TREND" if trend_flags[ci] else "CHOP"
    df["regime"] = labels
    return df


def run_arm(gate_chop: bool):
    frames = {p: prep(p) for p in PAIRS}
    # merged global timeline
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}

    balance = START_BALANCE
    peak = balance
    day_start = balance
    cur_day = None
    riskoff_day = None
    trip_count = 0
    last_entry_ts = None
    positions = []   # dicts: pair, entry_price, direction, tp_frac, mfe, be_armed, entry_ts
    trades = []
    eq_chain = [balance]

    for t in all_ts:
        # --- daily boundary (UTC) ---
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day = day
            day_start = balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None

        # --- daily loss limit check (live wiring: trips on current balance) ---
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day
            trip_count += 1

        # --- exits: update every open position whose pair has a bar at t ---
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
                fee_buffer = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005
                if pos["be_armed"] and favorable <= fee_buffer:
                    exit_type, exit_frac = "BE_STOP", fee_buffer
                elif favorable >= pos["tp_frac"]:
                    exit_type, exit_frac = "TP", pos["tp_frac"]
                elif (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                    exit_type = "MAX_AGE"
                    exit_frac = (row["close"] - e) / e * d
            if exit_type is None:
                continue
            net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            # realize P&L with the sizing in force at entry (locked at entry)
            sizing = pos.get("sizing", 1.0)
            balance *= (1 + net_frac * NOTIONAL_MULT * sizing)
            peak = max(peak, balance)
            eq_chain.append(balance)
            positions.remove(pos)
            trades.append({"pair": p, "ts": str(t), "net_frac": net_frac,
                           "exit_type": exit_type,
                           "hold_min": (t - pos["entry_ts"]).total_seconds() / 60,
                           "slots_at_entry": pos["slots_at_entry"],
                           "pair_open_at_entry": pos["pair_open_at_entry"],
                           "regime": pos["regime"]})

        # --- entries: one attempt per pair per bar, fixed order ---
        for p in PAIRS:
            if len(positions) >= MAX_POSITIONS:
                break
            i = ts_index[p].get(t)
            if i is None or i < 30:
                continue
            if riskoff_day is not None:
                break
            row = frames[p].iloc[i]
            atr = row["atr14"]
            if pd.isna(atr) or atr <= 0 or atr < ATR_MIN:
                continue
            if last_entry_ts is not None and (t - last_entry_ts).total_seconds() * 1000 < COOLDOWN_MS:
                break
            pair_open = sum(1 for q in positions if q["pair"] == p)
            if pair_open >= PAIR_CAP.get(p, PAIR_CAP_DEFAULT):
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
            regime = row["regime"]
            if gate_chop and regime == "CHOP":
                continue
            direction = 1 if want == "long" else -1
            tp_frac = max(1.6 * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": direction,
                              "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False,
                              "entry_ts": t, "regime": regime, "sizing": sizing,
                              "slots_at_entry": len(positions) + 1,
                              "pair_open_at_entry": pair_open})
            last_entry_ts = t

    # close leftovers at final prices
    for pos in positions:
        p = pos["pair"]
        lastrow = frames[p].iloc[-1]
        e, d = pos["entry_price"], pos["direction"]
        exit_frac = (lastrow["close"] - e) / e * d
        net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
        balance *= (1 + net_frac * NOTIONAL_MULT * pos.get("sizing", 1.0))
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": p, "ts": str(frames[p]["ts"].iloc[-1]), "net_frac": net_frac,
                       "exit_type": "EOD", "hold_min": 0,
                       "slots_at_entry": pos["slots_at_entry"],
                       "pair_open_at_entry": pos["pair_open_at_entry"],
                       "regime": pos["regime"]})

    # true max drawdown on the compounded equity chain (entry-locked sizing included)
    run_peak = eq_chain[0]
    maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    return pd.DataFrame(trades), balance, peak, trip_count, maxdd


def report(label, df, balance, peak, trips, maxdd):
    n = len(df)
    wr = (df["net_frac"] > 0).mean()
    exp = df["net_frac"].mean() * 100
    tot = df["net_frac"].sum() * 100
    print(f"\n=== {label} ===")
    print(f"trades={n}  win_rate={wr:.1%}  expectancy={exp:+.4f}%/trade  net_frac total={tot:+.2f}%")
    print(f"final balance=${balance:.3f} (start $10)  peak=${peak:.3f}  daily-limit trips={trips}  approx maxDD={maxdd:.1%}")
    print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]))
    print("\nby pair:")
    print(df.groupby("pair")["net_frac"].agg(["count", "mean", "sum"]))
    print("\nconcurrency at entry (global slots):")
    print(df.groupby("slots_at_entry")["net_frac"].agg(["count", "mean", "sum"]))
    return df


if __name__ == "__main__":
    a_df, a_bal, a_peak, a_trips, a_dd = run_arm(gate_chop=False)
    a_df = report("ARM A — 8-slot live machinery, NO regime gate", a_df, a_bal, a_peak, a_trips, a_dd)
    b_df, b_bal, b_peak, b_trips, b_dd = run_arm(gate_chop=True)
    b_df = report("ARM B — 8-slot live machinery, CHOP GATE ON", b_df, b_bal, b_peak, b_trips, b_dd)

    print("\n=== walk-forward (chronological thirds), ARM B (chop gate) ===")
    b_df["ts_parsed"] = pd.to_datetime(b_df["ts"])
    b_df["fold"] = pd.qcut(b_df["ts_parsed"].rank(method="first"), 3, labels=["fold1", "fold2", "fold3"])
    print(b_df.groupby("fold")["net_frac"].agg(["count", "mean", "sum"]).assign(
        mean_pct=lambda d: d["mean"] * 100, sum_pct=lambda d: d["sum"] * 100))

    a_df.to_csv("multislot_armA.csv", index=False)
    b_df.to_csv("multislot_armB.csv", index=False)
    print("\nsaved multislot_armA.csv, multislot_armB.csv")
