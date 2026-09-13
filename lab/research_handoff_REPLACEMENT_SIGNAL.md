# RESEARCH HANDOFF — NEAR SCALPER BOT (CozanetHQ/near-scalper-bot)
Prepared 2026-09-13 for independent audit. All numbers produced by the scripts
named in each section; raw outputs referenced by filename. Values not recorded
are marked UNKNOWN. No numbers invented.

## 1. DATA
- Dataset period: 2026-08-12 12:13 UTC → 2026-09-11 12:12 UTC (30 days minus 1 minute), 1-minute OHLCV candles (Bitget perp klines, files `data_<PAIR>_30d.json`).
- Pairs: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT.
- Timeframes: 1m execution data; 15m efficiency-ratio regime (TREND/CHOP) derived from same tape.
- Raw signals (entry-lab universe): 66,459 total — BTC 14,494 / ETH 14,433 / SOL 14,382 / NEAR 13,418 / XRP 9,732.
- Independent events after run-collapse: 5,294 — BTC 1,136 / ETH 1,132 / SOL 1,124 / NEAR 1,066 / XRP 836. Longs 2,660, shorts 2,634.
- Events per fold: f1 1,578 / f2 1,871 / f3 1,845. (XRP f1 = 132 only; cause UNKNOWN, likely early-window data coverage.)
- Protection-study trades: baseline 1,219 (fold split 463/408/348); act70_r15 1,178 (481/373/324).
- Costs, identical in every test: taker fee 0.06%/side + slippage 0.01%/side = 0.14% round trip. Protection sim: notional/trade = 0.85/8 × 10x = 1.0625× account, $10 start, compounding.

## 2. METHODOLOGY
Entry lab (`signal_edge_lab.py`, `signal_edge_analysis.py`; universe `signal_edge_events.csv`; log `edge_analysis.log`):
- Event definition (EMA-pullback premise): EMA9>EMA21 AND bear-colored 1m bar AND close>EMA21 → long candidate; EMA9<EMA21 AND bull-colored bar AND close<EMA21 → short. Requires ATR14 ≥ 0.08% of price. EMA 9/21, ATR period 14.
- Overlap removal: per (pair, side), keep a signal only if ≥60 bars (60 min) since the last kept signal. 66,459 → 5,294.
- Fold boundaries (calendar thirds): f1 2026-08-12 12:13 → 08-22 12:13; f2 → 09-01 12:13; f3 → 09-11 12:12.
- Outcome: forward 240 bars from signal-bar close. TP/SL grid: tp ∈ {0.6, 1.0, 1.6}×ATR14(signal bar), sl ∈ {1.0, 1.5, 2.0}×ATR14. First touch wins; if neither touched within 240 bars, exit at window close (censored events included, not dropped). Costs applied to every event.
- Look-ahead protection: all 19 features computed only from data ≤ signal bar; TP/SL scan starts at bar i+1. DISCLOSED EXCEPTION: `failed_bo` used close.shift(-1) (1-bar look-ahead); its True bucket had n<40 and was excluded from all tables — no reported conclusion depends on it.
- Train/test: NOT a train/test split. Single 30-day window; the grid was evaluated on all data; folds serve as a consistency check, not OOS. True OOS = live session (independent real fills, predates all lab work, listed in §5).

Protection study (`protection_sweep.py`; results `protection_results_all.json`; baseline trades `prot_baseline_trades.csv`):
- Same entry logic + the live risk stack: MAE walls (NEAR 2.5%, others 4%), HARD_SL 6% adverse, BE_STOP arms at 50% of TP distance and exits at 2×fee+2×slip+0.0005, max 8 concurrent positions, NEAR cap 3, entry cooldown 90 s, 12% daily loss limit, 25% drawdown wall with 0.25 recovery sizing, 48 h max age.
- Configs: activation X ∈ {30,40,50,60,70}% of TP distance × retracement allowance R ∈ {10,15,20,25,30}% → floor = max((X−R)% of TP distance, cost buffer 0.0019 abs). Plus two-stage 40→60 tighten variants and a progressive ratchet reference (floor = 50% of MFE once armed at 50%).
- Protection check order per bar: MAE wall → HARD_SL → protection floor → BE_STOP → TP.
- Decision rule fixed BEFORE results: GO iff pooled expectancy > baseline AND PF ≥ baseline AND maxDD ≤ baseline AND ≥2 of 3 folds ≥ baseline's same fold.
- Idempotency: arming is a pure function of (MFE, TP distance); exit removes the position.
- n per config varies because protection changes position lifetimes and slot occupancy (1,178–1,404).

