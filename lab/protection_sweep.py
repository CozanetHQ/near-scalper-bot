"""
protection_sweep.py — PROFIT-CAPTURE / TP-PROTECTION engine study (owner spec 2026-09-13).

Question: when a trade has traveled X% of its TP distance, arm a profit floor at
(X - R)% so a reversal exits with a protected profit instead of scraping back
to BE_STOP. 40% activation is the OWNER HYPOTHESIS — NOT assumed optimal.

Matrix: activation X in {30,40,50,60,70}% x retracement allowance R in
{10,15,20,25,30}% of TP distance  =>  floor = (X-R)% of TP distance, clamped to
>= entry+costs (fee_buffer) so protection can NEVER guarantee a loss after costs.
Plus: two-stage 40->60 tighten variants, and the progressive RATCHET as reference.
Baseline = live exit stack today (BE at 50%, MAE walls, HARD_SL, age cap).

Decision rule (locked a priori, before results):
  GO  iff full-sample expectancy > baseline AND profit factor >= baseline AND
        maxDD <= baseline AND >= 2 of 3 time-folds beat baseline's same fold.
  Else NO-GO — feature does not ship.

Costs identical everywhere: taker 0.06%/side + 0.01% slip/side, 10x notional
(0.85/8 x 10 = 1.0625 account-mult per trade), 12% daily limit, DD-wall sizing.
"""
import json
import sys
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
BE_TRIGGER_FRAC = 0.5
FEE_BUFFER = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005

