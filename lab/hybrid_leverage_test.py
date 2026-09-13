"""
hybrid_leverage_test.py — OWNER DESIGN 2026-09-13: hybrid leverage by pair x time zone.

Owner spec (verbatim intent):
  1. A pair that does bad in ALL time zones -> 5x leverage everywhere.
  2. The existing per-pair confidence (EV gate) keeps doing its job — unchanged.
  3. Leverage is hybrid per time zone: 5x / 10x / 15x / 20x.
  4. If a zone that was doing good starts doing bad, the bot switches tiers.

Rails added on top (a priori, locked before running — the falsification bar):
  R1. PROMOTION GATES: a (pair, 3h bucket) may hold 15x only if its 95% CI is
      entirely above zero in the assignment window; 20x additionally requires
      mean >= +0.10%/trade. Unproven buckets stay 10x. Proven-bad buckets (CI
      entirely below zero) drop to 5x. Nothing is promoted on hope.
  R2. PER-LEVERAGE HARD_SL (liquidation safety, 0.63x-liq rule from the
      2026-09-10 lock rationale): 5x->12% | 10x->6% | 15x->4% | 20x->3%.
      MAE walls per pair (2.5% NEAR / 4% others) unchanged — they fire first.
  R3. ASSIGNMENT IS OUT-OF-SAMPLE: tiers are computed from days 1-20 of the
      flat-10x reference run (lev10.csv); the hybrid is then evaluated on days
      21-30 only, against flat-10x trades from the same hold-out window.
  R4. Daily 12% limit, DD wall + recovery sizing, 8 slots, cooldown: unchanged.

Live re-evaluation rule (owner point 4, for the engine version): trailing 7-day
window, recomputed daily; a tier change requires TWO consecutive evaluations
agreeing (hysteresis, no flapping).
"""
import numpy as np
import pandas as pd

from multislot_ablation import (prep, PAIRS, FEE_RATE, SLIP_ASSUMED_PCT, ATR_MIN,
                                MAX_POSITIONS, PAIR_CAP, PAIR_CAP_DEFAULT,
                                COOLDOWN_MS, DAILY_LOSS_LIMIT, DD_WALL,
                                RECOVERY_SIZE_FRAC, START_BALANCE, MAX_AGE_MS,
                                MAE_WALL, MAE_WALL_DEFAULT, SPIKE_RANGE_MULT,
                                MIN_TP_DIST_FRAC, BE_TRIGGER_FRAC)

LEV_TIERS = [5, 10, 15, 20]
HARD_SL_BY_LEV = {5: 0.12, 10: 0.06, 15: 0.04, 20: 0.03}
REF_MULT = 0.85 / 8 * 10          # flat 10x reference exposure
ASSIGN_END = "2026-09-08"          # day 20 boundary of the reference window (locked a priori)


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x)
    if len(arr) < 5:
        return (-0.5, 0.5)  # not enough data -> cannot prove anything -> 10x
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi


def build_tier_table():
    """Assign a leverage tier to every (pair, 3h bucket) from the reference run."""
    ref = pd.read_csv("lev10.csv")
    ref["ts"] = pd.to_datetime(ref["ts"])
    ref["acct"] = ref["net_frac"] * REF_MULT
    ref["bucket"] = ref["ts"].dt.hour // 3
    assign = ref[ref["ts"] < ASSIGN_END]

    table = {}
    pair_verdicts = {}
    for p in PAIRS:
        d = assign[assign["pair"] == p]["acct"]
        lo, hi = boot_ci(d.tolist())
        pair_bad = len(d) >= 30 and hi < 0          # proven bad in ALL zones -> 5x everywhere
        pair_verdicts[p] = ("BAD-EVERYWHERE" if pair_bad else "mixed")
        for b in range(8):
            db = assign[(assign["pair"] == p) & (assign["bucket"] == b)]["acct"]
            if pair_bad:
                table[(p, b)] = 5
                continue
            if len(db) < 30:                        # too little data -> stay at 10x
                table[(p, b)] = 10
                continue
            lo, hi = boot_ci(db.tolist())
            m = db.mean()
            if hi < 0:                              # proven bad bucket -> 5x
                table[(p, b)] = 5
            elif lo > 0 and m >= 0.0010:            # proven good & strong -> 20x
                table[(p, b)] = 20
            elif lo > 0:                            # proven good -> 15x
                table[(p, b)] = 15
            else:
                table[(p, b)] = 10
    return table, pair_verdicts


