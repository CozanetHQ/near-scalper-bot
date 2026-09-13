"""
sweep_reclaim_analysis.py — gate evaluation for the sweep-reclaim premise test.

Per variant x hypothesis (collapse >=60 bars per pair/variant/dir/hyp):
  n, best grid combo by CI_lo, exp/CI, per-pair exp on that combo, per-fold,
  drop-max-pair exp, maker-aux exp, MFE/MAE asymmetry, 30/60/240-bar travel.

GATES (locked a priori): CI_lo>0 AND n>=300 AND >=4/5 pairs positive AND
>=2/3 folds positive AND freq>=10/pair/month AND drop-max-pair exp>0.
"""
import numpy as np
import pandas as pd

FEE_COST = 0.0014
T0 = pd.Timestamp("2026-08-12 12:13:00+00:00")
CUTS = (pd.Timestamp("2026-08-22 12:13:00+00:00"),
        pd.Timestamp("2026-09-01 12:13:00+00:00"))
COMBOS = [f"t{t}_s{s}" for t in (0.6, 1.0, 1.6) for s in (1.0, 1.5, 2.0)]

df = pd.read_csv("sweep_reclaim_raw.csv")
df["ts"] = T0 + pd.to_timedelta(df["i"], unit="m")

# run-collapse per (pair, variant, hyp, sweep, dir)
df = df.sort_values(["pair", "variant", "hyp", "sweep", "dir", "i"]).reset_index(drop=True)
keep, last = [], {}
for _, r in df.iterrows():
    k = (r["pair"], r["variant"], r["hyp"], r["sweep"], r["dir"])
    if k not in last or r["i"] - last[k] >= 60:
        keep.append(True)
        last[k] = r["i"]
    else:
        keep.append(False)
df = df[keep].reset_index(drop=True)
print(f"collapsed events: {len(df)}")


def boot_lo(x, nb=5000, seed=17):
    if len(x) < 20:
        return float("nan")
    rng = np.random.default_rng(seed)
    m = rng.choice(x, (nb, len(x))).mean(axis=1)
    return float(np.percentile(m, 5))


print(f"\n{'variant':12s} {'hyp':12s} {'n':>5s} {'best combo':>11s} {'exp%':>7s} {'CIlo%':>7s} "
      f"{'pairs+':>6s} {'folds+':>6s} {'dropmax%':>8s} {'maker%':>7s} {'mfe%':>6s} {'mae%':>6s} {'ret30%':>7s} {'ret240%':>8s} GATE")
results = []
for (v, h), g in df.groupby(["variant", "hyp"]):
    best = None
    for c in COMBOS:
        x = g[c].to_numpy()
        ci = boot_lo(x)
        if best is None or ci > best[0]:
            best = (ci, c, x.mean())
    ci, col, exp = best
    # pair breadth
    pp = {p: (gg[col].mean(), len(gg)) for p, gg in g.groupby("pair")}
    pairs_pos = sum(1 for e, _ in pp.values() if e > 0)
    # folds
    f1 = g[g["ts"] < CUTS[0]][col]
    f2 = g[(g["ts"] >= CUTS[0]) & (g["ts"] < CUTS[1])][col]
    f3 = g[g["ts"] >= CUTS[1]][col]
    folds_pos = sum(1 for f in (f1, f2, f3) if len(f) >= 20 and f.mean() > 0)
    # drop the single best pair; is pooled still positive?
    best_pair = max(pp, key=lambda p: pp[p][0])
    dm = g[g["pair"] != best_pair][col].mean()
    freq = len(g) / 5 / 1.0  # events per pair per month (30d window)
    ok = (ci > 0 and len(g) >= 300 and pairs_pos >= 4 and folds_pos >= 2
          and freq >= 10 and dm > 0)
    print(f"{v:12s} {h:12s} {len(g):5d} {col:>11s} {exp*100:+7.3f} {ci*100:+7.3f} "
          f"{pairs_pos}/5{'':>2s} {folds_pos}/3{'':>3s} {dm*100:+8.3f} {(exp+FEE_COST-0.0006)*100:+7.3f} "
          f"{g['mfe'].mean()*100:6.2f} {g['mae'].mean()*100:6.2f} {g['ret30'].mean()*100:+7.3f} {g['ret240'].mean()*100:+8.3f} {'PASS' if ok else 'fail'}")
    results.append({"variant": v, "hyp": h, "n": len(g), "best": col, "exp": exp,
                    "ci_lo": ci, "pairs_pos": pairs_pos, "folds_pos": folds_pos,
                    "drop_max": dm, "per_pair": pp,
                    "folds": {"f1": f1.mean() if len(f1) else None,
                              "f2": f2.mean() if len(f2) else None,
                              "f3": f3.mean() if len(f3) else None}})

# direction-travel evidence: signed returns per variant x hyp (unconditional)
print("\n=== travel test: mean signed return by horizon (does price go the hypothesis way?) ===")
for (v, h), g in df.groupby(["variant", "hyp"]):
    print(f"{v:12s} {h:12s} n={len(g):5d} ret30={g['ret30'].mean()*100:+.3f}% (CIlo {boot_lo(g['ret30'].to_numpy())*100:+.3f}) "
          f"ret60={g['ret60'].mean()*100:+.3f}% ret240={g['ret240'].mean()*100:+.3f}%")

pd.DataFrame([{k: v for k, v in r.items() if k != "per_pair"} for r in results]).to_csv("sweep_reclaim_gates.csv", index=False)
print("\nsaved sweep_reclaim_gates.csv")
