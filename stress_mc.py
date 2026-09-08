import json, statistics
exec(open('stress.py').read().split('h = json')[0])  # reuse run()
mcs = json.load(open('data_mc.json'))
print("═══ TEST 8: MONTE CARLO — 8 bootstrap synthetic weeks (ship config, 1.67% target) ═══")
nets, eqs, liqs, trades = [], [], 0, 0
for k, mc in enumerate(mcs):
    import os, sys
    for m in list(sys.modules):
        if m == "tick" or m.startswith("tick"): del sys.modules[m]
    os.environ.update({"MARGIN_BUDGET":"0.85","TP_AGING":"1","INVENTORY_CAP":"999","COORD_MODE":"0",
                       "SWING_TP":"0","SWING_AGE":"0","LIQ_MODEL":"0","MAX_POSITIONS":"2",
                       "TREND_MULT":"1.0","TREND_CHASE":"0","WIN_TARGET_PCT":"0.0167","SLIP_PCT":"0"})
    import tick
    tick.LEVERAGE = 10
    from backtest import Backtest
    bt = Backtest(tick, mc, start_balance=3.0)
    err = bt.run()
    if err: print(f"  mc{k+1}: {err}"); continue
    r = bt.report(); last = mc[-1]['close']
    pos = tick.parse_positions(bt.sim_state)
    unreal = sum((last-p['entry_price'])*(p['notional']/p['entry_price']) if p['side']=='long'
                 else (p['entry_price']-last)*(p['notional']/p['entry_price']) for p in pos)
    eq = r['final_balance']+unreal
    nets.append(r['net']/3*100); eqs.append((eq/3-1)*100)
    trades += len(bt.trades); 
    print(f"  mc{k+1} ({(mc[-1]['close']/mc[0]['open']-1)*100:+5.1f}% market): net {nets[-1]:+7.1f}% | equity {eqs[-1]:+7.1f}% | trades {len(bt.trades):3} | maxDD ${bt.min_eq-3.0:+.2f}")
    sys.stdout.flush()
print(f"\n  MEDIAN net {statistics.median(nets):+.1f}% | WORST net {min(nets):+.1f}% | BEST {max(nets):+.1f}%")
print(f"  MEDIAN equity {statistics.median(eqs):+.1f}% | WORST equity {min(eqs):+.1f}% | weeks with positive equity: {sum(1 for e in eqs if e>0)}/{len(eqs)}")
print(f"  weeks net-positive: {sum(1 for n in nets if n>0)}/{len(nets)}")
