# Mathematical Specification — v5 Engine

**Phase 2 of the owner's production path (2026-09-11).** Every decision rule
is explicit and measurable. No vague instructions. Status tags:

- **[IMPL]** — implemented; the formula below is the exact live code
- **[SPEC]** — specified precisely; pending Phase 3 implementation

Global notation: `p` = current price, `E` = entry price, `A` = 1m ATR,
`q` = open position, `tf` = TP distance as fraction of price, `W`/`L` =
wins/losses since reset, `B` = balance.

---

## 1. Entry score **[IMPL — as binary gates; scoring upgrade SPEC]**

**Live gates (ALL must pass; any fail → WAIT):**

```
G1  signal      pullback candle against the live momentum candle in the
                direction of the 15m bias (want = long|short)
G2  transition  if TRANSITION_GATE=1: no counter-4h-reversal in progress
G3  spike       |high−low| of forming 1m candle ≤ 3.0 × A      (SPIKE_RANGE_MULT)
G4  volume      pullback candle volume ≤ 1.2 × avg(last 10)    (heavy vol = reversal, skip)
G5  EMA slope   EMA_fast(3) must slope in trade direction       (SIG_EMA_SLOPE)
G6  no-chase    |p − EMA_fast| ≤ 0.5 × A                       (move not extended)
G7  cooldown    ≥ 90 s since last entry on the pair            (ENTRY_COOLDOWN_SEC)
G8  inventory   wedge cap: float on the side < wound threshold; MAX 8 slots
G9  EV gate     (see §16)
```

**[SPEC] upgrade to continuous score:**

```
score = 1[G1..G8] · sigmoid(k·(ev_frac − EV_MARGIN_REQ)) · alignment_mult
```
where alignment (trade direction == 15m bias) multiplies score by TREND_MULT.
Entry iff `score ≥ S_MIN` (lock S_MIN a priori; sensitivity-test in Phase 4).

## 2. Market-regime classification **[IMPL — chop/spike/runaway]**

```
spike    : forming 1m range > 3.0 × A                      → block entries this tick
runaway  : last 6 closed 15m candles all one color         → block counter-trend entries
chop     : default                                           → normal operation
```
`TREND_RUNAWAY_CANDLES = 6` (locked, wedge lesson).
**[SPEC] Phase 7 extends this to trend / range / expansion / reversal via
structure events (§4) rather than candle colors alone.**

## 3. Liquidity detection **[SPEC — Phase 7]**

Per pair: 1m candle volume z-score vs 60-candle rolling window; liquidity
sweep = wick beyond a prior 15m extreme with close back inside within ≤3
candles and volume z ≥ +2. Sweeps mark liquidity, not direction; they arm
structure rules below.

## 4. MSS / BOS definitions **[SPEC — Phase 7]**

```
swing_high(i) : 15m candle high > highs of the 2 candles on each side
swing_low(i)  : 15m candle low  < lows  of the 2 candles on each side
BOS  : close beyond the last confirmed swing in the direction of the
       prevailing 15m bias              → continuation confirmation
MSS  : close beyond the last counter-swing against the prior direction,
       with the sweep precondition (§3) on the opposite extreme
       → regime-shift event, blocks new trend-following entries, arms
       reversal retracement rules (§7)
```

## 5. FVG qualification **[SPEC — Phase 7]**

```
FVG_long : low(candle i+1) > high(candle i−1)      on a 1m chart
qualifies iff: gap size ≥ 0.25 × A, formed in direction of regime,
               and unfilled by later closes
```
Qualification thresholds locked a priori; a qualified FVG is a retracement
magnet — position protection (§11) tightens when price trades INTO a
counter-direction FVG.

## 6. TP calculation **[IMPL]**

```
tp_dist = max(1.6 × A, 0.006 × p)                    (SCALP_TP_ATR, MIN_TP_DIST_FRAC)
if SWING_TP=1 and a swing level lies in
   [0.006·p, tp_dist·SWING_MAX] ahead → snap tp to the level
aging  (TP_AGING=1): after h hours stuck, keep fraction f of tp_dist:
   [(6 h, 0.5), (24 h, 0.25), (72 h, 0.10)]
```
Aging relaxes the target only; forced exit is §13.

## 7. Retracement classification **[P4 — from P1 telemetry]**

For each open position, per tick:

```
r = (mfe_frac − current_favorable_frac) / mfe_frac     ∈ [0,1]
healthy : r <  R1  and regime unchanged        → hold
warning : r ≥  R1  or regime turned against   → tighten (§11 tier 2)
inval.  : r ≥  R2  or MSS against position    → exit
```
`R1, R2` locked per symbol from the empirical MFE→retracement→P(TP)
distribution (seed: NEAR 30d, 146 trades — see
research/mfe_mae_seed_NEAR_30d.json). Not yet locked: requires the harvest
from Phase 4 backtests across all five pairs. **Principle: thresholds come
from measured distributions, never judgment.**

## 8. MFE/MAE thresholds **[IMPL]**