## 3. FAILED ENTRY LAB (audit §19)
- Falsified premise: the EMA-pullback entry above, in ALL tested market-state conditions. No feature bucket, 2-way combination, session, regime, or pair produced positive expectancy after 0.14% RT costs. No TP/SL grid cell on any strong bucket had bootstrap CI lower bound > 0.
- Pooled unconditional expectancy (5,294 events, tp1.6/sl2.0): −0.135%/trade, CI lo −0.141%.
- Best cell anywhere: NEARUSDT × top quartile of 24h range: −0.082%/trade, CI lo −0.122%, n=304. Still negative.
- Best grid combo among strongest buckets: sweep_hi=True, tp1.6/sl1.0: −0.119%, CI lo −0.138%, n=290.
- Win rate is NOT the constraint; follow-through is: p(reach 1.6×ATR before 2.0×ATR adverse) = 27% pooled — NEAR 49%, XRP 39%, SOL 27%, ETH 16%, BTC 9%.
- Per-pair expectancy (tp1.6/sl2.0, mean [CI lo]): NEAR −0.115% [−0.133] / SOL −0.136% [−0.146] / ETH −0.139% [−0.147] / BTC −0.141% [−0.147] / XRP −0.146% [−0.169].
- Folds: 0/3 folds positive for every bucket tested, in every pair.
- Long/short asymmetry: long −0.129% [CI lo −0.138, n=2,660] vs short −0.141% [CI lo −0.150, n=2,634]. Longs consistently less bad; both negative. Per-pair long/short split: UNKNOWN (pooled only).
- Session expectancy (tp1.6/sl2.0): OVERLAP −0.131 / LONDON −0.132 / NY −0.134 / ASIA −0.137 / LATE −0.142. Regime: CHOP −0.134 (n=4,463) / TREND −0.143 (n=799).
- 19 features tested: 24h-range position (4q), 8h-VWAP deviation in ATR units (4q), distance to 24h high/low in ATR units, sweep of prior-1h low (bullish) / high (bearish) within 3 bars, failed breakout (excluded, see §2), equal-level touch count (12h, 0.15×ATR), signal-bar displacement body/ATR (3q), MSS with ≥1.3×ATR displacement in last 5 bars, ATR compression (ATR14/ATR1h < 0.8), ATR 24h percentile, 5-bar impulse/ATR (4q), 15-bar ROC/ATR, signed same-color run length, volume vs 1h median (3q), wick bias (3q), session bucket, 15m regime, side.
- Microstructure (funding/OI/liquidations): NOT TESTED — tape history insufficient (~3 days since 2026-09-10). UNKNOWN.

## 4. PROTECTION STUDY (audit §18) — 27 configs + baseline
Winner: arm at 70% of TP distance, floor at 55% (70−15), clamped ≥ cost buffer. Exit reason PROTECTED.

