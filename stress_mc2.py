import json, statistics, os, sys
mcs = json.load(open('data_mc.json'))
print("═══ MONTE CARLO at LIVE PLAN (5x, 40% budget, liq-bounded, 1.67% target) ═══")
nets, eqs, liqs = [], [], 0
for k, mc in enumerate(mcs):
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"MARGIN_BUDGET":"0.40","TP_AGING":"1","INVENTORY_CAP":"999","COORD_MODE":"0",
                       "SWING_TP":"0","SWING_AGE":"0","LIQ_MODEL":"1","MAX_POSITIONS":"2",
                       "TREND_MULT":"1.0","TREND_CHASE":"0","WIN_TARGET_PCT":"0.0167","SLIP_PCT":"0"})
    import tick
    tick.LEVERAGE = 5
    from backtest import Backtest
    bt = Backtest(tick, mc, start_balance=3.0)
    err = bt.run()
    if err: print(f"  mc{k+1}: {err}"); continue
    r = bt.report(); last = mc[-1]['close']
    pos = tick.parse_positions(bt.sim_state)
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance']+unreal
    nl = len([t for t in bt.trades if t.get('reason')=='LIQ'])
    liqs += nl
    nets.append(r['net']/3*100); eqs.append((eq/3-1)*100)
    print(f"  mc{k+1} ({(mc[-1]['close']/mc[0]['open']-1)*100:+5.1f}% mkt): net {nets[-1]:+6.1f}% | equity {eqs[-1]:+7.1f}% | liqs {nl} | maxDD ${bt.min_eq-3.0:+.2f}")
    sys.stdout.flush()
print(f"\n  MEDIAN net {statistics.median(nets):+.1f}% | WORST {min(nets):+.1f}% | BEST {max(nets):+.1f}% | total liqs {liqs}")
print(f"  MEDIAN equity {statistics.median(eqs):+.1f}% | WORST {min(eqs):+.1f}% | positive-equity weeks: {sum(1 for e in eqs if e>0)}/{len(eqs)}")
