# PROFIT-CAPTURE / TP-PROTECTION ENGINE — Study §18 (2026-09-13)

Owner spec: when a trade travels X% of its TP distance, arm a profit floor at
(X−R)% so a reversal exits with kept profit instead of scraping back to
BE_STOP. 40% activation was the HYPOTHESIS — not assumed. Full sweep, fold-
validated, identical taker costs everywhere (0.06%/side + 0.01% slip/side,
10x notional 1.0625 account-mult). Baseline = live exit stack as deployed.

## 1. Research findings (live session, 75 trades, real fills)
- ALL 60 BE_STOP trades reached an average of 68.5% of TP distance before
  reversing (p75 ≈ 81%) — the surrender is real and large.
- 11 TPs banked +$0.442 (avg $0.040); 60 BEs banked +$0.430 (avg $0.007);
  4 MAE_KILLs cost −$1.108. PF 0.79, expectancy −$0.003/trade.
- TP cohort holds 130m avg; BE cohort grinds 259m avg.

## 2. Mathematical reasoning
Protection is +EV iff: P(retrace→floor | armed) × (floor − BE_payoff)
  > P(dip→floor but would have reached TP | armed) × (TP_payoff − floor) + Δcosts.
The two error types pull opposite ways and activation X sets the mix:
- Early X (30-40%): nearly every trade arms (100% of live BEs pass 40%),
  so the floor converts many eventual TPs into protected exits — each cut
  winner costs (TP − floor) ≈ 45% of TP distance, each rescue gains only
  (floor − BE) ≈ 15-35 points. Bad ratio at high arm rates.
- Late X (70%): only proven travelers arm; the armed cohort's retrace-vs-TP
  odds are much better (57% still reach TP), and rescued trades have already
  banked most of the excursion. The live BE cohort's 68.5% mean progress says
  the information about survival sits near 70%, not 40%.
Floor placement R trades the same tradeoff a second time: too tight (r10)
retriggers on noise; too loose (r30) surrenders the kept profit.

## 3. Lab results table (30 days, 1219 baseline trades, taker costs)
Decision rule locked a priori: GO iff expectancy > baseline AND PF ≥ baseline
AND maxDD ≤ baseline AND ≥2 of 3 time-folds beat baseline's same fold.

```
config            exp%/tr    CIlo    PF   maxDD   TP  PROT   BE   MK  arm→TP  f1     f2     f3    GO
BASELINE          -0.0417  -0.098  0.84  40.7%  504     0  607   74     —   +.017  -.068  -.089   —
act70_r15         -0.0086  -0.066  0.96  36.5%  423   325  328   60   57%  +.080  -.047  -.096  GO
act70_r25         -0.0178  -0.073  0.93  33.6%  451   304  379   65   60%  +.055  -.052  -.076  GO
2stage40_r15      -0.0184  -0.063  0.91  38.9%  413  1042    0   70   28%  -.054  -.001  +.002  GO
act50_r20         -0.0344  -0.092  0.87  34.6%  511   614    0   73   45%  +.030  -.062  -.088  GO
RATCHET_REF       -0.0403  -0.097  0.84  35.7%  447   677    0   68   40%  -.027  -.039  -.060  GO
act40_r15..r30    -0.052x  -0.102  0.77  45.4%  ~408  ~894    0   68   31%   (all worse than baseline)
act30_r10..r30    -0.052x  -0.092  0.71  39.5%  340  1314    0   65   21%   (worst class)
act60_r25         -0.1006  -0.164  0.68  45.0%  447   438  198   80   51%   (worst single)
```

## 4. Recommendation — 40% is REJECTED
- **Recommended activation: 70%** (arm at 70% of TP distance).
- **Recommended protection level: 55%** (70% − 15% retracement allowance),
  clamped to ≥ round-trip costs (never a guaranteed loss; verified for TP
  distances 0.20%–1.20%).
- **Behavior**: <70% → existing stack unchanged (BE_STOP still arms at 50%);
  ≥70% → TP_PROTECTION_ARMED, floor at 55%; retrace through floor → PROTECTED
  exit; reach 100% → normal full TP.
