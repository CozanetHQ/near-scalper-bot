import json, os, importlib.util

spec = importlib.util.spec_from_file_location("bt", "backtest.py")
bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]  # NEAR already validated: 473 trades +0.2850
results = {}
for pair in PAIRS:
    fname = f"data_{pair}_30d.json"
    candles = json.load(open(fname))
    n = len(candles)
    os.environ["SIM_PAIR"] = pair
    os.environ["STATE_FILE"] = f"/tmp/p4_{pair}.json"
    tick = bt.load_module()
    e = bt.Backtest(tick, candles)
    err = e.run()
    if err: print(f"{pair} FULL: {err}", flush=True); continue
    full = e.report()
    os.environ["STATE_FILE"] = f"/tmp/p4_{pair}_h1.json"
    tick2 = bt.load_module()
    e2 = bt.Backtest(tick2, candles[: n // 2])
    err2 = e2.run()
    h1 = e2.report() if not err2 else {"error": str(err2)}
    os.environ["STATE_FILE"] = f"/tmp/p4_{pair}_h2.json"
    tick3 = bt.load_module()
    e3 = bt.Backtest(tick3, candles[n // 2:])
    err3 = e3.run()
    h2 = e3.report() if not err3 else {"error": str(err3)}
    results[pair] = {"full_30d": full, "first_15d": h1, "last_15d": h2}
    print(f"{pair}: full {full['trades']}t net {full['net']:+.4f} pf {full['profit_factor']} wr {full['win_rate']}% mdd {full['max_drawdown']} | h1 {h1['trades']}t {h1['net']:+.4f} | h2 {h2['trades']}t {h2['net']:+.4f}", flush=True)

json.dump(results, open("phase4_results.json", "w"), indent=2)
print("PHASE4 DONE", flush=True)
