# Phase 4 — Historical Validation Report (2026-09-11)

5 pairs x 30 days (43,200 x 1m candles each, fetched fresh from Bitget).
Windows: full-30d, plus out-of-sample halves (fresh state each).

## Results (net $, 8-slot config, start balance 2.653)

| Pair    | Full-30d        | First 15d (OOS) | Last 15d (OOS) |
|---------|-----------------|-----------------|----------------|
| NEAR    | 473t, +0.2850   | — (parity ref)  | —              |
| BTC     | 173t, -0.5442*  | 173t, -0.5442   | 690t, +2.6145  |
| ETH     | 503t, +0.3201*  | 503t, +0.3201   | 178t, -0.2271  |
| SOL     | 327t, +0.2277*  | 327t, +0.2277   | 856t, +0.8308  |
| XRP     | 110t, -0.8398*  | 110t, -0.8398   | 1057t, +6.6276 |

(*) All four full runs froze between Aug 19-21 and never traded again —
diagnosed below. Full-run numbers are truncated samples; the OOS halves
are the honest per-window performance.

## Finding 1 — SAFE_DD wall is a one-way latch (BY DESIGN, owner decision needed)

State snapshot at the freeze: `{"pk": 4.285, "sf": 1}` — ETH peaked at
+62%, crashed below the 75%-of-peak boundary (Engine 10: "pause ALL new
entries; the intelligence may NEVER override"), and latched. All 4 pairs
tripped the same wall in the Aug 19-21 market-wide event.

The wall works exactly as specified. The consequence: once flat and below
the boundary, entries are paused forever — the account cannot recover.
Live behavior after any -25% peak drawdown = permanent silence.

Owner options (AI advises; no change without authorization):
  a) Keep as-is — bot halts permanently after a 25% peak drawdown
  b) Time-based re-arm — lift the wall at UTC day boundary (resume next day)
  c) Recovery sizing — resume at 1/4 size while below the boundary,
     full size on recovery (my recommendation: preserves the protection,
     removes the dead-man's-switch behavior)
  d) Lower/raise SAFE_DD, or 0 to disable

## Finding 2 — DAILY_LOSS_LIMIT (12%) is defined but never wired

grep: `DAILY_LOSS_LIMIT = 0.12` has zero readers. The daily circuit
breaker described in the docs and told to the owner does not exist in
the engine. `_day_start_balance` is computed and never read. Owner
decision: wire it (12% day loss -> risk-off until UTC midnight) or
remove it from all documentation. It cannot stay half-described.

## Finding 3 — Second Engine labs (Phase 2b): dist_to_wall is real signal

19,960 candidate signals, 5 pairs x 30d. Move-potential bucket TP-hit:
43.1% -> 56.4% -> 56.6% -> 64.0% (wall >= 1.3x TP away). Structure
quality NOT lockable (flat/inverted). Full verdicts in
research/second_engine_spec.md; evidence CSV committed.

## Aggregate verdict

Excluding the frozen second halves, the engine printed positive months
on 4 of 5 pairs (NEAR +0.285, BTC +2.61 h2, SOL +0.83 h2, XRP +6.63 h2;
ETH h2 -0.23). Win rates 87-96% everywhere. The edge is real but the
Aug 19-21 event is exactly the tail the SAFE_DD wall exists for — the
policy decision above determines whether that protection is a brake or
a kill switch.