- **Do NOT activate**: protection is pooled-tested; per-pair overrides will be
  locked per pair only when per-pair evidence diverges (owner per-pair
  constitution). No protection during... none needed — MAE walls, hedge logic,
  and EV gate are orthogonal and unchanged.
- **Expected effect**: expectancy −0.0417% → −0.0086%/trade (79% bleed cut);
  PF 0.84 → 0.96; maxDD 40.7% → 36.5%; BE_STOPs −46%; MAE_KILLs 74 → 60
  (protection rescues some armed trades before the wall). NOT profitable at
  taker costs — the mechanism reduces surrender; edge still must come from the
  signal. At maker fills the ratchet arm measured +0.068%/trade (§9-10); 70/55
  should be re-measured when maker fills exist.
- **Failure modes**: (1) chop regimes that repeatedly drive 70%→55% wicks
  convert slow winners into small wins; (2) gap moves straight through the
  floor exit at a worse price than the floor (paper SLIP_PCT=0 today); (3) at
  very tight TP distances the floor rides the cost clamp and behaves like a
  BE_STOP with extra steps; (4) pooled config may misfit a single pair —
  watch per-pair PROTECTED exit rates after 100+ live armed trades.
- **Interactions**: BE_STOP — kept, arms at 50%, protection supersedes it once
  armed (floor > buffer always). MAE_KILL — kept; walls still fire first on
  adverse excursions; protection reduced kills 74→60 in sim. Hedge engine —
  untouched (protection is per-position exit, hedging is portfolio layer).
  Adaptive TP — protection is defined relative to TP distance, so it inherits
  adaptivity automatically. Cost guard — the clamp is the cost guard.

## 5. State machine (implemented in tick.py)
NORMAL → [MFE ≥ 0.70×tp] → TP_PROTECTION_ARMED → [retrace ≤ floor] → PROTECTED_CLOSE
                                        └→ [price ≥ TP] → FULL_TP
Idempotent by construction: arming is a pure function of (MFE, TP distance);
the exit removes the position so repeated ticks cannot re-trigger.
Events logged: PROTECTION_ARMED (once per run per position), PROTECTION_EXIT
(symbol, side, entry, exit, TP price, TP progress %, protection level, MFE,
MAE, realized P&L, fees, timestamp). Reason code: PROTECTED.

## 6-9. Backend files modified
- tick.py — constants (PROTECT_ARM_FRAC 0.70, PROTECT_FLOOR_FRAC 0.55,
  DYN_FLOOR default ON), protection state machine + event logging in the
  DYN_SL block (after MAE_KILL, before BE_STOP check), cost-floor clamp,
  registry_check entries.
- param_registry.json — PROTECT_ARM_FRAC/PROTECT_FLOOR_FRAC locked with
  rationale; FLOOR_* (rejected 60/40) removed.
- engine/positions.py — unchanged (armed state re-derives from MFE; nothing
  persisted, by design).
- docs/index.html — PROTECTED badge + system panel note.
- tests: py_compile; import + registry_check; floor-vs-cost-buffer unit test
  across TP distances 0.20–1.20% (caught and fixed a real clamp bug); the
  full sweep harness (lab/protection_sweep.py) is the backtest.

## 10-12. Backtest comparison / before-after / failure cases
Before: exp −0.0417%/trade, PF 0.84, maxDD 40.7%, BE 607/1219 (49.8%).
After:  exp −0.0086%/trade, PF 0.96, maxDD 36.5%, BE 328 + PROTECTED 325.
Failure cases tested: 40% and 30% activation (REJECTED — 24-71% worse than
baseline), 60% activation (worst single config at r25), tight r10 floors at
mid activations (noise retriggers), two-stage 40→60 tighten (better than
static 40 but still 2x worse than 70/15).

## 13. Decision: GO — ship 70/55 (owner's 40% hypothesis rejected)
Shipped 2026-09-13 with owner authorization per spec ("if 40% performs worse
than another threshold, say so clearly and recommend the better threshold").
Fold-validated, pooled across the 5 trading pairs at identical taker costs.
