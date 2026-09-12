"""
validate_v6.py — Monte Carlo survivability check. Transcribed verbatim from the
attached v6 dossier appendix, for independent verification.
"""
import numpy as np
import pandas as pd

N_PATHS = 5000
START_BALANCE = 10.0
NOTIONAL_MULT = 10.0 * 0.85 / 8


def run():
    df = pd.read_csv("results_v6.csv")
    fracs = df["net_frac"].to_numpy()
    n_trades = len(fracs)
    rng = np.random.default_rng(11)
    finals, max_dds = [], []
    for _ in range(N_PATHS):
        path = rng.choice(fracs, size=n_trades, replace=True)
        balance = START_BALANCE
        peak = balance
        max_dd = 0.0
        for f in path:
            balance += f * NOTIONAL_MULT * (balance / START_BALANCE)
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
            if balance <= 0:
                balance = 0
                break
        finals.append(balance)
        max_dds.append(max_dd)
    finals = np.array(finals)
    max_dds = np.array(max_dds)
    print(f"n_trades per path: {n_trades} paths: {N_PATHS}")
    print(f"Final balance — median: {np.median(finals):.3f} "
          f"5th pct: {np.percentile(finals,5):.3f} 95th pct: {np.percentile(finals,95):.3f}")
    print(f"P(ruin, balance<=0.5): {(finals<=0.5).mean():.1%}")
    print(f"Max drawdown — median: {np.median(max_dds):.1%} "
          f"75th pct: {np.percentile(max_dds,75):.1%} 95th pct: {np.percentile(max_dds,95):.1%}")


if __name__ == "__main__":
    run()
