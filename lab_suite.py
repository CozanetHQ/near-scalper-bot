"""Scalper v5 — Laboratory Suite (Phase 2b validation).

Runs the 5 labs described in research_second_engine_spec.md /
Scalper_v5_Laboratory_Suite.pdf against real historical candles.
Uses the SAME locked constants as the live engine (tick.py) for TP
distance, HARD_SL, MAE_CEIL, ATR period, spike multiplier — the labs
measure the Second Engine against the Main Engine's actual physics,
not an approximation.

Usage:
    python3 lab_suite.py --pairs NEARUSDT,BTCUSDT,... --out lab_signals.csv
"""
import argparse, csv, json, sys
from engine.features import compute_atr, compute_ema, fractal_swings, candle_color
from engine.second_engine import score_signal

ATR_PERIOD = 14
SCALP_TP_ATR = 1.6
MIN_TP_DIST_FRAC = 0.006
HARD_SL_FRAC = 0.06
MAE_CEIL_FRAC = 0.04
SPIKE_RANGE_MULT = 3.0
EMA_FAST, EMA_SLOW = 9, 21
FORWARD_MAX_BARS = 240  # 1m bars to look ahead (4h) before calling a signal "timed out"


def resample_15m(c1m):
    out = []
    for i in range(0, len(c1m) - 15 + 1, 15):
        chunk = c1m[i:i + 15]
        out.append({
            "ts": chunk[0]["ts"], "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk), "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"], "vol": sum(c.get("vol", 0) for c in chunk),
        })
    return out


def main_entry_proxy(c1m, i, ema_f_series, ema_s_series):
    """Lightweight G1+G5 proxy (per the owner doc §4): pullback candle against
    the live momentum candle, with EMA-fast slope confirming direction.
    Does NOT reimplement the full 9-gate live engine — just generates
    realistic candidate moments."""
    if i < EMA_SLOW + 3 or i >= len(c1m) - 1:
        return None
    cur, prev = c1m[i], c1m[i - 1]
    ef, ef_prev = ema_f_series[i], ema_f_series[i - 3]
    if ef is None or ef_prev is None:
        return None
    cur_color, prev_color = candle_color(cur), candle_color(prev)
    if prev_color == "bull" and cur_color == "bear" and ef > ef_prev:
        return "long"   # pullback against an up-move, EMA still sloping up
    if prev_color == "bear" and cur_color == "bull" and ef < ef_prev:
        return "short"
    return None


def forward_path(c1m, i, side, entry, tp_dist, atr):
    """Walk forward until TP, HARD_SL, MAE_KILL-equivalent, or timeout.
    Returns (reason, mfe_frac, mae_frac, bars_held)."""
    mfe, mae = 0.0, 0.0
    tp = entry + tp_dist if side == "long" else entry - tp_dist
    hard_sl_dist = entry * HARD_SL_FRAC
    for k in range(1, min(FORWARD_MAX_BARS, len(c1m) - i - 1) + 1):
        bar = c1m[i + k]
        fav = (bar["high"] - entry) / entry if side == "long" else (entry - bar["low"]) / entry
        adv = (entry - bar["low"]) / entry if side == "long" else (bar["high"] - entry) / entry
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        hit_tp = bar["high"] >= tp if side == "long" else bar["low"] <= tp
        hit_hsl = adv >= HARD_SL_FRAC
        hit_mae_kill = mae >= MAE_CEIL_FRAC
        if hit_tp:
            return "TP", mfe, mae, k
        if hit_hsl:
            return "HARD_SL", mfe, mae, k
        if hit_mae_kill:
            return "MAE_KILL", mfe, mae, k
    return "TIMEOUT", mfe, mae, k if 'k' in dir() else 0


