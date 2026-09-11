# Engine Generation Comparison — 30-Day Backtest (2026-09-10)

**Method.** Identical 30-day NEAR/USDT 1m candle window (2026-08-11 → 2026-09-10,
43,200 candles, zero gaps, Bitget USDT-FUTURES). Each engine generation ran
against the same data with its own backtester, $2.653 start, same paper-fill
model. The backtester has NO liquidation model — see Wedge caveat.

| Metric | Wedge (pre-v4) | v4 hardened | v5 EV+regime |
|---|---|---|---|
| Trades | 169 | 70 | **4** |
| Win rate | 100%* | 95.7% | 75% |
| Net P&L | +$9.00* | −$0.30 | **+$0.046** |
| Profit factor | INF* | 0.74 | **1.73** |
| Avg win / avg loss | +$0.053 / 0* | +$0.013 / −$0.381 | +$0.036 / −$0.063 |
| Max closed drawdown | $8.99* | $1.02 | **$0.11** |
| Min floating equity | **−$5.91*** | $1.63 | $2.52 |
| Capital-hours in market | 2,556 | 294 | **1.1** |
| Exit reasons | 169 TP* | 67 TP, 3 MAX_AGE (−$1.14) | 3 TP, 1 HARD_SL |

*\*Wedge numbers are a paper mirage. With no liquidation model, its floating
equity hit −$5.91 on a $2.65 account — every position would have been
force-liquidated live long before those 169 "TP wins." The 2026-09-06 live
wedge wipeout is what that row looks like in reality. The backtest also lets
it compound beyond real margin constraints.*

## Reading

- **v4 killed the mirage but kept the bleed.** Realizing MAX_AGE losses
  (−$1.14 across 3 exits) is honest accounting, but 67 tiny wins (+$0.013 avg)
  couldn't cover them: PF 0.74, net −$0.30. The grind itself was EV-negative.
- **v5 refuses the grind.** The EV gate filters to trades whose TP distance
  clears fees + assumed slip + expected kill loss. 4 trades instead of 70.
  Bigger wins (+$0.036 avg — higher-vol entries), tighter losses (−$0.063 —
  the 6% HARD_SL bounds the tail), PF 1.73, and capital-hours drop by ~99%
  vs v4: risk is priced per trade, not accumulated by exposure.
- **Risk-adjusted**: v5 net/maxDD = 0.42; v4 is negative; wedge is
  meaningless (DD implies liquidation).

## Honest caveats

1. **Sample size.** 4 trades in 30 days is selective by design — but it is a
   tiny statistical sample. One HARD_SL exit does not establish tail behavior.
2. **In-sample design.** The v5 gates were locked a priori relative to v5's
   own runs, but the wedge-lesson inputs (runaway regime, tail discipline)
   came from this same history. This comparison is therefore partially
   in-sample; the live epoch started 2026-09-10T14:44Z is the true out-of-
   sample test.
3. **One venue, one asset, one month.** Regime diversity untested; 30 days of
   NEAR behavior is one draw from the distribution, not the distribution.

Reproduce: `data_1m_30d.json` (not committed — 43,200 candles; re-fetch via
Bitget API), harness = per-era git worktree of `tick.py`/`backtest.py` at
d7f89e3 (wedge), e173fd1 (v4), main (v5).

---

## ERRATUM — 2026-09-11: wins/losses miscount in the v5 state path

**Bug found during the multi-pair refactor.** The v5 state sync incremented
`wins` on *every* close (a stale "TP-only: every close is a win" comment from
the pre-v4 hedge era), while losses stayed flat. The EV gate blends state
wins/losses into p̂ — so after a HARD_SL kill the gate saw a fake win and
**loosened itself**, allowing the marginal grind the honest gate refuses.

**Corrected 30-day v5 numbers (same data, fixed accounting):**

| Metric | Reported (buggy) | Corrected |
|---|---|---|
| Trades | 4 (3 TP, 1 HARD_SL) | 2 (1 TP, 1 HARD_SL) |
| Net | +$0.0458 | **−$0.0414** |
| PF | 1.73 | 0.34 |

Sequence: the first HARD_SL (−$0.063) drops p̂ to ~0.66; the honest gate then
WAITs through windows the inflated gate (p̂ ≈ 1.0) traded. The two extra TPs the
buggy version caught were selected by a gate lying to itself about its own
edge. The comparison's *qualitative* conclusion stands (v5 is the only design
whose losses are bounded and whose gate is honest) — but the quantitative
edge of the single-pair v5 on this window is **negative after the fix**.

This is exactly the failure mode the v4 discipline warns about: a gate that
tunes on results, implemented by accident at the accounting layer. All other
engine generations in this comparison remain unaffected (they pre-date the
multi-position close path that carried the bug).

## ARCHITECTURE — 2026-09-11: multi-pair expansion (owner-approved)

- **5 pairs**: NEAR, BTC, ETH, SOL, XRP (Bitget USDT perps; PAIRS locked in
  param_registry.json a priori).
- **Shared account** ($3 paper), **global budgets**: 8 slots and the 80%
  margin cap are enforced across all pairs combined (per-pair context
  injected via `_other_open` / `_other_margin`).
- **Per-pair intelligence**: each pair keeps its own regime classifier, EV
  gate p̂ (per-pair W/L blended with the 0.85 prior), positions, heartbeat.
- **State lives in the repo**: `state/state.json` committed by the tick
  workflow every run — git history is the audit trail, raw.githubusercontent
  is the public dashboard feed, no secrets and no external middleware.
  The legacy Base44 backend (superagent-ae0aaf02, single-pair, schema
  whitelist) is retired with this change.
- **MIN_TP_DIST → MIN_TP_DIST_FRAC (0.00125)**: the old absolute $0.003 TP
  floor was tuned for NEAR's $2.4 price; multi-pair spans $1.3–$77k, so the
  floor is now price-proportionate (registry-locked; NEAR-equivalent).