```
config          exp%/tr   PF   maxDD   n     TP  PROT   BE   MK  arm→TP  f1%    f2%    f3%   rule
BASELINE        -0.0417  0.84  40.7%  1219  504     0  607   74    —     +.017  -.068  -.089  —
act70_r15       -0.0086  0.96  36.5%  1178  423   325  328   60  57%     +.080  -.047  -.096  GO
act70_r25       -0.0178  0.93  33.6%  1150  451   304  379   65  60%     +.055  -.052  -.076  GO
2stage40_r15    -0.0184  0.91  38.9%  1290  413  1042    0   70  28%     -.054  -.001  +.002  GO
act70_r10       -0.0272  0.90  42.2%  1136  386   331  294   62  54%     +.089  -.166  -.055  no
act50_r20       -0.0344  0.87  34.6%  1226  511   614    0   73  45%     +.030  -.062  -.088  GO
act60_r30       -0.0346  0.87  34.7%  1225  511   424  190   73  55%     +.030  -.062  -.088  GO
act50_r25       -0.0352  0.86  34.8%  1225  511   614    0   73  45%     +.028  -.062  -.088  GO
2stage40_r25    -0.0387  0.82  36.2%  1270  424   979    0   67  30%     -.020  -.079  -.016  no
act50_r30       -0.0400  0.85  37.3%  1223  506   612    0   74  45%     +.018  -.064  -.089  GO
RATCHET_REF     -0.0403  0.84  35.7%  1192  447   677    0   68  40%     -.027  -.039  -.060  GO
act70_r30       -0.0412  0.85  39.9%  1204  482   269  353   69  64%     +.047  -.108  -.091  no
act60_r15       -0.0488  0.82  36.2%  1185  402   518  194   71  44%     -.032  -.081  -.037  no
act40_r15       -0.0516  0.77  45.4%  1370  408   894    0   68  31%     -.078  -.067  -.006  no
act30_r10       -0.0518  0.71  39.4%  1404  340  1314    0   65  21%     -.059  -.034  -.062  no
act40_r10       -0.0519  0.77  45.4%  1364  406   890    0   68  31%     -.077  -.069  -.005  no
act40_r20       -0.0520  0.77  45.4%  1370  408   894    0   68  31%     -.079  -.068  -.006  no
act70_r20       -0.0520  0.82  38.3%  1203  407   309  321   66  57%     +.045  -.174  -.070  no
act30_r15       -0.0521  0.71  39.5%  1404  340  1314    0   65  21%     -.059  -.035  -.062  no
act40_r25       -0.0522  0.77  45.5%  1370  408   894    0   68  31%     -.079  -.068  -.006  no
act30_r20       -0.0523  0.71  39.6%  1404  340  1314    0   65  21%     -.060  -.035  -.062  no
act40_r30       -0.0523  0.76  45.5%  1370  408   894    0   68  31%     -.080  -.068  -.006  no
act30_r25       -0.0524  0.71  39.6%  1404  340  1314    0   65  21%     -.060  -.035  -.062  no
act30_r30       -0.0524  0.71  39.6%  1404  340  1314    0   65  21%     -.060  -.035  -.062  no
act60_r20       -0.0633  0.77  45.8%  1201  415   466  211   70  47%     +.012  -.221  -.037  no
act60_r10       -0.0656  0.77  42.0%  1170  356   516  198   70  41%     -.029  -.108  -.065  no
act50_r10       -0.0673  0.77  38.8%  1188  391   592    0   64  40%     -.005  -.179  -.043  no
act50_r15       -0.0768  0.74  44.3%  1234  442   613    0   72  42%     -.006  -.108  -.150  no
act60_r25       -0.1006  0.68  45.0%  1208  447   438  198   80  51%     -.049  -.146  -.120  no
```
- Pooled winner detail (act70_r15): CI [−0.0663, +0.0473]%; win rate 91.4% (baseline 91.2%); avg MFE 0.536% / MAE 0.728% (baseline 0.540%/0.739%); avg hold 278.9 min (baseline 274.7); 30-day final balance $9.36 vs baseline $6.52; fees paid $1.75 vs $1.81.
- 56.6% of armed positions still reach full TP; mean post-arm adverse excursion 0.099%.
- Owner's 40% hypothesis: REJECTED — every act40 config ≈ −0.052%/trade, PF 0.77, maxDD 45.4% (worse than baseline); early activation cuts eventual winners (31% of act40-armed reach TP vs 57% at act70).
- Weaknesses: (1) fold 3 slightly worse than baseline (−0.096 vs −0.089) — improvement concentrated in folds 1–2; (2) pooled expectancy still negative (damage control, not edge); (3) 43.4% of armed trades diverted from eventual-TP path to PROTECTED exits; (4) config selected on pooled data — the pre-locked rule and fold requirement mitigate but do not eliminate selection bias; live session is the ongoing true OOS.
- Why frozen: exit-tuning space exhausted (27 static + 2-stage + ratchet all ≤ act70_r15); marginal effort now belongs to the entry side.