def collect_signals(pair, c1m):
    closes = [c["close"] for c in c1m]
    # incremental EMA — O(n), NOT compute_ema() recomputed from scratch per index
    ema_f_series = [None] * len(closes)
    k = 2.0 / (EMA_FAST + 1)
    if len(closes) >= EMA_FAST:
        e = sum(closes[:EMA_FAST]) / EMA_FAST
        ema_f_series[EMA_FAST - 1] = e
        for j in range(EMA_FAST, len(closes)):
            e = closes[j] * k + e * (1 - k)
            ema_f_series[j] = e
    c15 = resample_15m(c1m)
    idx15_of_1m = [min(j // 15, len(c15) - 1) for j in range(len(c1m))]

    signals = []
    for i in range(ATR_PERIOD + EMA_SLOW + 5, len(c1m) - FORWARD_MAX_BARS - 1, 3):  # every 3rd bar, enough candidates without over-sampling
        window = c1m[max(0, i - ATR_PERIOD - 1):i + 1]
        atr = compute_atr(window, ATR_PERIOD)
        if not atr:
            continue
        side = main_entry_proxy(c1m, i, ema_f_series, ema_f_series)
        if not side:
            continue
        price = c1m[i]["close"]
        forming_range = c1m[i]["high"] - c1m[i]["low"]
        tp_dist = max(SCALP_TP_ATR * atr, MIN_TP_DIST_FRAC * price)
        j15 = idx15_of_1m[i]
        closed15 = c15[:j15 + 1]
        if len(closed15) < 10:
            continue
        se = score_signal(closed15, side, price, atr, tp_dist, forming_range)
        reason, mfe, mae, bars = forward_path(c1m, i, side, price, tp_dist, atr)
        tp_frac = tp_dist / price
        main_correct = reason == "TP"
        second_pred_success = se["overall_score"] > 0.0
        second_correct = second_pred_success == main_correct
        signals.append({
            "pair": pair, "i": i, "side": side, "price": price, "atr": atr,
            "tp_dist": tp_dist, "tp_frac": round(tp_frac, 6),
            **se,
            "reason": reason, "mfe_frac": round(mfe, 6), "mae_frac": round(mae, 6),
            "bars_held": bars, "main_correct": main_correct, "second_correct": second_correct,
        })
    return signals


def lab1_structure_quality(sig):
    print("\n=== LAB 1 — Structure Quality ranking ===")
    buckets = {}
    for s in sig:
        buckets.setdefault(s["structure_quality"], []).append(s)
    print(f"{'sq':>6} {'n':>6} {'tp_hit%':>8} {'mae_kill%':>10} {'avg_mfe%':>9} {'avg_mae%':>9} {'simple_ev':>10}")
    for k in sorted(buckets):
        rows = buckets[k]
        n = len(rows)
        tp_hit = 100 * sum(1 for r in rows if r["reason"] == "TP") / n
        mk = 100 * sum(1 for r in rows if r["reason"] == "MAE_KILL") / n
        avg_mfe = 100 * sum(r["mfe_frac"] for r in rows) / n
        avg_mae = 100 * sum(r["mae_frac"] for r in rows) / n
        ev = sum((r["tp_frac"] if r["reason"] == "TP" else -r["mae_frac"]) for r in rows) / n
        print(f"{k:>6.2f} {n:>6} {tp_hit:>8.1f} {mk:>10.1f} {avg_mfe:>9.2f} {avg_mae:>9.2f} {ev:>10.5f}")


def lab2_move_potential(sig):
    print("\n=== LAB 2 — Move Potential ranking ===")
    buckets = {}
    for s in sig:
        buckets.setdefault(s["move_potential"], []).append(s)
    print(f"{'mp':>6} {'n':>6} {'tp_hit%':>8} {'mae_kill%':>10} {'avg_wall_ratio':>15}")
    for k in sorted(buckets):
        rows = buckets[k]
        n = len(rows)
        tp_hit = 100 * sum(1 for r in rows if r["reason"] == "TP") / n
        mk = 100 * sum(1 for r in rows if r["reason"] == "MAE_KILL") / n
        ratios = [r["wall_ratio"] for r in rows if r["wall_ratio"] is not None]
        avg_r = sum(ratios) / len(ratios) if ratios else float("nan")
        print(f"{k:>6.2f} {n:>6} {tp_hit:>8.1f} {mk:>10.1f} {avg_r:>15.2f}")


def lab3_spike_alignment(sig):
    print("\n=== LAB 3 — Spike Risk Alignment ===")
    buckets = {}
    for s in sig:
        buckets.setdefault(s["spike_bucket"], []).append(s)
    print(f"{'bucket':>8} {'n':>6} {'tp_hit%':>8} {'mae_kill%':>10} {'avg_mae%':>9}")
    for k in ["low", "medium", "high"]:
        rows = buckets.get(k, [])
        if not rows:
            continue
        n = len(rows)
        tp_hit = 100 * sum(1 for r in rows if r["reason"] == "TP") / n
        mk = 100 * sum(1 for r in rows if r["reason"] == "MAE_KILL") / n
        avg_mae = 100 * sum(r["mae_frac"] for r in rows) / n
        print(f"{k:>8} {n:>6} {tp_hit:>8.1f} {mk:>10.1f} {avg_mae:>9.2f}")


def lab4_agreement_matrix(sig):
    print("\n=== LAB 4 — Agreement Matrix ===")
    cells = {"both_correct": 0, "main_right_second_wrong": 0, "main_wrong_second_right": 0, "both_wrong": 0}
    for s in sig:
        m, sc = s["main_correct"], s["second_correct"]
        if m and sc:
            cells["both_correct"] += 1
        elif m and not sc:
            cells["main_right_second_wrong"] += 1
        elif not m and sc:
            cells["main_wrong_second_right"] += 1
        else:
            cells["both_wrong"] += 1
    n = len(sig) or 1
    for k, v in cells.items():
        print(f"  {k:>28}: {v:>6}  ({100*v/n:.1f}%)")
    return cells


def lab5_joint_ev_impact(sig):
    print("\n=== LAB 5 — Joint EV Impact (simulation only, NOT live) ===")
    def pnl(r):
        return r["tp_frac"] if r["reason"] == "TP" else (-r["mae_frac"] if r["reason"] in ("MAE_KILL", "HARD_SL") else 0.0)
    base_total = sum(pnl(r) for r in sig)
    def mod(score):
        if score < -0.6:
            return 0.5
        if score > 0.6:
            return 1.2
        return 1.0
    sim_total = sum(pnl(r) * mod(r["overall_score"]) for r in sig)
    n = len(sig) or 1
    print(f"  baseline total pnl-proxy : {base_total:+.5f}  (avg {base_total/n:+.6f})")
    print(f"  modulated total pnl-proxy: {sim_total:+.5f}  (avg {sim_total/n:+.6f})")
    print(f"  difference               : {sim_total-base_total:+.5f}  ({'ENCOURAGING' if sim_total>base_total else 'NO IMPROVEMENT'} — advisory only, needs OOS + registry lock)")


def run(pairs, out_csv, labs):
    all_sig = []
    for pair in pairs:
        fname = f"data_{pair}_30d.json"
        try:
            c1m = json.load(open(fname))
        except FileNotFoundError:
            print(f"  [skip] {fname} not found")
            continue
        print(f"collecting signals: {pair} ({len(c1m)} 1m candles)...")
        sig = collect_signals(pair, c1m)
        print(f"  -> {len(sig)} candidate signals")
        all_sig.extend(sig)

    print(f"\nTOTAL candidate signals across all pairs: {len(all_sig)}")
    if "1" in labs: lab1_structure_quality(all_sig)
    if "2" in labs: lab2_move_potential(all_sig)
    if "3" in labs: lab3_spike_alignment(all_sig)
    if "4" in labs: lab4_agreement_matrix(all_sig)
    if "5" in labs: lab5_joint_ev_impact(all_sig)

    if out_csv and all_sig:
        keys = list(all_sig[0].keys())
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(all_sig)
        print(f"\nsaved {len(all_sig)} signals -> {out_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="NEARUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")
    ap.add_argument("--out", default="lab_suite_signals.csv")
    ap.add_argument("--labs", default="1,2,3,4,5")
    args = ap.parse_args()
    run(args.pairs.split(","), args.out, args.labs.split(","))
