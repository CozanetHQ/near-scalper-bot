import json, os, importlib.util

spec = importlib.util.spec_from_file_location("bt", "backtest.py")
bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
results = {}
for pair in PAIRS:
    candles = json.load(open(f"data_{pair}_30d.json"))
    n = len(candles)
    os.environ["SIM_PAIR"] = pair
    # FULL with instrumentation: track balance curve + trade timestamps
    os.environ["STATE_FILE"] = f"/tmp/p4b_{pair}.json"
    tick = bt.load_module()
    e = bt.Backtest(tick, candles)
    err = e.run()
    if err:
        print(f"{pair} FULL ERROR: {err}", flush=True); continue
    full = e.report()
    tr = e.trades
    # when did the last trade close? (dead-zone detector)
    if tr:
        first_ts = tr[0].get("closed_at"); last_ts = tr[-1].get("closed_at")
    else:
        first_ts = last_ts = None
    # OOS halves, fresh state each
    os.environ["STATE_FILE"] = f"/tmp/p4b_{pair}_h1.json"
    t2 = bt.load_module()
    e2 = bt.Backtest(t2, candles[:n//2]); err2 = e2.run()
    h1 = e2.report() if not err2 else {"error": str(err2)}
    os.environ["STATE_FILE"] = f"/tmp/p4b_{pair}_h2.json"
    t3 = bt.load_module()
    e3 = bt.Backtest(t3, candles[n//2:]); err3 = e3.run()
    h2 = e3.report() if not err3 else {"error": str(err3)}
    results[pair] = {"full_30d": full, "first_15d": h1, "last_15d": h2,
                     "first_trade_close": first_ts, "last_trade_close": last_ts}
    print(f"{pair}: full {full['trades']}t net {full['net']:+.4f} pf {full['profit_factor']} wr {full['win_rate']}% | "
          f"h1 {h1.get('trades','?')}t {h1.get('net','?')} | h2 {h2.get('trades','?')}t {h2.get('net','?')} | "
          f"last close {last_ts}", flush=True)

json.dump(results, open("phase4_results.json", "w"), indent=2)
print("PHASE4B DONE", flush=True)
