import os, sys, json
from datetime import datetime
from backtest import Backtest
def run(label, candles, live=False, safe="0.25"):
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"TP_AGING":"1","INVENTORY_CAP":"999","COORD_MODE":"0","SWING_TP":"0",
                       "SWING_AGE":"0","MAX_POSITIONS":"2","TREND_MULT":"1.0","TREND_CHASE":"0",
                       "WIN_TARGET_PCT":"0.0167","SLIP_PCT":"0","TRANSITION_GATE":"0","SAFE_DD":safe})
    if live:
        os.environ.update({"MARGIN_BUDGET":"0.40","LIQ_MODEL":"1"})
    else:
        os.environ.update({"MARGIN_BUDGET":"0.85","LIQ_MODEL":"0"})
    import tick
    tick.LEVERAGE = 5 if live else 10
    bt = Backtest(tick, candles, start_balance=3.0)
    err = bt.run()
    if err: print(f"  {label}: {err}"); return
    r = bt.report(); last=candles[-1]['close']
    pos = tick.parse_positions(bt.sim_state)
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance']+unreal
    nliq = len([t for t in bt.trades if t.get('reason')=='LIQ'])
    snap = json.loads(bt.sim_state.get('last_reversal_at') or '{}')
    print(f"  {label:34} net {r['net']/3*100:+8.1f}% | equity ${(eq/3-1)*100:+7.1f}% | trades {len(bt.trades):3} | wedges {len(pos)} | liqs {nliq} | maxDD ${bt.min_eq-3.0:+6.2f} | safe-fired {int(snap.get('sf') or 0)}")
    sys.stdout.flush()
print("═══ FINAL RESEARCH: what the market has against the bot (paper ship config) ═══")
run("dead-quiet week (vol ÷4)", json.load(open('data_dead.json')))
run("vol storm (vol ×3)", json.load(open('data_storm.json')))
run("whipsaw XL (vol ×2)", json.load(open('data_whipxl.json')))
run("gap-and-dead (3× -6% gaps)", json.load(open('data_gapdead.json')))
print("═══ ENGINE 10 SAFE-DD wall: trend massacre at LIVE plan (5x/40%/liq) ═══")
t = json.load(open('data_trend.json'))
run("SAFE off", t, live=True, safe="0")
run("SAFE 0.25 (shipped default)", t, live=True, safe="0.25")