## 5. STRUCTURAL FINDINGS
- Signal frequency: 66,459 raw signals / 30 days ≈ one per ~65 bars per pair (~22/day/pair). The EMA-pullback condition is not selective.
- Gates vs signal: the multi-slot sim (90 s cooldown, 8-slot cap, NEAR cap 3, spike filter, walls) admitted 1,219 trades from 66,459 raw signals (1.8%). The live engine additionally applies an EV gate (p_win = (20×0.85 + wins)/(20 + wins + losses); WAIT if EV < 0.0005/unit) and admitted 75 trades in ~2 live days. Selection was performed almost entirely by the gates, not by the signal premise.
- Excursions (mean MFE/MAE over 240-bar window, % of price): NEAR 1.79/1.77 · XRP 1.46/1.46 · SOL 1.00/0.98 · ETH 0.78/0.74 · BTC 0.58/0.57. Symmetric — the signal enters into noise, not skewed distributions. NEAR has the largest absolute excursions with equally large adverse excursions.
- Live session (2026-09-11 18:05 → 09-13, pre-protection, real fills, 75 trades): WR 94.7%, PF 0.79, avg −$0.003/trade. TP 11 (+$0.442, avg $0.040, avg hold 130 m); BE_STOP 60 (+$0.430, avg $0.007, avg hold 259 m); MAE_KILL 4 (−$1.108). All 60 BE trades reached a mean of 68.5% of TP distance before reversing (p75 ≈ 81%). High win rate coexists with PF < 1 — the excursions surrendered at BE exceed the kills' cost structure.
- Where the edge may reside: (1) cost regime — maker fills would cut RT costs from 0.14% to ~0.04–0.06%; under maker assumptions an earlier ratchet variant measured +0.068%/trade (study §9–10); act70_r15 under maker fees: UNKNOWN (not yet run); (2) new entry premises (§6); (3) funding/OI conditioning once the market-state tape has ≥30 days of history.

## 6. NEXT RESEARCH — SWEEP-RECLAIM TEST SPEC
Event (test both directions, and both with and without a trend filter):
- Long: bar i low < min(low of bars i−60..i−1) AND close_i > that swept level (same-bar reclaim). Variant B: reclaim within next 2 bars.
- Short: mirror at the 60-bar high.
Methodology: same dataset, same run-collapse (≥60-bar gap per pair+side), same folds, same 240-bar forward window, same TP/SL grid {0.6,1.0,1.6}×ATR14 × {1.0,1.5,2.0}×ATR14, taker costs mandatory; maker sensitivity reported separately. Production check: full multi-slot sim with the live risk stack + protection 70/55.
Acceptance criteria (ALL must hold; sides evaluated separately — a failing side is excluded, not averaged):
1. Pooled bootstrap 95% CI lower bound > 0 on ≥1 tp/sl combo with ≥300 pooled events.
2. Point expectancy > 0 in ≥4 of 5 pairs on that combo.
3. ≥2 of 3 folds positive pooled on that combo.
4. Live-stack simulation PF > 1.0 pooled (CI-positive expectancy preferred).
5. Usable frequency ≥ ~10 events/pair/month after collapse.
Falsification: if no variant passes, premise rejected; next premise = MSS-displacement continuation (displacement ≥1.3×ATR break of 20-bar swing, continuation entry on retest), then compression breakout.

## 7. RAW NUMBERS
See tables in §3 and §4. Audit artifacts in repo `lab/`: `signal_edge_lab.py`, `signal_edge_analysis.py`, `signal_edge_events.csv` (5,294 events × 19 features + outcomes), `edge_analysis.log`, `protection_sweep.py`, `protection_results_all.json` (27 configs, cohorts, folds), `prot_baseline_trades.csv` (1,219 rows), `PROFIT_PROTECTION_STUDY.md`. Live ledger: `data/trades.jsonl` (238 trades). Parameters: `param_registry.json`.
Sim constants: spike filter rejects bars with range > SPIKE_RANGE_MULT = 3.0 × ATR; TP distance floor MIN_TP_DIST_FRAC = 0.006 (tp_frac = max(1.6×ATR/price, floor)).