# ── shared, precomputed once ────────────────────────────────────────────────
FRAMES = {p: prep(p) for p in PAIRS}
TS_INDEX = {p: {t: i for i, t in enumerate(FRAMES[p]["ts"].tolist())} for p in PAIRS}
ALL_TS = sorted(set().union(*[set(FRAMES[p]["ts"].tolist()) for p in PAIRS]))
T0 = ALL_TS[0]
T1 = ALL_TS[len(ALL_TS) // 3]
T2 = ALL_TS[2 * len(ALL_TS) // 3]
FOLDS = {"f1": (T0, T1), "f2": (T1, T2), "f3": (T2, None)}

CONFIGS = []
for act in (0.30, 0.40, 0.50, 0.60, 0.70):
    for retr in (0.10, 0.15, 0.20, 0.25, 0.30):
        CONFIGS.append({"name": f"act{int(act*100)}_r{int(retr*100)}",
                        "act": act, "retr": retr, "two_stage": False})
# two-stage owner example: arm 40 -> floor (40-R); tighten at 60 -> floor (60-R)
for retr in (0.15, 0.25):
    CONFIGS.append({"name": f"2stage40_r{int(retr*100)}",
                    "act": 0.40, "retr": retr, "two_stage": True})
SHARDS = [CONFIGS[0:9], CONFIGS[9:19], CONFIGS[19:]]  # baseline+ratchet live in shard 0 extras


def floor_frac_of(cfg, tp_frac, stage):
    lvl = max(cfg["act"] - cfg["retr"], 0.0) if stage == 1 else max(0.60 - cfg["retr"], 0.0)
    f = lvl * tp_frac
    return max(f, FEE_BUFFER)  # NEVER below entry+costs


def run_sim(cfg, tp_mult=1.6, fee=0.0006, slip=0.0001):
    """cfg None => baseline (no protection). Returns (trades_df, final, maxdd)."""
    balance, peak, day_start = START_BALANCE, START_BALANCE, START_BALANCE
    cur_day = riskoff_day = None
    last_entry_ts = None
    positions, trades = [], []
    eq_chain = [balance]

    def close_pos(pos, exit_type, exit_frac, t, extra=None):
        nonlocal balance, peak
        net_frac = exit_frac - 2 * fee - 2 * slip
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "gross_frac": exit_frac, "exit_type": exit_type, "mfe": pos["mfe"],
                       "mae": pos["mae"], "tp_frac": pos["tp_frac"], "lev": 10,
                       "hold_min": (t - pos["entry_ts"]).total_seconds() / 60,
                       "regime": pos["regime"], "atr_frac": pos["atr_frac"],
                       "armed": bool(cfg and pos.get("armed")),
                       "post_arm_dd": (pos["mfe"] - (pos.get("mfe_at_arm") or pos["mfe"]))
                                       if (cfg and pos.get("armed")) else 0.0,
                       **(extra or {})})

    for t in ALL_TS:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day, day_start = day, balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day

        for pos in list(positions):
            p = pos["pair"]
            i = TS_INDEX[p].get(t)
            if i is None:
                continue
            row = FRAMES[p].iloc[i]
            e, d = pos["entry_price"], pos["direction"]
            if d == 1:
                adverse, favorable = (e - row["low"]) / e, (row["high"] - e) / e
            else:
                adverse, favorable = (row["high"] - e) / e, (e - row["low"]) / e
            pos["mfe"] = max(pos["mfe"], favorable)
            pos["mae"] = max(pos["mae"], adverse)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            if adverse >= wall:
                close_pos(pos, "MAE_KILL", -wall, t); positions.remove(pos); continue
            if adverse >= HARD_SL_FRAC:
                close_pos(pos, "HARD_SL", -HARD_SL_FRAC, t); positions.remove(pos); continue
            # ── protection state machine ──
            if cfg is not None and not pos.get("armed") and pos["mfe"] >= cfg["act"] * pos["tp_frac"]:
                pos["armed"] = True
                pos["mfe_at_arm"] = pos["mfe"]
                pos["floor"] = floor_frac_of(cfg, pos["tp_frac"], 1)
            if cfg is not None and cfg["two_stage"] and pos.get("armed") \
                    and pos["mfe"] >= 0.60 * pos["tp_frac"]:
                pos["floor"] = floor_frac_of(cfg, pos["tp_frac"], 2)
            if cfg is not None and pos.get("armed") and favorable <= pos["floor"]:
                close_pos(pos, "PROTECTED", pos["floor"], t); positions.remove(pos); continue
            if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                pos["be_armed"] = True
            if pos["be_armed"] and favorable <= FEE_BUFFER:
                close_pos(pos, "BE_STOP", FEE_BUFFER, t); positions.remove(pos); continue
            if favorable >= pos["tp_frac"]:
                close_pos(pos, "TP", pos["tp_frac"], t); positions.remove(pos); continue
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                close_pos(pos, "MAX_AGE", (row["close"] - e) / e * d, t); positions.remove(pos)

        for p in PAIRS:
            if len(positions) >= MAX_POSITIONS or riskoff_day is not None:
                break
            i = TS_INDEX[p].get(t)
            if i is None or i < 30:
                continue
            row = FRAMES[p].iloc[i]
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
                              "tp_frac": tp_frac, "mfe": 0.0, "mae": 0.0, "be_armed": False,
                              "armed": False, "floor": None, "mfe_at_arm": None,
                              "entry_ts": t, "sizing": sizing,
                              "regime": row["regime"], "atr_frac": atr / price})
            last_entry_ts = t

    for pos in positions:
        lr = FRAMES[pos["pair"]].iloc[-1]
        close_pos(pos, "EOD", (lr["close"] - pos["entry_price"]) / pos["entry_price"] * pos["direction"],
                  FRAMES[pos["pair"]]["ts"].iloc[-1])
    run_peak = eq_chain[0]; maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    df = pd.DataFrame(trades)
    df["ts"] = pd.to_datetime(df["ts"])
    return df, balance, maxdd


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x, float)
    if len(arr) < 5:
        return (float("nan"), float("nan"))
    means = rng.choice(arr, (n_boot, len(arr))).mean(axis=1)
    return tuple(np.percentile(means, [2.5, 97.5]))


def fold_of(ts):
    for k, (a, b) in FOLDS.items():
        if ts >= a and (b is None or ts < b):
            return k
    return "f3"


