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

---

# Run 2 — 2m Execution Switch (owner request, 2026-09-15) + maker-retest + BTC + short-side attempt

The engine is now timeframe-parametric: `EXEC_TF_MIN` (default **2m**, per owner) sets the
execution walk; gates unchanged (4H bias / 1H FVG / 15m sweep / 2m CHOCH / live L2).
`ENTRY_MODE`: `taker` (spec: market at CHOCH close) vs `maker` (POST_ONLY retest limit
at the CHOCH level — lever #1). `EXIT_ORDER`: SL-first vs TP-first bounding.

## Results matrix (all 1.5% risk, 10x cap, $10 start)

| Run | Data | Exec | Entry | Trades | WR | Breakeven | Net |
|---|---|---|---|---|---|---|---|
| prev | NEAR 21d | 1m walk | taker | 16 | 56.2% | 52.8% | **+0.17** |
| A | NEAR 21d | 2m | taker | 15 | 40.0% | 52.9% | −0.95 |
| B | NEAR 21d | 2m | **maker retest** | 14 | 50.0% | 44.1% | **+0.33** |
| C | BTC 21d | 2m | taker | 12 | 33.3% | 80.1% | −1.52 |
| D | BTC 21d | 2m | maker | 12 | 33.3% | 59.2% | −1.04 |
| E | BTC bear 5d | 2m | maker | 0 | — | — | 0 |
| F | NEAR 21d | 1m | maker | 19 | 31.6% | 48.2% | −1.62 |

## Findings

1. **2m switch works; exit ambiguity is free.** TP-first vs SL-first bounding on run B is
   identical (+0.3276 both) — no trade had both levels inside one 2m candle at ATR-scaled
   stops. The visible cost of the 2m walk (A vs prev) is *entry timing*: the momentum
   market-buy fills up to 2 minutes after the CHOCH close, chasing the move.
2. **Maker retest entry is the fix and the best config** (B): waiting for the pullback to
   the CHOCH level (a) fills at 0.02% maker instead of 0.09% taker+slip, (b) buys the
   dip rather than the breakout spike. Fees drop $1.34 → $0.69. Breakeven 44.1% vs
   realized 50.0% → expectancy +0.234%/trade. NOTE: this deviates from the spec's
   "Execute Market Buy" — owner decision required before live.
3. **BTC at 2m ATR width is mathematically dead** (C/D): median ΔP = 0.088% vs
   round-trip friction 0.11–0.18%. TP gross = 0.176% cannot cover the 0.198% net loss.
   No execution model fixes a sub-friction stop width — BTC needs a wider execution TF
   or an ATR multiple. (5m/15m probes were too thin to conclude: 9 and 1 trades.)
4. **Short side: implemented, untested.** Bitget 1m history reaches only ~30 days; the
   May–Jun bear stretch is unavailable at 1m/1H. The partial Aug 13–17 window (5 days
   of 1m) produced a funnel of 1,382 bear-bias 2m candles → 99 inside an active FVG →
   0 sweep+CHOCH alignments. The 4-gate stack is rare by design (~0.7 setups/day on
   NEAR); 0 in 5 days is within variance. Shorts will exercise live when a bear
   stretch occurs; logic is fully mirrored (bias, supply FVG, swing-high sweep, CHOCH
   below post-sweep low).
5. **F (1m + maker) is the worst combo** — the 1m ATR stop (median 0.171%) plus retest
   fill sits closer to the stop: tighter stop, same friction. Confirms: stop width must
   scale with timeframe; 2m ATR is the narrowest workable width on NEAR-class vol.

## Corrected breakeven math

W\*(2ΔP − C_win) = (1−W)(ΔP + C_loss) → **W\* = (ΔP + C_loss) / (3ΔP + C_loss − C_win)**

| Entry mode | C_win | C_loss |
|---|---|---|
| taker entry + maker TP | 0.11% | 0.18% |
| maker entry + maker TP | 0.04% | 0.11% |

## Recommendation for live v6 (pending owner approval)

- Execution walk: **2m** (owner directive) — exits verified unambiguous at this width.
- Entry: **POST_ONLY retest limit** at the CHOCH level, 3h expiry — the single largest
  EV lever (+0.3 vs −0.95 on the same window).
- Pair filter: require ΔP = ATR14(2m) ≥ friction floor (≈0.15% for taker, ≈0.10% maker);
  excludes BTC/ETH-class at 2m, keeps NEAR/SOL-class.
- Before any live port: out-of-sample NEAR window + first live bear stretch short audit.

**Files:** `lab/sovereign_engine.py` (parametric), `lab/sovereign_v6_trades_*.json` (7 runs),
`lab/data_sovereign/` (NEAR + BTC datasets incl. bear window).