Measured per tick from candle extremes since entry; persisted round-trip
(mf/me keys). Classification at close (principle 2):

```
target_success : reason = TP
target_failure : mfe ≥ 0.5 × tp_frac   (reached half the target, failed)
trade_failure  : mfe < 0.25 × tp_frac  (never went anywhere)
mixed          : otherwise
```
Empirical seed (NEAR 30d, 146 trades): reach 0.5×tp → 91% eventual TP;
MAE ≥1% → 62% still TP; winners' MAE p90 = 2.89%.

## 9. Position sizing **[IMPL]**

```
per_unit   = tf − 2×FEE                                (FEE = 0.0006/side)
target     = 0.0167 × B  (≈ $0.05 at $3)  × TREND_MULT if aligned
slot_cap   = B × 0.85 × 10 / 8                          (MARGIN_BUDGET, LEVERAGE, MAX_POSITIONS)
notional   = min(target/per_unit, slot_cap, margin_left×10, 40)
margin     = notional / 10 ; skip if notional < 1
```
Global constraint: Σ margin ≤ 0.85 × B across all pairs.

## 10. Emergency exits **[IMPL]**

```
LIQ model  : adverse ≥ (1/10 − 0.005)         → LIQ        (exchange force-close simulation)
HARD_SL    : adverse ≥ 0.06                   → HARD_SL    (locked airbag, never relaxed)
MAE_KILL   : mae_frac ≥ 0.04                  → MAE_KILL   (P2 ceiling; per-symbol in Phase 4)
```

## 11. Dynamic protection **[IMPL — tier 1]**

```
armed   : mfe_frac ≥ 0.5 × tp_frac
BE_STOP : armed and p crosses E×(1 ± 0.0019) against the position
          (buffer = 2×FEE + 2×slip + crumb; exit never locks a loss)
```
**[SPEC] tier 2 (P4):** warning state (§7) moves protection to
`max(BE price, p − k×A)` trailing by k×ATR, k locked a priori.

## 12. Re-entry rules **[IMPL — minimal; full gate SPEC]**

Live: 90 s per-pair cooldown; after 3 consecutive SLs on one side the next
entry flips direction (SL_STREAK_REVERSAL); after 4 consecutive SLs → 1 h
pause (MAX_STREAK, STREAK_PAUSE_SEC).
**[SPEC]** re-entry after a trade_failure exit requires: cooldown elapsed +
G1..G8 re-passed + a NEW signal (not the aged one) + the re-entry EV premium
`ev_frac ≥ EV_MARGIN_REQ + REENTRY_PREMIUM` (lock premium > 0 a priori).
**Averaging down is prohibited absolutely (owner principle 1).**

## 13. Time-based exits **[IMPL — MAX_AGE; duration model SPEC]**

```
MAX_AGE : held ≥ 48 h → force close at market          (MAX_POS_AGE_HOURS, locked)
```
**[SPEC — P3] expected duration model:** per symbol+regime, the empirical
distribution of `minutes_held` for target_success trades (seed: NEAR median
98 min). A trade older than the p75 of its cohort with mfe < 0.5×tp_frac is
stale → tighten to tier-2 protection or exit at BE if armed.

## 14. Strategy switching **[IMPL — registry discipline]**

No silent adaptation: every parameter is locked in `param_registry.json`;
the engine refuses to trade on any mismatch (registry_check at boot, state
poison + grace period for open positions). Parameter changes are owner-
authorized events, committed with rationale, never tuned on results in flight.

## 15. Portfolio-level risk **[IMPL]**

```
DAILY_LOSS_LIMIT : B_day_start − B ≤ 12% → RISK-OFF until next UTC day
MARGIN_BUDGET    : Σ margin ≤ 85% of B
MAX_POSITIONS     : 8 slots global
```

## 16. Expected-value threshold **[IMPL]**

```
p_win   = (20 × 0.85 + W) / (20 + W + L)               (EV_PRIOR_WEIGHT, EV_PRIOR_WINRATE)
ev_frac = p_win × (tf − 2×FEE) − (1−p_win) × 0.02 − 2×0.0001
                                                       (EV_ASSUMED_LOSS_FRAC, SLIP_ASSUMED_PCT)
trade   ⇔  ev_frac ≥ 0.0005                            (EV_MARGIN_REQ) and §9 sizing > 0
else    →  WAIT with the numeric reason persisted
```

---

## Open items (locked before Phase 4)

| Item | Needs |
|---|---|
| §1 S_MIN scoring gate | decide gate-vs-score after Phase 4 A/B |
| §7 R1/R2 retracement bands | empirical harvest, 5 pairs |
| §3–5 liquidity/MSS/BOS/FVG thresholds | a-priori locks + adversarial validation |
| §12 REENTRY_PREMIUM | distribution of post-exit continuation |
| §13 duration model | per-symbol minutes_held harvest |
| §10 MAE ceiling per symbol | replace global 4% with per-pair empirical p99 |
