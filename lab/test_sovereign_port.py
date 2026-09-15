#!/usr/bin/env python3
"""Parity harness: replay the lab NEAR 21d dataset through the LIVE engine
path (tick.process_tick -> engine.sovereign.process_pair) with fetch/sync
monkeypatched, stepping a simulated clock every ~5 minutes (tick cadence).

Goal: the live port must reproduce the lab run B (maker entry):
  14 trades, 7W/7L, net ~ +0.33 on $10.

Small divergences are expected and acceptable (tick-time snapshots vs the
lab's per-2m-candle walk), but the trade count and sign of net must match.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
os.chdir(REPO)

os.environ.update(dict(SOVEREIGN_V6="1", SOVEREIGN_PAIRS="NEARUSDT", SOVEREIGN_L2_GATE="0"))

import tick as T
from engine import sovereign

c4h = json.load(open("lab/data_sovereign/near_4h.json"))
c1h = json.load(open("lab/data_sovereign/near_1h.json"))
c1m_all = json.load(open("lab/data_sovereign/near_1m.json"))
from sovereign_engine import resample as _res
c15 = _res(c1m_all, 15)

def slice_to(candles, ts, limit):
    """Closed candles with ts <= cursor, last `limit` of them."""
    got = [c for c in candles if c["ts"] < ts]   # strictly: a candle opening AT the clock is still forming
    return got[-limit:]

def fetch_candles(granularity, limit=5, symbol="NEARUSDT"):
    cur = CLOCK["ms"]
    g = {"4H": (c4h, 4*3600_000), "1H": (c1h, 3600_000), "15m": (c15, 900_000), "1m": (c1m_all, 60_000)}[granularity]
    rows = slice_to(g[0], cur, limit)
    # append a synthetic forming candle (fetch includes forming last in live)
    forming = dict(rows[-1]); forming["ts"] = rows[-1]["ts"] + g[1]
    return rows + [forming]

def fetch_ticker(symbol="NEARUSDT"):
    rows = [c for c in c1m_all if c["ts"] < CLOCK["ms"]]
    return {"last": rows[-1]["close"], "mark": rows[-1]["close"]}

T.fetch_candles = fetch_candles
T.fetch_ticker = fetch_ticker
T.send_telegram = lambda text: None

# in-memory state + sync
master = {
    "account": {"balance": 10.0, "peak_balance": 10.0, "status": "running", "max_positions": 2},
    "pairs": {"NEARUSDT": T.fresh_pair_state("NEARUSDT")},
    "trades": [],
}
def load_master():
    return master
def sync(pair, state_update=None, trade=None):
    su = state_update or {}
    if "balance" in su:
        master["account"]["balance"] = su["balance"]
        master["account"]["peak_balance"] = max(master["account"]["peak_balance"], su["balance"])
    ps = master["pairs"].setdefault(pair, T.fresh_pair_state(pair))
    for k, v in su.items():
        if k != "balance":
            ps[k] = v
    if trade:
        master["trades"].insert(0, trade)
    return {"ok": True}
T.load_master = load_master
T.sync = sync

CLOCK = {"ms": c1m_all[200]["ts"]}   # start after warmup
end_ms = c1m_all[-1]["ts"]
step = 5 * 60_000                     # tick cadence

actions = {}
tick_n = 0
while CLOCK["ms"] < end_ms:
    state = T.get_state("NEARUSDT")
    try:
        action, details = T.process_tick(state)
    except Exception as e:
        import traceback
        print("TICK ERROR at", tick_n, ":", e)
        traceback.print_exc()
        break
    actions[action] = actions.get(action, 0) + 1
    tick_n += 1
    CLOCK["ms"] += step

trades = master["trades"]
net = sum(t["net_pnl"] for t in trades)
wins = sum(1 for t in trades if t["net_pnl"] > 0)
print(f"\nticks={tick_n}  actions={actions}")
print(f"trades={len(trades)}  wins={wins}  losses={len(trades)-wins}  net={net:+.4f}  balance={master['account']['balance']:.4f}")
print("lab run B reference: trades=14  wins=7  net=+0.3276")
for t in trades:
    print(f"  {t['reason']:<3} {t['side']:<5} entry={t['entry_price']:.6g} exit={t['exit_price']:.6g} net={t['net_pnl']:+.4f} held={t['minutes_held']}m")
