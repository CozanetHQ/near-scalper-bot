"""floor_6040_test.py — owner's 60/40 floor (2026-09-13): once MFE >= 60% of the
TP distance, the protective floor locks at entry + 40% of the TP distance.
A retrace to the floor exits THERE (+0.4xTP) instead of scraping back to the
fee-buffer BE. Existing BE (arm @50% -> exit at fee buffer) stays for trades
that only reached 50-59%. Everything else = live exit stack, 10x, taker fees.
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                MAX_POSITIONS, PAIR_CAP, PAIR_CAP_DEFAULT,
                                COOLDOWN_MS, DAILY_LOSS_LIMIT, DD_WALL,
                                RECOVERY_SIZE_FRAC, START_BALANCE, MAX_AGE_MS,
                                MAE_WALL, MAE_WALL_DEFAULT, SPIKE_RANGE_MULT,
                                MIN_TP_DIST_FRAC, BE_TRIGGER_FRAC)

NOTIONAL_MULT = 0.85 / 8 * 10
HARD_SL_FRAC = 0.06
FLOOR_ARM = 0.60
FLOOR_LVL = 0.40


def run_arm(tp_mult, fee=0.0006, slip=0.0001, floor_on=True):
    frames = {p: prep(p) for p in PAIRS}
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}
    balance, peak, day_start = START_BALANCE, START_BALANCE, START_BALANCE
    cur_day = riskoff_day = None
    last_entry_ts = None
    positions, trades = [], []
    eq_chain = [balance]

    def close_pos(pos, exit_type, exit_frac, t):
        nonlocal balance, peak
        net_frac = exit_frac - 2 * fee - 2 * slip
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "exit_type": exit_type, "mfe_frac": pos["mfe"], "tp_frac": pos["tp_frac"]})

    for t in all_ts:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day, day_start = day, balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day

        for pos in list(positions):
            p = pos["pair"]
            i = ts_index[p].get(t)
            if i is None:
                continue
            row = frames[p].iloc[i]
            e, d = pos["entry_price"], pos["direction"]
            if d == 1:
                adverse, favorable = (e - row["low"]) / e, (row["high"] - e) / e
            else:
                adverse, favorable = (row["high"] - e) / e, (e - row["low"]) / e
            pos["mfe"] = max(pos["mfe"], favorable)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            if adverse >= wall:
                close_pos(pos, "MAE_KILL", -wall, t); positions.remove(pos); continue
            if adverse >= HARD_SL_FRAC:
                close_pos(pos, "HARD_SL", -HARD_SL_FRAC, t); positions.remove(pos); continue
            if not pos["floor_armed"] and pos["mfe"] >= FLOOR_ARM * pos["tp_frac"]:
                pos["floor_armed"] = True
            if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                pos["be_armed"] = True
            if floor_on and pos["floor_armed"] and favorable <= FLOOR_LVL * pos["tp_frac"]:
                close_pos(pos, "FLOOR40", FLOOR_LVL * pos["tp_frac"], t); positions.remove(pos); continue
            fee_buffer = 2 * fee + 2 * slip + 0.0005
            if pos["be_armed"] and favorable <= fee_buffer:
                close_pos(pos, "BE_STOP", fee_buffer, t); positions.remove(pos); continue
            if favorable >= pos["tp_frac"]:
                close_pos(pos, "TP", pos["tp_frac"], t); positions.remove(pos); continue
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                close_pos(pos, "MAX_AGE", (row["close"] - e) / e * d, t); positions.remove(pos)

        for p in PAIRS:
            if len(positions) >= MAX_POSITIONS or riskoff_day is not None:
                break
            i = ts_index[p].get(t)
            if i is None or i < 30:
                continue
            row = frames[p].iloc[i]
            atr = row["atr14"]
            if pd.isna(atr) or atr <= 0 or atr < ATR_MIN:
                continue
            if last_entry_ts is not None and (t - last_entry_ts).total_seconds() * 1000 < COOLDOWN_MS:
                break
            if sum(1 for q in positions if q["pair"] == p) >= PAIR_CAP.get(p, PAIR_CAP_DEFAULT):
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
            tp_frac = max(tp_mult * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": 1 if want == "long" else -1,
                              "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False, "floor_armed": False,
                              "entry_ts": t, "sizing": sizing})
            last_entry_ts = t

    for pos in positions:
        lr = frames[pos["pair"]].iloc[-1]
        close_pos(pos, "EOD", (lr["close"] - pos["entry_price"]) / pos["entry_price"] * pos["direction"],
                  frames[pos["pair"]]["ts"].iloc[-1])
    run_peak = eq_chain[0]; maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    return pd.DataFrame(trades), balance, maxdd


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x)
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi


if __name__ == "__main__":
    for label, floor in [("CURRENT (no floor, live today)", False), ("60/40 FLOOR (owner spec)", True)]:
        df, bal, dd = run_arm(1.6, floor_on=floor)
        net = df["net_frac"].mean() * 100
        ci = boot_ci(df["net_frac"].tolist())
        df["tsp"] = pd.to_datetime(df["ts"])
        df["fold"] = pd.qcut(df["tsp"].rank(method="first"), 3, labels=["f1", "f2", "f3"])
        folds = df.groupby("fold")["net_frac"].mean() * 100
        print(f"\n=== {label} ===")
        print(f"n={len(df)} WR={(df['net_frac']>0).mean():.1%} net={net:+.4f}%/trade CI[{ci[0]*100:+.3f}%,{ci[1]*100:+.3f}%] final=${bal:.2f} maxDD={dd:.1%}")
        print("folds: " + " ".join(f"{k}:{v:+.3f}%" for k, v in folds.items()))
        print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]).round(5))
        df.to_csv("floor6040.csv" if floor else "floor_base.csv", index=False)