def metrics(df, final, maxdd, base_exp_by_fold=None):
    net = df["net_frac"]
    acct = net * NOTIONAL_MULT
    wins = net[net > 0]; losses = net[net <= 0]
    gp = wins.sum() * NOTIONAL_MULT if len(wins) else 0.0
    gl = abs(losses.sum() * NOTIONAL_MULT) if len(losses) else 0.0
    tp = int((df["exit_type"] == "TP").sum())
    be = int((df["exit_type"] == "BE_STOP").sum())
    mk = int(df["exit_type"].isin(["MAE_KILL", "HARD_SL"]).sum())
    prot = int((df["exit_type"] == "PROTECTED").sum())
    armed = int(df["armed"].sum())
    armed_tp = int(((df["exit_type"] == "TP") & df["armed"]).sum())
    by = {}
    if base_exp_by_fold is not None:
        pass
    m = {
        "n": len(df), "tp": tp, "tp_rate": tp / max(len(df), 1),
        "protected": prot, "protected_rate": prot / max(len(df), 1),
        "be": be, "maekill": mk, "armed": armed,
        "armed_then_tp_pct": (armed_tp / armed) if armed else None,
        "wr": (net > 0).mean(),
        "gross_profit": round(gp, 4), "gross_loss": round(-gl, 4),
        "net": round(net.sum() * NOTIONAL_MULT, 4),
        "pf": round(gp / gl, 3) if gl > 0 else None,
        "exp_trade": round(net.mean(), 6),
        "exp_trade_ci": [round(v, 6) for v in boot_ci(net.tolist())],
        "avg_mfe": round(df["mfe"].mean(), 6), "avg_mae": round(df["mae"].mean(), 6),
        "avg_hold": round(df["hold_min"].mean(), 1),
        "maxdd": round(maxdd, 4), "final": round(final, 3),
        "fees_paid": round(len(df) * 2 * (FEE_RATE + SLIP_ASSUMED_PCT) * NOTIONAL_MULT, 4),
        "post_arm_dd_mean": round(df.loc[df["armed"], "post_arm_dd"].mean(), 6) if armed else None,
    }
    for k, (a, b) in FOLDS.items():
        sub = df[(df["ts"] >= a) & ((df["ts"] < b) if b is not None else True)]
        m[f"exp_{k}"] = round(sub["net_frac"].mean(), 6) if len(sub) else None
    return m


def cohort_analysis(df):
    """Behavioral cohorts: eventual TP vs BE vs kills; regime; ATR tercile."""
    out = {}
    for et in ("TP", "BE_STOP", "MAE_KILL", "PROTECTED"):
        sub = df[df["exit_type"] == et]
        if len(sub) == 0:
            continue
        prog = sub["mfe"] / sub["tp_frac"]          # MFE as % of TP distance
        out[et] = {"n": len(sub), "mean_mfe_tp_progress": round(prog.mean(), 3),
                   "p75_mfe_progress": round(prog.quantile(0.75), 3),
                   "armed_pct": round(sub["armed"].mean(), 3),
                   "net": round(sub["net_frac"].sum() * NOTIONAL_MULT, 4)}
    for reg in ("TREND", "CHOP"):
        sub = df[df["regime"] == reg]
        if len(sub) >= 10:
            out[f"regime_{reg}"] = {"n": len(sub), "exp": round(sub["net_frac"].mean(), 6)}
    q1, q2 = df["atr_frac"].quantile([0.33, 0.67])
    for lbl, sub in (("atr_lo", df[df["atr_frac"] <= q1]),
                     ("atr_mid", df[(df["atr_frac"] > q1) & (df["atr_frac"] <= q2)]),
                     ("atr_hi", df[df["atr_frac"] > q2])):
        if len(sub) >= 10:
            out[lbl] = {"n": len(sub), "exp": round(sub["net_frac"].mean(), 6)}
    return out


