"""
maker_check.py — verify the ratchet exit under maker execution costs.
Patches the module fee constants (call-time globals) and reruns the ratchet arm.
Scenarios: maker 0.02%+0.01% slip; stress 0.02%+0.03% slip (the 3x slip trap).
"""
import numpy as np
import pandas as pd
import ladder_exit_sweep as L


def run(fee, slip, k=1.6):
    L.FEE_RATE = fee
    L.SLIP_ASSUMED_PCT = slip
    L.BE_BUFFER = 2 * fee + 2 * slip + 0.0005
    df, bal, dd = L.run_arm(k, "ratchet")
    net = df["net_frac"].mean() * 100
    ci = L.boot_ci(df["net_frac"].tolist())
    df["ts_parsed"] = pd.to_datetime(df["ts"])
    df["fold"] = pd.qcut(df["ts_parsed"].rank(method="first"), 3, labels=["f1", "f2", "f3"])
    folds = df.groupby("fold")["net_frac"].mean() * 100
    print(f"ratchet k={k} fee={fee*100:.2f}% slip={slip*100:.2f}%: "
          f"n={len(df)} net={net:+.4f}%/trade CI[{ci[0]*100:+.3f}%,{ci[1]*100:+.3f}%] "
          f"final=${bal:.3f} maxDD={dd:.1%}")
    print(f"   folds: " + "  ".join(f"{f}: {v:+.3f}%" for f, v in folds.items()))


print("=== RATCHET EXIT + MAKER EXECUTION (the candidate combination) ===")
run(0.0002, 0.0001)   # maker fills, normal slip
run(0.0002, 0.0003)   # maker, 3x slip stress (falsification-bar trap)
run(0.0002, 0.0)      # maker, zero slip (theoretical ceiling of this lever)