def run_hybrid(tp_mult=1.6, fee=0.0006, slip=0.0001):
    table, _ = build_tier_table()
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
        balance *= (1 + net_frac * pos["mult"] * pos["sizing"])
        peak = max(peak, balance)
        eq_chain.append(balance)
        trades.append({"pair": pos["pair"], "ts": str(t), "net_frac": net_frac,
                       "exit_type": exit_type, "lev": pos["lev"], "acct": net_frac * pos["mult"]})

    for t in all_ts:
        day = t.strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day, day_start = day, balance
            if riskoff_day is not None and day > riskoff_day:
                riskoff_day = None
        if riskoff_day is None and balance <= day_start * (1 - DAILY_LOSS_LIMIT):
            riskoff_day = day

        for pos in list(positions):
            p, i = pos["pair"], ts_index[pos["pair"]].get(t)
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
            if adverse >= HARD_SL_BY_LEV[pos["lev"]]:
                close_pos(pos, "HARD_SL", -HARD_SL_BY_LEV[pos["lev"]], t); positions.remove(pos); continue
            if not pos["be_armed"] and pos["mfe"] >= BE_TRIGGER_FRAC * pos["tp_frac"]:
                pos["be_armed"] = True
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
            lev = table[(p, t.hour // 3)]
            tp_frac = max(tp_mult * atr / price, MIN_TP_DIST_FRAC)
            sizing = RECOVERY_SIZE_FRAC if balance < peak * (1 - DD_WALL) else 1.0
            positions.append({"pair": p, "entry_price": price, "direction": 1 if want == "long" else -1,
                              "tp_frac": tp_frac, "mfe": 0.0, "be_armed": False, "entry_ts": t,
                              "sizing": sizing, "lev": lev, "mult": 0.85 / 8 * lev})
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
    return pd.DataFrame(trades), balance, maxdd, table


if __name__ == "__main__":
    table, pair_verdicts = build_tier_table()
    print("=== TIER TABLE (assigned from days 1-20, locked before the hold-out run) ===")
    print(f"pair verdicts: {pair_verdicts}")
    for p in PAIRS:
        row = [f"{table[(p,b)]}x" for b in range(8)]
        print(f"{p:9s} " + " | ".join(f"{b*3:02d}-{b*3+2:02d}h:{r}" for b, r in enumerate(row)))
    tier_counts = pd.Series([table[k] for k in table]).value_counts().sort_index()
    print("tier usage across 40 cells:", dict(tier_counts))

    df, bal, dd, _ = run_hybrid()
    df["ts"] = pd.to_datetime(df["ts"])
    holdout = df[df["ts"] >= ASSIGN_END]

    ref = pd.read_csv("lev10.csv")
    ref["ts"] = pd.to_datetime(ref["ts"])
    ref_hold = ref[ref["ts"] >= ASSIGN_END]
    ref_hold_acct = ref_hold["net_frac"] * REF_MULT

    print(f"\n=== HOLD-OUT EVALUATION (days 21-30, out-of-sample) ===")
    for label, acct in [("flat 10x  ", ref_hold_acct), ("HYBRID    ", holdout["acct"])]:
        lo, hi = boot_ci(acct.tolist())
        print(f"{label} n={len(acct):4d}  mean {acct.mean()*100:+.4f}%/trade  CI [{lo*100:+.3f}%, {hi*100:+.3f}%]  sum {acct.sum()*100:+.2f}%")
    lo, hi = boot_ci((holdout['acct'] - 0).tolist())
    diff = holdout["acct"].mean() - ref_hold_acct.mean()
    print(f"hybrid minus flat (per trade): {diff*100:+.4f}%")
    print(f"hybrid full-run: final ${bal:.2f}, maxDD {dd:.1%}   (flat-10x full-run reference: $6.52, 40.7%)")
    print("\nhold-out exit mix:")
    print(holdout.groupby("exit_type")["acct"].agg(["count", "mean", "sum"]).round(5))
    holdout.to_csv("hybrid_holdout.csv", index=False)
