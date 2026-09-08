import os, sys, json
from datetime import datetime
from backtest import Backtest
def run(label, candles, gate):
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"MARGIN_BUDGET":"0.85","TP_AGING":"1","INVENTORY_CAP":"999","COORD_MODE":"0",
                       "SWING_TP":"0","SWING_AGE":"0","LIQ_MODEL":"0","MAX_POSITIONS":"2",
                       "TREND_MULT":"1.0","TREND_CHASE":"0","WIN_TARGET_PCT":"0.0167",
                       "SLIP_PCT":"0","TRANSITION_GATE":gate})
    import tick
    tick.LEVERAGE = 10
    bt = Backtest(tick, candles, start_balance=3.0)
    err = bt.run()
    if err: print(f"  {label}: {err}"); return
    r = bt.report(); last = candles[-1]['close']
    pos = tick.parse_positions(bt.sim_state)
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance']+unreal
    print(f"  {label:38} net {r['net']/3*100:+8.1f}% | equity {(eq/3-1)*100:+8.1f}% | trades {len(bt.trades):3} | wedges {len(pos)} (${unreal:+.2f}) | maxDD ${bt.min_eq-3.0:+.2f}")
    sys.stdout.flush()
h = json.load(open('data_1m.json')); f = json.load(open('data_1m_fresh.json')); t = json.load(open('data_trend.json'))
print("═══ YELLOW-STATE TRANSITION GATE: OFF vs ON (ship config) ═══")
for g in ("0","1"):
    print(f"── gate {g} ──")
    run("hostile pump week", h, g)
    run("fresh V-shape week", f, g)
    run("trend massacre (-57%)", t, g)
