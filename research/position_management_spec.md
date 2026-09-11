# Position-Management System — Owner Spec & Build Plan

**Owner standard (2026-09-11, verbatim intent):** the system is complete only
when, at every point after entry, it can answer — *"Is this trade still
behaving like the historical trades that justified entering it, and what is
the mathematically best action now?"*

## The owner's tree (the contract)

```
SIGNAL → ENTRY
  ├── TP progression          (normal / near TP / TP rejection)
  ├── Retracement engine     (healthy pullback → hold / warning → tighten / invalidation → exit)
  ├── Risk engine             (dynamic SL / maximum MAE / emergency kill)
  ├── Time engine             (expected duration / stale trade / forced exit)
  ├── Market-regime engine    (trend / range / expansion / reversal)
  ├── Liquidity/structure     (sweep / MSS / BOS / FVG & structure failure)
  └── Position recovery      (partial close / move protection / re-entry eligibility / final exit)
```

**Owner principles (binding):**
1. **No averaging down.** Recovery logic manages the existing position; it
   never adds risk to rescue a bad entry. Re-entry only via a separate,
   statistically validated re-entry gate.
2. **Trade failure ≠ target failure.** Immediately-wrong entries and
   moved-substantially-then-failed entries get different management.
3. **Empirical fallbacks.** Per symbol and regime, the engine must know
   MFE → retracement → P(TP) and MAE → recovery probability → P(further loss).

## Gap map (2026-09-11, before this build)

| Tree node | Status before | 
|---|---|
| TP progression | partial — TP_AGING relaxes target; no near-TP/rejection logic |
| Retracement engine | **absent** |
| Risk engine | **DONE (P2, 2026-09-11)** — dynamic SL (BE_STOP), MAE ceiling (MAE_KILL), HARD_SL airbag |
| Time engine | partial — MAX_AGE forced exit; no expected-duration model |
| Market-regime engine | partial — chop/spike/runaway; no trend/range/expansion/reversal split |
| Liquidity/structure | **absent** |
| Position recovery | **absent** |
| Empirical distributions | **absent — nothing recorded** |

## Phase 1 — TELEMETRY (DONE, shipped 2026-09-11)

- Every open position tracks **MFE/MAE from candle extremes** since entry,
  round-trip-safe through the compact position serializer (mf/me keys).
- Every ledger record now carries: `trade_state`, `mfe_frac`, `mae_frac`,
  `minutes_held`, `tp_frac`, alongside PnL/balance.
- **trade_state classification** (principle 2), computed at close:
  - `target_success` — TP hit
  - `target_failure` — MFE ≥ 50% of TP distance, never hit
  - `trade_failure` — MFE < 25% of TP distance (immediately wrong)
  - `mixed` — between

## Phase 1.5 — EMPIRICAL SEED (DONE — see `mfe_mae_seed_NEAR_30d.json`)

146-trade NEAR 30-day harvest under the released config:
- Trades reaching ≥50% of TP distance: **141/146**; of those **128/141 = 91%**
  eventually hit TP → the target-failure boundary is statistically real.
- **MAE ≥ 1% en route: 47 trades → 29 (62%) still hit TP**, 7 HARD_SL,
  11 MAX_AGE → giving deep retracements room WAS profitable in this sample.
- TP trades saw **median 0.54% MAE** en route — winners routinely bleed
  half a percent before paying.
- HARD_SL kills took **median 15.2 HOURS** to die — the 6% backstop lets
  doomed trades rot; the time engine + dynamic SL will compress this.

## Phase plan (in build order)

- **P2 — RISK ENGINE (DONE, shipped 2026-09-11).** BE_TRIGGER_FRAC 0.5 /
  BE_BUFFER 0.19% / MAE_CEIL 4% — all registry-locked with empirical
  rationale. A/B on NEAR 30d: release config net −0.7316 (7 HARD_SL,
  median 15.2h rot) → P2 net **+0.2850**, 96% WR, 295 BE_STOP scratches
  banking +0.654, 159 TPs, 19 MAE_KILLs, **zero HARD_SL deaths**, median
  doomed-trade lifetime 15.2h → 5.2h.
- **P3 — Time engine:** expected duration per pair/regime from the ledger
  (seeded from backtest harvests); "stale" = holding beyond the historical
  TP-time distribution with poor MFE → tighten or exit.
- **P4 — Retracement engine:** MFE-retracement bands per regime; healthy
  pullback → hold, warning → tighten, invalidation (structure break) → exit.
- **P5 — TP progression:** near-TP handling and TP-rejection detection.
- **P6 — Position recovery:** partial closes at MFE milestones, move
  protection, re-entry eligibility via a separate validated gate
  (principle 1 — never averaging down).
- **P7 — Liquidity/structure:** sweep, MSS/BOS, FVG detection feeding the
  regime and retracement engines.

Every phase locks its params a priori in `param_registry.json` with the
empirical rationale attached.
