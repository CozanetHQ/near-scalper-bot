"""
split_tp_test.py — owner proposal, tested under the real 8-slot machinery:
  (a) TP split in two: bank 50% of notional at 75% of TP distance,
      runner (50%) rides to the normal full TP.
  (b) BE raised above BE: once MFE >= 50% of TP, stop sits at entry + 25% of TP
      distance (not entry + fee buffer) — the "BE above BE a little bit".

Compared against BASE (current live exit) and R (ratchet-only) from §9.
Runner that retraces exits at the raised BE stop; banked half keeps its level.
Fees pro-rated; round-trip cost unchanged. k = 1.6 (live value).
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                HARD_SL_FRAC, MAX_POSITIONS, PAIR_CAP,
                                PAIR_CAP_DEFAULT, COOLDOWN_MS, DAILY_LOSS_LIMIT,
                                DD_WALL, RECOVERY_SIZE_FRAC, NOTIONAL_MULT,
                                START_BALANCE, MAX_AGE_MS, MAE_WALL, MAE_WALL_DEFAULT,
                                SPIKE_RANGE_MULT, MIN_TP_DIST_FRAC)

ARM_MFE = 0.50      # arm raised-BE at 50% of TP distance (same as live)
STOP_FRAC = 0.25    # raised BE stop at 25% of TP distance ("a little bit above BE")
BANK_MFE = 0.75     # bank half at 75% of TP distance
BANK_W = 0.50       # half the position


def run_arm(tp_mult, fee, slip):
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
                       "mfe_frac": pos["mfe"], "tp_mult": tp_mult})

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
            pos["mfe"] = max(pos["mfe"], favorable)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            tp = pos["tp_frac"]
            rem = 1.0 - (BANK_W if pos["banked"] else 0.0)
            banked = BANK_W * BANK_MFE * tp if pos["banked"] else 0.0

            # walls (banked half keeps its level; remaining size takes the hit)
            if adverse >= wall or adverse >= HARD_SL_FRAC:
                frac = -wall if adverse >= wall else -HARD_SL_FRAC
                realize(banked + rem * frac - 2 * (fee + slip), pos, t,
                        "MAE_KILL" if frac == -wall else "HARD_SL")
                positions.remove(pos)
                continue
            # arm raised BE
            if not pos["armed"] and pos["mfe"] >= ARM_MFE * tp:
                pos["armed"] = True
            # bank half at 75% of TP
            if not pos["banked"] and pos["mfe"] >= BANK_MFE * tp:
                pos["banked"] = True
                banked = BANK_W * BANK_MFE * tp
                rem = 1.0 - BANK_W
            # stop at raised BE
            if pos["armed"] and favorable <= STOP_FRAC * tp:
                cls = "BE_RAISED_BANKED" if pos["banked"] else "BE_RAISED"
                realize(banked + rem * STOP_FRAC * tp - 2 * (fee + slip), pos, t, cls)
                positions.remove(pos)
                continue
            # full TP
            if pos["mfe"] >= tp:
                realize(banked + rem * tp - 2 * (fee + slip), pos, t, "TP")
                positions.remove(pos)
                continue
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                frac = (row["close"] - e) / e * d
                realize(banked + rem * frac - 2 * (fee + slip), pos, t, "MAX_AGE")
                positions.remove(pos)

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
                              "tp_frac": tp_frac, "mfe": 0.0, "armed": False, "banked": False,
                              "entry_ts": t, "sizing": sizing})
            last_entry_ts = t

    for pos in positions:
        lastrow = frames[pos["pair"]].iloc[-1]
        frac = (lastrow["close"] - pos["entry_price"]) / pos["entry_price"] * pos["direction"]
        banked = BANK_W * BANK_MFE * pos["tp_frac"] if pos["banked"] else 0.0
        rem = 1.0 - (BANK_W if pos["banked"] else 0.0)
        realize(banked + rem * frac - 2 * (fee + slip), pos, frames[pos["pair"]]["ts"].iloc[-1], "EOD")

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
    for label, fee, slip in [("TAKER (live today)", 0.0006, 0.0001),
                             ("MAKER (economics lever)", 0.0002, 0.0001)]:
        df, bal, dd = run_arm(1.6, fee, slip)
        net = df["net_frac"].mean() * 100
        ci = boot_ci(df["net_frac"].tolist())
        wr = (df["net_frac"] > 0).mean()
        print(f"\n=== split-TP + raised-BE, k=1.6, {label} ===")
        print(f"n={len(df)} WR={wr:.1%} net={net:+.4f}%/trade CI[{ci[0]*100:+.3f}%,{ci[1]*100:+.3f}%] final=${bal:.3f} maxDD={dd:.1%}")
        print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]))
        df.to_csv(f"split_tp_{'taker' if fee>0.0003 else 'maker'}.csv", index=False)
