import os, sys, json
from backtest import Backtest
def run(label, candles, slip="0"):
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"MARGIN_BUDGET": "0.85", "TP_AGING": "1", "INVENTORY_CAP": "999",
                       "COORD_MODE": "0", "SWING_TP": "0", "SWING_AGE": "0", "LIQ_MODEL": "0",
                       "MAX_POSITIONS": "2", "TREND_MULT": "1.0", "TREND_CHASE": "0",
                       "WIN_TARGET_PCT": "0.0167", "SLIP_PCT": slip})
    import tick
    tick.LEVERAGE = 10
    bt = Backtest(tick, candles, start_balance=3.0)
    err = bt.run()
    if err: print(f"  {label:44} ERROR {err}"); return
    r = bt.report(); last = candles[-1]['close']
    ts=[c['ts'] for c in candles]; lows=[c['low'] for c in candles]; highs=[c['high'] for c in candles]
    from datetime import datetime
    def ts_of(iso): return int(datetime.fromisoformat(iso).timestamp()*1000)
    def idx(t): return min(range(len(ts)), key=lambda k: abs(ts[k]-t))
    crossed=0
    for t in bt.trades:
        i0,i1 = idx(ts_of(t['opened_at'])), idx(ts_of(t['closed_at']))
        lo,hi = min(lows[i0:i1+1]), max(highs[i0:i1+1])
        adv = (t['entry_price']-lo)/t['entry_price'] if t['side']=='long' else (hi-t['entry_price'])/t['entry_price']
        if adv>=0.20: crossed+=1
    pos = tick.parse_positions(bt.sim_state)
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance']+unreal
    print(f"  {label:44} net {r['net']/3*100:+8.1f}% | eq ${(eq/3-1)*100:+7.1f}% | maxDD ${bt.min_eq-3.0:+6.2f} | liq-first {crossed}/{len(bt.trades)+len(pos)} | $/(cap·h) {r['net']/max(bt.cap_hours,1):+.4f}")
    sys.stdout.flush()
h = json.load(open('data_1m.json')); f = json.load(open('data_1m_fresh.json'))
print("═══ TEST 1+2+5+6: COST STRESS (RT cost 0.12% → 0.24% → 0.36%) ═══")
for slip,lab in (("0.0006","2x cost"),("0.0012","3x cost")):
    run(f"fresh V-week, {lab}", f, slip)
    run(f"hostile pump-week, {lab}", h, slip)
print("═══ TEST 3: VIOLENT TREND (-57% one-way week, real noise) ═══")
run("trend massacre, base cost", json.load(open('data_trend.json')))
print("═══ TEST 4: FLASH MOVE (-8% in one minute, mid-week) ═══")
run("flash crash, base cost", json.load(open('data_flash.json')))
