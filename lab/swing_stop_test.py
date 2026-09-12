"""
swing_stop_test.py — owner's proposal: stop-loss placed where it belongs (behind
the swing point), BE/ratchet machinery taken OFF.

Entry signal unchanged. At entry:
  long  -> stop = lowest low of the last 120 bars (2-hour swing), exit if crossed
  short -> stop = highest high of the last 120 bars
Exits: SWING_STOP (at the swing), HARD_SL 6% (catastrophic backstop only),
TP 1.6xATR, MAX_AGE 48h, EOD. NO breakeven, NO ratchet, NO MAE wall.

Variants: SWING (raw swing distance) and SWING_CAP4 (swing capped at 4%).
Full 8-slot live machinery otherwise. Taker fees (today's reality).
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                HARD_SL_FRAC, MAX_POSITIONS, PAIR_CAP, PAIR_CAP_DEFAULT,
                                COOLDOWN_MS, DAILY_LOSS_LIMIT, DD_WALL, RECOVERY_SIZE_FRAC,
                                NOTIONAL_MULT, START_BALANCE, MAX_AGE_MS,
                                SPIKE_RANGE_MULT, MIN_TP_DIST_FRAC)

SWING_N = 120
CAP = None  # or 0.04


def run_arm(tp_mult, cap, fee, slip):
    frames = {p: prep(p) for p in PAIRS}
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}
    balance, peak, day_start = START_BALANCE, START_BALANCE, START_BALANCE
    cur_day = riskoff_day = None
    last_entry_ts = None
    positions, trades = [], []
    eq_chain = [balance]

    def realize(net_frac, pos, t, cls):
        nonlocal balance, peak
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "net_frac": net_frac, "exit_type": cls,
                       "stop_frac": pos["stop_frac"], "hold_min": (t - pos["entry_ts"]).total_seconds()/60})

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
            adverse = (e - row["low"]) / e if d == 1 else (row["high"] - e) / e
            favorable = (row["high"] - e) / e if d == 1 else (e - row["low"]) / e

            if adverse >= pos["stop_frac"]:
                realize(-pos["stop_frac"] - 2*(fee+slip), pos, t, "SWING_STOP")
                positions.remove(pos); continue
            if adverse >= HARD_SL_FRAC:
                realize(-HARD_SL_FRAC - 2*(fee+slip), pos, t, "HARD_SL")
                positions.remove(pos); continue
            if favorable >= pos["tp_frac"]:
                realize(pos["tp_frac"] - 2*(fee+slip), pos, t, "TP")
                positions.remove(pos); continue
            if (t - pos["entry_ts"]).total_seconds()*1000 >= MAX_AGE_MS:
                realize((row["close"]-e)/e*d - 2*(fee+slip), pos, t, "MAX_AGE")
                positions.remove(pos)

        for p in PAIRS:
            if len(positions) >= MAX_POSITIONS or riskoff_day is not None:
                break
            i = ts_index[p].get(t)
            if i is None or i < max(30, SWING_N):
                continue
            row = frames[p].iloc[i]
            atr = row["atr14"]
            if pd.isna(atr) or atr <= 0 or atr < ATR_MIN:
                continue
            if last_entry_ts is not None and (t-last_entry_ts).total_seconds()*1000 < COOLDOWN_MS:
                break
            if sum(1 for q in positions if q["pair"] == p) >= PAIR_CAP.get(p, PAIR_CAP_DEFAULT):
                continue
            ema_f, ema_s, price, color = row["ema9"], row["ema21"], row["close"], row["color"]
            mom = "bull" if ema_f > ema_s else "bear"
            want = None
            if mom == "bull" and color == "bear" and price > ema_s:
                want = "long"
            elif mom == "bear" and color == "bull" and price < ema_s:
                want = "short"
            if want is None:
                continue
            if (row["high"] - row["low"]) > SPIKE_RANGE_MULT * atr:
                continue
            d = 1 if want == "long" else -1
            if want == "long":
                swing = frames[p]["low"].iloc[i-SWING_N:i].min()
                stop = max((price - swing) / price, 0.001)
            else:
                swing = frames[p]["high"].iloc[i-SWING_N:i].max()
                stop = max((swing - price) / price, 0.001)
            if cap is not None:
                stop = min(stop, cap)
            tp_frac = max(tp_mult * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": d,
                              "tp_frac": tp_frac, "stop_frac": stop,
                              "entry_ts": t, "sizing": sizing})
            last_entry_ts = t

    for pos in positions:
        lr = frames[pos["pair"]].iloc[-1]
        realize((lr["close"]-pos["entry_price"])/pos["entry_price"]*pos["direction"] - 2*(fee+slip),
                pos, frames[pos["pair"]]["ts"].iloc[-1], "EOD")

    run_peak = eq_chain[0]; maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v/run_peak)
    return pd.DataFrame(trades), balance, maxdd


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x)
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi


if __name__ == "__main__":
    for label, cap, fee in [("SWING (raw, today's fees)", None, 0.0006),
                            ("SWING capped at 4% (today's fees)", 0.04, 0.0006),
                            ("SWING (raw, maker fees)", None, 0.0002)]:
        df, bal, dd = run_arm(1.6, cap, fee, 0.0001)
        net = df["net_frac"].mean() * 100
        ci = boot_ci(df["net_frac"].tolist())
        wr = (df["net_frac"] > 0).mean()
        df["ts"] = df.get("ts", None)
        print(f"\n=== {label} ===")
        print(f"trades={len(df)}  win_rate={wr:.1%}  average/trade={net:+.4f}%  "
              f"surely in [{ci[0]*100:+.3f}%, {ci[1]*100:+.3f}%]  end=${bal:.2f}  worst_dip={dd:.0%}")
        print(f"stop sizes: median {df.stop_frac.median()*100:.2f}%  mean {df.stop_frac.mean()*100:.2f}%  worst {df.stop_frac.max()*100:.2f}%")
        print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]).round(5))
        df.to_csv(f"swing_{abs(hash(label))%10000}.csv", index=False)
