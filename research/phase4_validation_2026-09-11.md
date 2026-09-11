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

---

# P3 TIME ENGINE STUDY — 2026-09-11 (11,243 trades, 5 pairs, 30d fresh data)

## Duration distributions (pooled)
- TP: p50 17m, p90 157m, p99 360m — winners finish FAST.
- BE_STOP: p50 30m, p90 360m.
- MAE_KILL: p50 182m, p90 936m — killers die SLOW (the 3-16h lingerers).
- MAX_AGE (48h floor): only 49/11,243 trades.

## Conditional survival (the core P3 evidence)
| survived past | n | P(ever TP) | net-positive | combined net |
|---|---|---|---|---|
| 10m | 7,807 | 30.3% | 93.5% | -$8.74 |
| 60m | 4,043 | 22.7% | 88.8% | -$21.55 |
| 240m | 1,951 | 10.3% | 88.8% | -$14.59 |
| 480m (8h) | 300 | 7.3% | 54.7% | -$12.22 |
| 1,440m (24h) | 121 | 4.1% | 32.2% | -$5.85 |

The edge lives in fast completion; lingering positions are a net drag at every
age despite BE scratches. MAE_KILLs accrue disproportionately to 3-16h lingerers.

## Counterfactual: MAX_POS_AGE 48h -> 8h (full re-simulation, both windows)
| pair | full-30d delta | OOS-h2 delta |
|---|---|---|
| BTC | +0.1048 | +1.4301 |
| ETH | +8.1097 | +2.8513 |
| SOL | -1.0450 | +2.6587 |
| XRP | +0.2027 | -1.0357 |
| NEAR | -0.4301 | -0.5002 |
| TOTAL | +6.9420 | +5.4042 |

Per-symbol (owner standing rule: thresholds from empirical distributions PER
SYMBOL): 8h on BTC/ETH/SOL only, 48h unchanged on XRP/NEAR
= +7.16 full / +6.94 OOS — strictly dominates the global 8h in both windows.
XRP/NEAR winners resolve fast (TP p99 ~5h), so a cap only kills their rare
slow winners; BTC/ETH/SOL trades resolve slowly, so the cap removes their
toxic lingerers.

## AI recommendation (owner locks per v4 Sec 1.1 re-lock procedure)
Re-lock MAX_POS_AGE_HOURS per symbol: 8h BTC/ETH/SOL, 48h XRP/NEAR.
Caveats: single 30d dataset, deterministic re-simulation, two windows;
the wall/recovery dynamics interact with position lifetimes. Value picked
before any live results under it — a-priori discipline intact.

---

# P3 REVISION — PER-PAIR TIME ENGINE (owner directive 2026-09-11: no universal rules)

Owner standing rule, now structural: **every rule is tested and locked PER PAIR.**
The registry carries a `per_pair` section as locked source of truth; tick.py
resolves pair_param() = lab env > registry per_pair > global. This section
supersedes the pooled P3 recommendation above (8h-on-BTC/ETH/SOL mix).

## Per-pair TP-hit probability cliffs (conditional survival, 30d fresh dumps)
- BTC: TP p50 50m, p99 360m. P(TP|alive): 20.8% @10m → 6.9% @4h → 1.8% @6h.
- ETH: TP p50 50m, p99 835m (~14h — slowest winners). P(TP): 26.8% @10m → 9.8% @4h → 10.3% @12h.
- SOL: TP p50 22m, p99 352m. P(TP): 29.1% → 8.4% @4h → 1.9% @6h.
- XRP: TP p50 13m, p99 290m. P(TP): 31.8% → 13.9% @4h → 3.8% @6h.
- NEAR: TP p50 12m, p99 321m. P(TP): 36.9% → 15.4% @4h → 7.2% @6h.

