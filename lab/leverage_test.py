"""
leverage_test.py — 15x and 20x vs the current 10x, under the live machinery.
Engine: tp_sweep_multislot.run_arm (identical to the live exit stack: BE@50% of
TP, per-pair MAE walls, HARD_SL, 12% daily limit, DD wall + recovery sizing,
8 slots, cooldown). Module globals NOTIONAL_MULT / HARD_SL_FRAC are patched per
arm (read at call time).

Engine semantics (tick.py, verified 2026-09-13): per-slot notional =
balance x 0.85 x LEVERAGE / 8 (slot_cap binding) -> higher leverage = BIGGER
trades at the same margin budget. A priori HARD_SL per leverage (2026-09-10
lock rationale: SL inside the exchange liquidation, ~60% of liq distance):
10x->6% (liq ~9.5%), 15x->4% (liq ~6.2%), 20x->3% (liq ~4.75%). Without the
re-derivation, 20x + 6% SL = the exchange liquidates first (100% of slot
margin) — forbidden config.
"""
import numpy as np
import pandas as pd
import tp_sweep_multislot as T


def boot_ci(x, n_boot=10000, seed=17):
    rng = np.random.default_rng(seed)
    arr = np.asarray(x)
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_boot)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return lo, hi


print(f"{'LEV':>4} {'SL':>5} {'notional':>9} {'n':>5} {'WR':>6} {'acct/trade':>11} {'CI low':>9} {'CI high':>9} {'final$':>7} {'maxDD':>6}")
for lev, sl in [(10, 0.06), (15, 0.04), (20, 0.03)]:
    T.NOTIONAL_MULT = 0.85 / 8 * lev
    T.HARD_SL_FRAC = sl
    df, bal, dd = T.run_arm(1.6, 0.0006, 0.0001)
    mult = 0.85 / 8 * lev
    acct = (df["net_frac"] * mult)
    net = acct.mean() * 100
    ci = boot_ci(acct.tolist())
    wr = (df["net_frac"] > 0).mean()
    print(f"{lev:>4} {sl*100:>4.0f}% {mult:>8.3f}x {len(df):>5} {wr:>6.1%} {net:>+10.4f}% "
          f"{ci[0]*100:>+8.3f}% {ci[1]*100:>+8.3f}% {bal:>7.3f} {dd:>6.1%}")
    df["ts_parsed"] = pd.to_datetime(df["ts"])
    df["fold"] = pd.qcut(df["ts_parsed"].rank(method="first"), 3, labels=["f1", "f2", "f3"])
    folds = (df.groupby("fold")["net_frac"].mean() * mult) * 100
    print(f"      folds acct%/trade: " + "  ".join(f"{f}: {v:+.3f}%" for f, v in folds.items()))
    df.to_csv(f"lev{lev}.csv", index=False)

print("""
Risk anatomy per bad event (account % at $10):
 10x: NEAR wall kill -2.7% | 6% SL -6.4% | avg TP win +0.5%
 15x: NEAR wall kill -4.0% | 4% SL -6.4% | avg TP win +0.8%
 20x: NEAR wall kill -5.3% | 3% SL -6.4% | avg TP win +1.1%
Daily 12% limit: ~2 stops at 20x trip it (vs ~3 at 10x).""")