def run_ratchet():
    """Progressive ratchet reference (§9-10 best exit variant): floor = 50% of
    the highest MFE level once past 50% of TP; strictly better-ordered floors."""
    # Reuse run_sim with a trick: ratchet = floor follows mfe*0.5 once armed at 50%.
    # Implemented directly for clarity:
    balance, peak, day_start = START_BALANCE, START_BALANCE, START_BALANCE
    cur_day = riskoff_day = None
    last_entry_ts = None
    positions, trades = [], []
    eq_chain = [balance]

    def close_pos(pos, exit_type, exit_frac, t):
        nonlocal balance, peak
        net_frac = exit_frac - 2 * FEE_RATE - 2 * SLIP_ASSUMED_PCT
        balance *= (1 + net_frac * NOTIONAL_MULT * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "gross_frac": exit_frac, "exit_type": exit_type, "mfe": pos["mfe"],
                       "mae": pos["mae"], "tp_frac": pos["tp_frac"], "lev": 10,
                       "hold_min": (t - pos["entry_ts"]).total_seconds() / 60,
                       "regime": pos["regime"], "atr_frac": pos["atr_frac"],
                       "armed": pos["armed"], "post_arm_dd": (pos["mfe"] - pos["mfe_at_arm"]) if pos["armed"] else 0.0})

    for t in ALL_TS:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day, day_start = day, balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day
        for pos in list(positions):
            p = pos["pair"]
            i = TS_INDEX[p].get(t)
            if i is None:
                continue
            row = FRAMES[p].iloc[i]
            e, d = pos["entry_price"], pos["direction"]
            if d == 1:
                adverse, favorable = (e - row["low"]) / e, (row["high"] - e) / e
            else:
                adverse, favorable = (row["high"] - e) / e, (e - row["low"]) / e
            pos["mfe"] = max(pos["mfe"], favorable)
            pos["mae"] = max(pos["mae"], adverse)
            wall = MAE_WALL.get(p, MAE_WALL_DEFAULT)
            if adverse >= wall:
                close_pos(pos, "MAE_KILL", -wall, t); positions.remove(pos); continue
            if adverse >= HARD_SL_FRAC:
                close_pos(pos, "HARD_SL", -HARD_SL_FRAC, t); positions.remove(pos); continue
            if not pos["armed"] and pos["mfe"] >= 0.50 * pos["tp_frac"]:
                pos["armed"] = True
                pos["mfe_at_arm"] = pos["mfe"]
            if pos["armed"]:
                pos["floor"] = max(0.5 * pos["mfe"], FEE_BUFFER)
                if favorable <= pos["floor"]:
                    close_pos(pos, "PROTECTED", pos["floor"], t); positions.remove(pos); continue
            if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                pos["be_armed"] = True
            if pos["be_armed"] and favorable <= FEE_BUFFER:
                close_pos(pos, "BE_STOP", FEE_BUFFER, t); positions.remove(pos); continue
            if favorable >= pos["tp_frac"]:
                close_pos(pos, "TP", pos["tp_frac"], t); positions.remove(pos); continue
            if (t - pos["entry_ts"]).total_seconds() * 1000 >= MAX_AGE_MS:
                close_pos(pos, "MAX_AGE", (row["close"] - e) / e * d, t); positions.remove(pos)
        for p in PAIRS:
            if len(positions) >= MAX_POSITIONS or riskoff_day is not None:
                break
            i = TS_INDEX[p].get(t)
            if i is None or i < 30:
                continue
            row = FRAMES[p].iloc[i]
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
            tp_frac = max(1.6 * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": 1 if want == "long" else -1,
                              "tp_frac": tp_frac, "mfe": 0.0, "mae": 0.0, "be_armed": False,
                              "armed": False, "mfe_at_arm": 0.0,
                              "entry_ts": t, "sizing": sizing,
                              "regime": row["regime"], "atr_frac": atr / price})
            last_entry_ts = t
    for pos in positions:
        lr = FRAMES[pos["pair"]].iloc[-1]
        close_pos(pos, "EOD", (lr["close"] - pos["entry_price"]) / pos["entry_price"] * pos["direction"],
                  FRAMES[pos["pair"]]["ts"].iloc[-1])
    run_peak = eq_chain[0]; maxdd = 0.0
    for v in eq_chain:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            maxdd = max(maxdd, 1 - v / run_peak)
    df = pd.DataFrame(trades)
    df["ts"] = pd.to_datetime(df["ts"])
    return df, balance, maxdd


if __name__ == "__main__":
    shard = int(sys.argv[1])
    results = []
    if shard == 0:
        df, final, dd = run_sim(None)
        df.to_csv("prot_baseline_trades.csv", index=False)
        results.append({"name": "BASELINE", "m": metrics(df, final, dd),
                        "cohorts": cohort_analysis(df)})
    for cfg in SHARDS[shard]:
        df, final, dd = run_sim(cfg)
        df.to_csv(f"prot_trades_{cfg['name']}.csv", index=False)
        results.append({"name": cfg["name"], "m": metrics(df, final, dd),
                        "cohorts": cohort_analysis(df)})
        print(f"shard{shard}: {cfg['name']} done exp={results[-1]['m']['exp_trade']}", flush=True)
    if shard == 0:
        # ratchet reference: arm 50->25, 75->50, 100->75 (progressive, not static)
        df, final, dd = run_ratchet()
        results.append({"name": "RATCHET_REF", "m": metrics(df, final, dd),
                        "cohorts": cohort_analysis(df)})
    with open(f"protection_shard{shard}.json", "w") as f:
        json.dump(results, f, indent=1)
    print(f"shard{shard} COMPLETE — {len(results)} configs", flush=True)