## Age-cap counterfactual grid — full re-simulations, both windows (net, $)
| pair | 48h | 4h | 6h | 8h | 12h | verdict (both windows) |
|---|---|---|---|---|---|---|
| BTC full | +0.87 | −0.13 | −0.07 | **+0.98** | — | 8h wins both |
| BTC OOS | +2.34 | +0.11 | +0.83 | **+3.77** | — | |
| ETH full | +0.15 | +0.31 | +3.21 | **+8.26** | +4.95 | 8h wins both |
| ETH OOS | +2.01 | −0.11 | +1.31 | **+4.86** | +4.34 | |
| SOL full | +2.37 | −0.18 | **+6.60** | +1.33 | — | 6h beats 48h both |
| SOL OOS | +0.83 | −0.70 | **+1.66** | +3.49 | — | windows |
| XRP full | **−0.31** | −1.84 | −1.00 | −0.11 | — | keep 48h — every cap |
| XRP OOS | **+5.73** | −0.27 | +1.71 | +4.69 | — | hurts both windows |
| NEAR full | **+0.43** | −0.81 | −0.51 | +0.003 | — | keep 48h — every cap |
| NEAR OOS | **+0.88** | −1.67 | −0.36 | +0.38 | — | hurts both windows |

Methodology: survival curves nominate candidates; full re-simulation decides —
force-close-at-market ≠ the survivors' realized exits (BE protection does the
saving better than a market kill on XRP/NEAR, whose cliff is early but whose
tails still pay). One pair's optimum is another pair's loss — the universal 8h
rule is dead.

## Proposed per-pair locks (await owner authorization; values in the registry
## per_pair.MAX_POS_AGE_HOURS, currently 48h for all five)
- BTC 48h → 8h (+0.10 full / +1.43 OOS)
- ETH 48h → 8h (+8.11 full / +2.85 OOS)
- SOL 48h → 6h (+4.23 full / +0.83 OOS)
- XRP: unchanged 48h
- NEAR: unchanged 48h


---

# SESSION RESET — 2026-09-11T18:05:34.336583+00:00 (owner-authorized)

Owner directive: the $3 session never proved the engine out; reset to $10 as a
fresh, honestly tracked trial. If this session doesn't grow the account, that's
the signal to stop iterating on this engine shape and start refinement from
first principles.

**What changed:**
- Balance/peak balance hard-set: 2.9076 -> 10.0000
- All 5 pairs flattened to `fresh_pair_state()` (the engine's own clean-slate
  shape) — no open positions, no per-pair history carried forward.
- Per-pair wins/losses/total_trades reset to 0. These feed the EV gate's
  Bayesian blend (`p_win = (20 * 0.85 + wins_obs) / (20 + wins_obs + losses_obs)`)
  — so every pair starts this session purely on the 0.85 fixed prior and
  re-learns its own win rate from this session's trades only. This is a real
  behavior reset, not cosmetic — flagged explicitly so it's not mistaken for
  just a balance change.
- `account.session_start_at` / `session_start_balance` added — the dashboard's
  SESSION view filters to trades closed after this timestamp. The lifetime
  ledger (trades.jsonl) is untouched; nothing is deleted, only the live state
  is reset and the dashboard defaults to showing the new trial.

**Closed at reset (audit record, not injected into the automated trades.jsonl
ledger):** NEARUSDT held all 8 global slots at reset time (the known
no-per-pair-slot-cap gap flagged in the 2026-09-11 dashboard review), last
mark $2.5822. Sub-positions at reset:
- LONG entry 2.6497 -> tp 2.6791, mfe 0.21% mae 3.51%, opened 2026-09-11T16:08:10.926211+00:00
- LONG entry 2.6486 -> tp 2.6756, mfe 0.06% mae 3.47%, opened 2026-09-11T16:09:54.695228+00:00
- SHORT entry 2.5701 -> tp 2.5501, mfe 0.18% mae 1.85%, opened 2026-09-11T16:33:05.440994+00:00
- SHORT entry 2.5682 -> tp 2.5525, mfe 0.05% mae 1.92%, opened 2026-09-11T16:40:14.729839+00:00
- SHORT entry 2.5718 -> tp 2.5560, mfe 0.19% mae 1.78%, opened 2026-09-11T16:41:44.746957+00:00
- LONG entry 2.6096 -> tp 2.6253, mfe 0.13% mae 1.64%, opened 2026-09-11T17:28:00.356896+00:00
- LONG entry 2.6057 -> tp 2.6213, mfe 0.28% mae 1.49%, opened 2026-09-11T17:31:13.974004+00:00
- LONG entry 2.6075 -> tp 2.6231, mfe 0.19% mae 1.56%, opened 2026-09-11T17:37:09.997909+00:00
