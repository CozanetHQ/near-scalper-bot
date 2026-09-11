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


---

# LIVE AUDIT — 2026-09-11 (owner dashboard review, 120 live trades)

## CRITICAL: live-only cross-pair telemetry bug (found, root-caused, FIX PENDING OWNER AUTHORIZATION)

`fetch_candles()` defaults `symbol=SYMBOL` (line 241) where `SYMBOL = "NEARUSDT"`
hardcoded at module import (line 32). The signal path passes the real pair
(lines 570-573) — but the MFE/MAE telemetry scan (line 651) calls
`fetch_candles("1m", 15)` with NO symbol: **every pair's TP-scan and MFE/MAE
telemetry runs on NEAR candles.**

Consequences, all confirmed in data/trades.jsonl:
- ETH ($2.5k) / BTC ($77k) / SOL ($100) positions: mae_frac = (entry - NEAR_low)/entry
  ≈ 0.97-1.00 → MAE_KILL fires within minutes of opening, at ~$1 of real adverse
  movement. 100% of ETH live trades (8/8), both BTC trades, 4/5 SOL trades are
  bogus kills. Fingerprint: implied adverse extreme = $2.44-2.65 = NEAR's price,
  on every poisoned trade, all four pairs.
- XRP ($1.34) partially poisoned: NEAR highs read as bogus MFE (+0.87) → BE arms
  instantly → 12 BE_STOP scratches are bug-artifacts of arming, not real 50%-of-TP
  cohorts.
- MFE for ETH/BTC/SOL never accumulates (NEAR highs far below their entries) →
  BE protection could never arm on those pairs.
- The 120-trade live sample: NEAR 87t is CLEAN (its own candles); BTC/SOL/ETH
  live win rates (0%/20%/25%) are the BUG'S footprint, not strategy evidence.
- Sim backtests UNAFFECTED: the harness fetcher serves the pair's own candles.
- A BE-armed ETH exit (+$0.0054) was killed-and-labeled MAE_KILL because the
  ceiling check runs before BE_STOP — the one "MAE 0.0%" oddity (its mae had not
  yet round-tripped through a contaminated scan when the record was written).

Proposed fix (one line, awaiting owner authorization per the constitution):
`scan = fetch_candles("1m", 15, symbol)` — after which the contaminated live
records for BTC/SOL/ETH/XRP should be treated as void; NEAR's record stands.

## Item 1 — simultaneous LONG+SHORT on one pair

Intentional: COORD_MODE 0 (each slot trades its own signal) — now documented in
param_registry.json with the empirical evidence. Real structural gap flagged for
owner decision: no per-pair slot cap (live: 7/8 slots on NEARUSDT).

## Item 4/5 — designed asymmetry and target-failure ratio

Live NEAR (87t): TP 40x avg +$0.0224 | BE_STOP 41x avg +$0.0023 | MAE_KILL 6x
avg −$0.1407. PF 1.18 clean / 1.09 all-pairs. The win/loss asymmetry is the
registry-locked scalper structure (TP ~1.7% of notional vs MAE ceiling 4%,
HARD_SL 6% backstop). Target failure 41 vs success 40: BE_STOP catching
half-completed trades at breakeven is the P2 design working; the 51% post-arm
retrace rate vs the seed cohort's 9% is regime (post-Aug-19 chop). TP levels stay
locked a priori per v4 Sec 1.1 — no tuning on live results.

## Authorized changes shipped today (both validated in sim)

- RECOVERY_SIZE_FRAC 0.25: wall trips → ¼ slots (dust floor $1) → engine keeps
  trading instead of freezing. NEAR 30d: wall at trade #338, recovery era 3,836
  dust trades net −$0.32 (old behavior: silence). Honest first datapoint, on
  record in the registry rationale.
- DAILY_LOSS_LIMIT 0.12 wired: trips at ≥12% day loss → no entries until next
  UTC day → auto-releases at midnight (validated: Aug-15 00:09 trip → silence →
  Aug-16 00:00 release). Flag rides the last_reversal_at snapshot (drd).
- Registry-mismatch crash fixed: want/can_open/opened_this_tick hoisted so a
  lineage mismatch degrades to graceful WAIT instead of UnboundLocalError.
- Second Engine advisory scores now logged on every entry (advisory_score,
  wall_ratio), round-tripped through the compact position format (probe passed).
