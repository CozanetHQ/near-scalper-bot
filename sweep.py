#!/usr/bin/env python3
"""
Parameter grid search for the NEAR scalper. Runs the REAL tick.py logic
over 7.6 days of 1m candles for every config. A config only counts as good
if it is profitable in BOTH halves of the data (walk-forward stability) —
protects against picking a one-lucky-week fluke.
"""
import json, itertools, time
from backtest import load_module, load_candles, Backtest

GRID = {
    "SL_ATR_MULT":   [1.5, 2.2, 3.0, 4.0],
    "TP_SL_RATIO":   [1.5, 2.2, 3.0, 4.0],
    "ATR_MIN":       [0.0008, 0.0015, 0.0025, 0.0040],
    "RSI_LONG_ENTRY": [30, 40, 50],
    "TRAIL":         [(0.55, 0.35), (0.75, 0.25), (99.0, 99.0)],  # last = trail off
    "CONSOL":        [0.004, 0.0],  # regime brain on / off
}

candles = load_candles()
mid = len(candles) // 2
results_f = open("sweep_results.jsonl", "w")
combos = list(itertools.product(*GRID.values()))
print(f"{len(combos)} configs", flush=True)
t0 = time.time()

for n, (slm, tpr, atrm, rsi, trail, consol) in enumerate(combos):
    tick = load_module()
    tick.SL_ATR_MULT = slm
    tick.TP_SL_RATIO = tpr
    tick.ATR_MIN = atrm
    tick.NIGHT_ATR_MIN = max(2 * atrm, 0.002)
    tick.RSI_LONG_ENTRY = rsi
    tick.RSI_SHORT_ENTRY = 100 - rsi
    tick.TRAIL_TRIGGER, tick.TRAIL_DIST = trail
    tick.CONSOL_MAX_RANGE = consol
    try:
        b1 = Backtest(tick, candles)
        e1 = b1.run(30, mid)
        r1 = b1.report() if not e1 else None
        b2 = Backtest(tick, candles)
        e2 = b2.run(mid)
        r2 = b2.report() if not e2 else None
    except Exception as ex:
        continue
    rec = {"cfg": {"SL": slm, "TP": tpr, "ATR": atrm, "RSI": rsi, "TRAIL": trail, "CONSOL": consol},
           "h1": r1, "h2": r2, "full": None}
    if r1 and r2:
        b3 = Backtest(tick, candles)
        b3.run()
        rec["full"] = b3.report()
    results_f.write(json.dumps(rec) + "\n")
    results_f.flush()
    if n % 50 == 0:
        print(f"{n}/{len(combos)} elapsed {time.time()-t0:.0f}s", flush=True)

print("DONE", flush=True)
