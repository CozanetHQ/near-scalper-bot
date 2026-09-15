# Sovereign v6 Lab Report — CozySovereignAI Entry Engine
**Date:** 2026-09-15 · **Data:** NEARUSDT USDT-perp, 180d 4H + 42d 1H + 21d 1m (Bitget v2, native fetch)
**Status:** LAB ONLY — no changes to tick.py or the live loop.

## 1. Engine built (per locked owner spec, a priori — not tuned on results)

| Phase | TF | Gate | Implementation |
|---|---|---|---|
| Filter | 4H | Close > EMA50 > EMA200 (long), mirrored short | EMA seeded SMA, last completed candle only |
| Zone | 1H | Price inside active Fair Value Gap | FVG = 3-candle displacement; zone dies on 1H close through it |
| Setup | 15m | Sweep: Low < SwingLow AND Close > SwingLow | k=2 confirmed fractals, sweep valid 3h, invalidated on 15m close below swept level |
| Trigger | 2m | CHOCH: close above post-sweep pullback lower high | Fractal high must FORM after the sweep; + L2 gate I_L2 ≥ 1.5 (live-only, stubbed in lab) |
| Target | Live | TP = Entry + 2×ΔP, SL = Entry − 1×ΔP | ΔP = ATR14(2m); POST_ONLY TP (maker) |

Risk: 1.5% fixed capital per trade; notional capped 10× equity; $5 Bitget minimum.
Exits: conservative (SL assumed first if both levels inside one 1m candle).

## 2. Friction math (exact)

- taker 0.06% + slip 0.03% per market leg; maker 0.02%
- C_win (taker entry + maker TP) = **0.11%** — spec's all-taker baseline 0.15%
- C_loss (taker entry + taker SL, both + slip) = **0.18%**
- Breakeven win rate: W* = (ΔP + C_loss) / (3ΔP + 0.04%·… netted) → at ΔP = 0.245% (median ATR14 2m in sample): **W* ≈ 52.8%**
- Note: spec text says the maker exit reclaims 0.06%; exact arithmetic is 0.04% (0.06 taker TP leg → 0.02 maker). All EV math above uses the exact figure.

**Old v5 engine:** W* ≈ 81% breakeven, realized 91.7% WR — still lost money because loss:win asymmetry was 26:1 (tiny TP, 6% hard SL). **New structure:** W* ≈ 53%, realized 56.2% — the asymmetry is fixed; now friction is the binding constraint (see §4).

## 3. Results — 21-day replay (NEAR, ~30k 1m candles, one-way 4H-bull window)

```
trades=16  wins=9  losses=7  win_rate=56.2%
equity: 10.00 -> 10.17  (net +0.1728)
avg_win=+0.2397  avg_loss=-0.2835  realized payoff 0.85:1 (gross 2:1 minus friction)
expectancy: +0.0108/trade (+0.108% of equity)
exits: 9 TP_MAKER / 7 SL
SL distance (ATR14 2m): median 0.245%, min 0.157%, max 0.805%
hold: median 13 min, max 48 min   (old engine: median losers held 400–2880 min)
```

## 4. The binding constraint — friction vs scalp distance

At ΔP = 0.245%: a WIN pays 2ΔP = 0.49% gross but C_win 0.11% eats **23%** of it;
a LOSS costs ΔP = 0.245% gross but C_loss 0.18% adds **73%** on top.
Realized payoff fell from 2:1 gross to 0.85:1 net — friction consumed roughly the whole gross edge. Levers, in EV-impact order:

1. **Maker entry.** The CHOCH level is known in advance — a POST_ONLY limit at the fractal level makes the entry 0.02% instead of 0.09% (taker+slip), saving 0.07%/side. Caveat: a pure break may run through without filling (momentum risk); a hybrid (limit, chase on close-above) preserves most of the saving.
2. **Wider ΔP.** C/dP scales inversely: at ΔP ≈ 0.5% (5m ATR or 2×2m ATR), friction share drops by half. Cost: fewer trades.
3. **L2 gate (live only)** — cannot be replayed from candles; if it lifts the win rate even 5–8pts the EV turns decisively positive.

## 5. Caveats (honesty ledger)

- 16 trades is a small sample; 56.2% ±12pt confidence interval straddles breakeven. Not yet statistical proof of edge.
- All 16 trades were LONG — the window was one-way 4H-bull. Short side untested (needs a bearish window; BTC/ETH data fetchable).
- TP maker fill assumed on touch (optimistic — a limit can go untouched).
- SL-first intra-candle assumption (conservative) partially offsets the above.
- L2 imbalance gate stubbed True — live filtering will reduce trade count, direction of EV impact unknown.

## 6. Recommended next steps

1. Out-of-sample: earlier Aug weeks (local data_1m_fresh.json) + a bearish pair/window for the short side.
2. Maker-entry variant lab run (the 0.07%/side is the single largest EV lever at this scalp width).
3. Only after out-of-sample + maker-entry shows W* margin ≥ 5pts sustained: port the state machine into tick.py v6 behind a feature flag.

**Files:** `lab/sovereign_engine.py`, `lab/sovereign_v6_trades.json`, `lab/data_sovereign/{near_4h,near_1h,near_1m}.json`
