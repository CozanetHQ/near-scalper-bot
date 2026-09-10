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
