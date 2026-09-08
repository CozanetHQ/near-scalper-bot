import os, sys, json, statistics
from datetime import datetime
from backtest import Backtest

def ts_of(iso): return int(datetime.fromisoformat(iso).timestamp()*1000)

def run(week, candles, tpmult):
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"MARGIN_BUDGET": "0.85", "TP_AGING": "1", "INVENTORY_CAP": "999",
                       "COORD_MODE": "0", "SWING_TP": "0", "SWING_AGE": "0", "LIQ_MODEL": "0",
                       "MAX_POSITIONS": "2", "TREND_MULT": "1.0", "TREND_CHASE": "0"})
    import tick
    tick.LEVERAGE = 10; tick.WIN_TARGET_DOLLARS = 0.05; tick.SCALP_TP_ATR = tpmult
    bt = Backtest(tick, candles, start_balance=3.0)
    err = bt.run()
    if err: print(f"  {week} {tpmult}x: {err}"); return None
    r = bt.report()
    ts = [c['ts'] for c in candles]; lows=[c['low'] for c in candles]; highs=[c['high'] for c in candles]
    last = candles[-1]['close']
    def idx(t): return min(range(len(ts)), key=lambda k: abs(ts[k]-t))
    crossed = 0; ttt = []
    for t in bt.trades:
        i0, i1 = idx(ts_of(t['opened_at'])), idx(ts_of(t['closed_at']))
        ttt.append((ts[i1]-ts[i0])/3600000)
        lo, hi = min(lows[i0:i1+1]), max(highs[i0:i1+1])
        adv = (t['entry_price']-lo)/t['entry_price'] if t['side']=='long' else (hi-t['entry_price'])/t['entry_price']
        if adv >= 0.20: crossed += 1
    pos = tick.parse_positions(bt.sim_state)
    for p in pos:
        i0 = idx(ts_of(p['opened_at']))
        lo, hi = min(lows[i0:]), max(highs[i0:])
        adv = (p['entry_price']-lo)/p['entry_price'] if p['side']=='long' else (hi-p['entry_price'])/p['entry_price']
        if adv >= 0.20: crossed += 1
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance'] + unreal
    n = len(bt.trades)+len(pos)
    print(f"  {week:8} {tpmult}x | trades {n:3} | net {r['net']/3*100:+6.1f}% | equity ${eq:+.2f} | "
          f"maxDD ${bt.min_eq-3.0:+.2f} | capHours {bt.cap_hours:7.0f} | $/(cap·h) {r['net']/max(bt.cap_hours,1):+.4f} | "
          f"medTP {statistics.median(ttt)*60 if ttt else 0:4.0f}min | liq-first {crossed}/{n}")
    sys.stdout.flush()

hostile = json.load(open('data_1m.json'))
fresh = json.load(open('data_1m_fresh.json'))
print("═══ FRESH WEEK (Aug 23–30, V-shape whipsaw, -0.7% net, 16.8% range) ═══")
for w in (0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0): run("fresh", fresh, w)
print("═══ HOSTILE WEEK (Aug 30–Sep 6, +32% pump) — full width grid ═══")
for w in (0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0): run("hostile", hostile, w)
