"""
ladder_exit_sweep.py — the owner's laddered-TP proposal, tested under the real
8-slot live machinery.

Baseline exit (current live): BE_STOP armed at 50% of TP distance, exits at
entry + fee buffer on retrace; full exit at TP.

Variant R (ratchet-only): 4 levels at 25/50/75/100% of TP distance. Passing
level j ratchets the stop to level j-1 (level 0 = entry + fee buffer). Single
exit — full size out at the ratchet level (or full TP).

Variant P (owner's full idea): bank 25% of notional at each of levels 1-3 as
they pass; the final 25% runner rides with the ratchet stop. Fees pro-rated
(total exit volume = full notional, so round-trip costs are unchanged).
net_frac = sum(w_i * exit_frac_i) - 2*(fee+slip), weights sum to 1.

Everything else identical to tp_sweep_multislot.py: same signal (no regime
gate), same machinery (8 slots, NEAR 3-cap, 90s cooldown, 12% daily limit,
DD wall + recovery sizing, per-pair MAE walls, level fills, adverse-first
within-bar ordering). k = TP in ATR multiples.
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                HARD_SL_FRAC, MAX_POSITIONS, PAIR_CAP,
                                PAIR_CAP_DEFAULT, COOLDOWN_MS, DAILY_LOSS_LIMIT,
                                DD_WALL, RECOVERY_SIZE_FRAC, NOTIONAL_MULT,
                                START_BALANCE, MAX_AGE_MS, MAE_WALL, MAE_WALL_DEFAULT,
                                SPIKE_RANGE_MULT, MIN_TP_DIST_FRAC)

LEVELS = [0.25, 0.50, 0.75, 1.00]   # fractions of TP distance
BANK_WEIGHT = 0.25                  # variant P: bank at levels 1-3, runner = 0.25
BE_BUFFER = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005


def run_arm(tp_mult: float, variant: str):
    """variant: 'base' | 'ratchet' | 'ladder'"""
    frames = {p: prep(p) for p in PAIRS}
    all_ts = sorted(set().union(*[set(f["ts"].tolist()) for f in frames.values()]))
    ts_index = {p: {t: i for i, t in enumerate(frames[p]["ts"].tolist())} for p in PAIRS}

    balance = START_BALANCE
    peak = balance
    day_start = balance
    cur_day = riskoff_day = None
    last_entry_ts = None
    positions, trades = [], []
    eq_chain = [balance]

    def realize(net_frac, pos, t, cls):
        nonlocal balance, peak
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "exit_type": cls, "variant": variant, "k": tp_mult,
                       "mfe_frac": pos["mfe"], "hold_min": (t - pos["entry_ts"]).total_seconds() / 60})

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
                adverse = (e - row["low"]) / e
                favorable = (row["high"] - e) / e
            else:
                adverse = (row["high"] - e) / e
                favorable = (e - row["low"]) / e
            pos["mfe"] = max(pos["mfe"], favorable)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            tp = pos["tp_frac"]

            # 1) adverse walls first (conservative within-bar order, as before)
            remaining = 1.0 - pos["banked_weight"]
            if adverse >= wall or adverse >= HARD_SL_FRAC:
                frac = -wall if adverse >= wall else -HARD_SL_FRAC
                net = pos["banked_sum"] + remaining * frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                realize(net, pos, t, "MAE_KILL" if frac == -wall else "HARD_SL")
                positions.remove(pos)
                continue

            # 2) pass new levels (ratchet up / bank)
            if variant == "base":
                if not pos["be_armed"] and pos["mfe"] >= 0.5 * tp:
                    pos["be_armed"] = True
            else:
                while pos["lvl"] < len(LEVELS) and pos["mfe"] >= LEVELS[pos["lvl"]] * tp:
                    lf = LEVELS[pos["lvl"]] * tp
                    if variant == "ladder" and pos["lvl"] < 3:
                        pos["banked_sum"] += BANK_WEIGHT * lf
                        pos["banked_weight"] += BANK_WEIGHT
                    pos["lvl"] += 1  # stop now ratchets to LEVELS[lvl-1]
            # 3) stop-out at ratchet / BE
            if variant == "base":
                if pos["be_armed"] and favorable <= BE_BUFFER:
                    realize(BE_BUFFER - 2 * (FEE_RATE + SLIP_ASSUMED_PCT), pos, t, "BE_STOP")
                    positions.remove(pos)
                    continue
            else:
                stop_frac = (LEVELS[pos["lvl"] - 1] * tp) if pos["lvl"] >= 1 else None
                if pos["lvl"] == 0 and variant == "ratchet":
                    stop_frac = None  # no BE until level 1 passes (ratchet-only)
                if pos["lvl"] == 0 and variant == "ladder":
                    stop_frac = None
                if stop_frac is not None and favorable <= stop_frac:
                    if variant == "ratchet":
                        net = stop_frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                        cls = "RATCHET"
                    else:  # ladder: banked tranches + runner at stop level
                        net = pos["banked_sum"] + (1 - pos["banked_weight"]) * stop_frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                        cls = "LADDER_RATCHET"
                    realize(net, pos, t, cls)
                    positions.remove(pos)
                    continue
            # 4) full TP
            if pos["mfe"] >= tp:
                if variant == "ladder":
                    net = pos["banked_sum"] + (1 - pos["banked_weight"]) * tp - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                else:
                    net = tp - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                realize(net, pos, t, "TP")
                positions.remove(pos)
                continue
            # 5) max age
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                frac = (row["close"] - e) / e * d
                net = (pos["banked_sum"] + (1 - pos["banked_weight"]) * frac
                       - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)) if variant == "ladder" else frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
                realize(net, pos, t, "MAX_AGE")
                positions.remove(pos)

        # entries — identical to prior arms
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
                              "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False,
                              "lvl": 0, "banked_sum": 0.0, "banked_weight": 0.0,
                              "entry_ts": t, "sizing": sizing})
            last_entry_ts = t

    for pos in positions:
        lastrow = frames[pos["pair"]].iloc[-1]
        frac = (lastrow["close"] - pos["entry_price"]) / pos["entry_price"] * pos["direction"]
        net = (pos["banked_sum"] + (1 - pos["banked_weight"]) * frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)) \
            if variant == "ladder" else frac - 2 * (FEE_RATE + SLIP_ASSUMED_PCT)
        realize(net, pos, frames[pos["pair"]]["ts"].iloc[-1], "EOD")

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
    names = {"base": "BASE (BE@50%, full TP)",
             "ratchet": "R (ratchet-only, 4 levels)",
             "ladder": "P (bank 25% @ L1-L3 + runner)"}
    results = {}
    print(f"{'variant':32s} {'k':>4} {'n':>5} {'WR':>6} {'NET/trade':>10} {'CI low':>8} {'CI high':>8} {'final$':>7} {'maxDD':>6}")
    for k in (1.6, 2.0):
        for v in ("base", "ratchet", "ladder"):
            df, bal, dd = run_arm(k, v)
            results[(v, k)] = df
            net = df["net_frac"].mean() * 100
            ci = boot_ci(df["net_frac"].tolist())
            wr = (df["net_frac"] > 0).mean()
            print(f"{names[v]:32s} {k:>4} {len(df):>5} {wr:>6.1%} {net:>+9.4f}% {ci[0]*100:>+7.3f}% {ci[1]*100:>+7.3f}% {bal:>7.3f} {dd:>6.1%}")

    print("\n--- exit mix (k=2.0) ---")
    for v in ("base", "ratchet", "ladder"):
        df = results[(v, 2.0)]
        print(f"\n{names[v]}:")
        print(df.groupby("exit_type")["net_frac"].agg(["count", "mean", "sum"]))

    print("\n--- walk-forward folds (k=2.0) ---")
    for v in ("base", "ratchet", "ladder"):
        df = results[(v, 2.0)].copy()
        df["ts_parsed"] = pd.to_datetime(df["ts"])
        df["fold"] = pd.qcut(df["ts_parsed"].rank(method="first"), 3, labels=["fold1", "fold2", "fold3"])
        print(f"\n{names[v]}:")
        print(df.groupby("fold")["net_frac"].agg(["count", "mean", "sum"]).assign(
            mean_pct=lambda d: d["mean"] * 100))

    pd.concat([d.assign(variant=v, k=k) for (v, k), d in results.items()]).to_csv("ladder_exit_results.csv", index=False)
    print("\nsaved ladder_exit_results.csv")
