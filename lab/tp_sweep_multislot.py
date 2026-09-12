"""
tp_sweep_multislot.py — TP-size sweep under the REAL 8-slot live machinery.

Built to transfer: identical fund-handling to multislot_ablation.py (8 slots,
NEAR 3-cap, 90s cooldown, 12% daily limit wired, 25% DD wall with 1/4 recovery
sizing, entry-locked sizing, equity compounding, level fills, MAE>BE>TP within
bar). No regime gate (proven: no benefit under concurrency). The ONLY variable
is TP distance = max(k * ATR14, 0.6% price) — the fee-amortization lever.

BE trigger stays proportional (50% of TP distance) so the rescue scales with k.
MAE walls stay per-pair (NEAR 2.5%, others 4%) — bigger TPs ride further and
will hit walls more; that trade-off is exactly what this sweep measures.

Run: python3 tp_sweep_multislot.py
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                HARD_SL_FRAC, BE_TRIGGER_FRAC, MAX_POSITIONS, PAIR_CAP,
                                PAIR_CAP_DEFAULT, COOLDOWN_MS, DAILY_LOSS_LIMIT,
                                DD_WALL, RECOVERY_SIZE_FRAC, NOTIONAL_MULT,
                                START_BALANCE, MAX_AGE_MS, MAE_WALL, MAE_WALL_DEFAULT,
                                SPIKE_RANGE_MULT, MIN_TP_DIST_FRAC)


def run_arm(tp_mult: float, fee: float = FEE_RATE, slip: float = SLIP_ASSUMED_PCT):
    frames = {p: prep(p) for p in PAIRS}
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}

    balance = START_BALANCE
    peak = balance
    day_start = balance
    cur_day = None
    riskoff_day = None
    last_entry_ts = None
    positions = []
    trades = []
    eq_chain = [balance]

    def close_pos(pos, exit_type, exit_frac, t, row=None):
        nonlocal balance, peak
        net_frac = exit_frac - 2 * fee - 2 * slip
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "gross_frac": exit_frac, "exit_type": exit_type,
                       "tp_mult": tp_mult, "hold_min": (t - pos["entry_ts"]).total_seconds() / 60,
                       "regime": pos["regime"]})

    for t in all_ts:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day = day
            day_start = balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day

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
            if adverse >= wall:
                close_pos(pos, "MAE_KILL", -wall, t); positions.remove(pos); continue
            if adverse >= HARD_SL_FRAC:
                close_pos(pos, "HARD_SL", -HARD_SL_FRAC, t); positions.remove(pos); continue
            if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                pos["be_armed"] = True
            fee_buffer = 2 * fee + 2 * slip + 0.0005
            if pos["be_armed"] and favorable <= fee_buffer:
                close_pos(pos, "BE_STOP", fee_buffer, t); positions.remove(pos); continue
            if favorable >= pos["tp_frac"]:
                close_pos(pos, "TP", pos["tp_frac"], t); positions.remove(pos); continue
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                close_pos(pos, "MAX_AGE", (row["close"] - e) / e * d, t); positions.remove(pos)

        # entries (identical signal, no regime gate)
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
            direction = 1 if want == "long" else -1
            tp_frac = max(tp_mult * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": direction,
                              "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False,
                              "entry_ts": t, "regime": row["regime"], "sizing": sizing})
            last_entry_ts = t

    for pos in positions:
        lastrow = frames[pos["pair"]].iloc[-1]
        e, d = pos["entry_price"], pos["direction"]
        close_pos(pos, "EOD", (lastrow["close"] - e) / e * d, frames[pos["pair"]]["ts"].iloc[-1])

    run_peak = eq_chain[0]; maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    return pd.DataFrame(trades), balance, maxdd


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x)
    if len(arr) == 0:
        return None
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi


if __name__ == "__main__":
    K_VALUES = [1.6, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
    results = {}
    print(f"{'k':>4} | {'n':>5} {'WR':>6} {'gross/trade':>12} {'NET/trade':>11} {'CI low':>8} {'CI high':>8} {'final$':>7} {'maxDD':>6} | exit mix (TP/BE/KILL)")
    for k in K_VALUES:
        df, bal, dd = run_arm(k)
        results[k] = df
        n = len(df)
        wr = (df["net_frac"] > 0).mean()
        gross = df["gross_frac"].mean() * 100
        net = df["net_frac"].mean() * 100
        ci = boot_ci(df["net_frac"].tolist())
        tp = (df["exit_type"] == "TP").sum(); be = (df["exit_type"] == "BE_STOP").sum()
        kl = (df["exit_type"].isin(["MAE_KILL", "HARD_SL"])).sum()
        print(f"{k:>4} | {n:>5} {wr:>6.1%} {gross:>+11.4f}% {net:>+10.4f}% {ci[0]*100:>+7.3f}% {ci[1]*100:>+7.3f}% {bal:>7.3f} {dd:>6.1%} | {tp}/{be}/{kl}")

    best_k = max(results, key=lambda k: results[k]["net_frac"].mean())
    print(f"\nBest k by net expectancy: {best_k}")
    df = results[best_k]
    df["ts_parsed"] = pd.to_datetime(df["ts"])
    df["fold"] = pd.qcut(df["ts_parsed"].rank(method="first"), 3, labels=["fold1", "fold2", "fold3"])
    print("\nwalk-forward (chronological thirds) at best k:")
    print(df.groupby("fold").agg(n=("net_frac", "count"), net_mean=("net_frac", lambda x: x.mean()*100),
                                 net_sum=("net_frac", lambda x: x.sum()*100)))
    print("\nby pair at best k:")
    print(df.groupby("pair")["net_frac"].agg(["count", "mean", "sum"]).assign(
        mean_pct=lambda d: d["mean"]*100, sum_pct=lambda d: d["sum"]*100))
    print("\nby exit type at best k:")
    print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]))

    # save all arms for the record
    pd.concat([d.assign(k=k) for k, d in results.items()]).to_csv("tp_sweep_multislot.csv", index=False)
    print("\nsaved tp_sweep_multislot.csv")
