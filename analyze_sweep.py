#!/usr/bin/env python3
import json

recs = [json.loads(l) for l in open("sweep_results.jsonl") if l.strip()]
recs = [r for r in recs if r.get("h1") and r.get("h2") and r.get("full")]

# stability: profitable in BOTH halves = robust, not a fluke
stable = [r for r in recs if r["h1"]["net"] > 0 and r["h2"]["net"] > 0]
print(f"configs tested: {len(recs)} | profitable BOTH halves: {len(stable)}\n")

def key(r):
    # rank by worst-half net first (robustness), then total
    return (min(r["h1"]["net"], r["h2"]["net"]), r["full"]["net"])

stable.sort(key=key, reverse=True)
print(f"{'SL':>4} {'TP':>4} {'ATR':>7} {'RSI':>4} {'trail':>10} {'consol':>6} | {'h1net':>7} {'h2net':>7} {'worst':>7} | {'fullnet':>8} {'WR%':>5} {'PF':>5} {'n':>4} {'maxDD':>6}")
for r in stable[:15]:
    c = r["cfg"]; f = r["full"]
    tr = "off" if c["TRAIL"][0] > 5 else f"{c['TRAIL'][0]}/{c['TRAIL'][1]}"
    co = "on" if c["CONSOL"] else "off"
    print(f"{c['SL']:>4} {c['TP']:>4} {c['ATR']:>7.4f} {c['RSI']:>4} {tr:>10} {co:>6} | {r['h1']['net']:>+7.3f} {r['h2']['net']:>+7.3f} {min(r['h1']['net'],r['h2']['net']):>+7.3f} | {f['net']:>+8.3f} {f['win_rate']:>5.1f} {str(f['profit_factor']):>5} {f['trades']:>4} {f['max_drawdown']:>6.3f}")

# what does the data say about each parameter across ALL configs?
print("\n── parameter effect (median full net by value, all configs) ──")
import statistics
for pname, pos in [("SL_ATR_MULT",0),("TP_SL_RATIO",1),("ATR_MIN",2),("RSI_LONG_ENTRY",3),("TRAIL",4),("CONSOL",5)]:
    by = {}
    for r in recs:
        v = r["cfg"][pname]
        key = str(v)
        by.setdefault(key, []).append(r["full"]["net"])
    line = "  " + pname + ": "
    for v, nets in sorted(by.items(), key=lambda x: str(x[0])):
        line += f"{v}(med {statistics.median(nets):+.2f}, n={len(nets)})  "
    print(line)
