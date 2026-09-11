# Second Engine — Formula Definitions v1 (authored, not received)

**Status:** The owner supplied a "Laboratory Suite" doc that assumes a
"Joint Mathematical Specification" already defines `structure_quality`,
`move_potential`, `dist_to_wall`, `spike_risk`, and `overall_score`. That
spec does not exist anywhere in this repo or in anything previously
written for this project — searched exhaustively, zero hits. Rather than
guess at someone else's numbers, these are **my own v1 definitions**,
built to be precise, computable, and consistent with what's already locked
(the ATR spike language shared with Main G3, the MFE/MAE vocabulary, the
fractal swing detector in `engine/features.py`).

This is exactly what Phase 2b is for: run the labs, see if these
definitions actually separate good outcomes from bad ones, and only lock
what survives evidence. Nothing here is implemented in the live pipeline.

---

## 1. Structure Quality (5-bucket, `0.00 / 0.15 / 0.40 / 0.70 / 1.00`)

Inputs: last 40 closed 15m candles, fractal swings (`wing=2`, same rule as
`nearest_swing_tp`), candidate direction `side`.

```
swings_hi, swings_lo = fractal_swings(closed15m[-40:], wing=2)

level 0 (0.00) — undefined: fewer than 2 confirmed swings on either side
level 1 (0.15) — BOS only: latest same-direction swing exceeds the prior
                 one (higher-high for long / lower-low for short) — plain
                 continuation, no fresh reversal evidence
level 2 (0.40) — BOS staircase: 2+ consecutive same-direction confirmed
                 swings — stronger continuation
level 3 (0.70) — CHoCH: price has closed beyond the last COUNTER-direction
                 swing just before the signal — character just changed
                 in favor of the trade
level 4 (1.00) — MSS: CHoCH condition AND price also closed beyond the
                 second-to-last counter-swing — double-confirmed shift
```

## 2. Move Potential (`dist_to_wall` formula)

```
wall     = nearest OPPOSING fractal swing ahead of price in the trade
           direction (the same swing set used by nearest_swing_tp)
ratio    = dist_to_wall / tp_dist     (∞ / open-air if no wall found)

ratio >= 1.3           → 0.90   (clear runway past TP — strongest bucket)
1.0  <= ratio < 1.3     → 0.65
0.7  <= ratio < 1.0     → 0.40
ratio < 0.7             → 0.15   (wall sits inside TP distance — likely stall)
no wall (open air)      → 0.90

Overlay: if spike_risk bucket == "high" → move_potential = min(move_potential, 0.20)
```

## 3. Spike Risk (shared 3.0×ATR language with Main G3)

```
range_ratio = forming_1m_candle_range / ATR
score       = clamp(range_ratio / 3.0, 0, 1)   # 1.0 == AT the G3 spike gate

bucket: low    if score <  0.33
        medium if score <  1.00
        high   if score >= 1.00   (== the exact G3 spike block condition)
```

## 4. Overall Score (advisory only, never gates a trade)

```
overall_score = 0.4 * (2*structure_quality - 1)
              + 0.4 * (2*move_potential   - 1)
              + 0.2 * (1 - 2*spike_score)
              clipped to [-1, 1]
```

Weights: structure and move potential carry equal weight (0.4 each);
spike risk carries 0.2 and is inverted (high spike = bad = pulls score
down). This is the only free parameter set here — Lab 4/5 results are
the evidence for adjusting it, never intuition.

## 5. What "correct" means for the Agreement Matrix (Lab 4)

```
Main correct    := the candidate signal's forward path closed at TP
Second correct  := (overall_score >  0.0) predicted TP-hit, OR
                    (overall_score <= 0.0) predicted NOT-TP-hit
```

## Decision rule (unchanged from the owner's doc, §7)

1. Labs 1–3 show clear, monotonic rankings in the expected direction →
   lock into `param_registry.json`.
2. Any ranking flat or inverted → stop, adjust the definition above with
   evidence, do not implement.
3. Lab 4: if "both correct" isn't materially better than the other cells,
   the Second Engine opinion is not informative on this sample.
4. Lab 5 is simulation only — a positive result does not authorize a live
   size change without a separate registry event.


---

## LAB VERDICTS — 2026-09-11, 5 pairs x 30d, 19,960 candidate signals

(Engine: lab_suite.py, data: data_*_30d.json, evidence CSV: lab_suite_signals.csv.)

**Lab 2 — Move Potential: VALIDATED, lock-eligible.** Clean monotonic
ranking across every bucket: TP-hit 43.1% (wall inside TP) -> 56.4% ->
56.6% -> 64.0% (wall >= 1.3x TP away), avg wall_ratio 0.28 -> 2.75.
The single strongest finding of the suite. dist_to_wall is real signal.

**Lab 1 — Structure Quality: NOT LOCKED.** Ranking flat-to-inverted
(0.40 bucket outperforms 0.70 and 1.00 on simple EV). Per decision rule
2: the definition needs evidence-driven adjustment, not implementation.

**Lab 3 — Spike Risk: ADVISORY ONLY.** Direction correct (high-spike
bucket: 37.4% TP-hit vs ~51% baseline) but n=179 is too thin to lock.

**Lab 4 — Agreement Matrix: weakly informative.** both_correct 45.5%;
second engine right-when-main-wrong 7.6% vs wrong-when-main-right 5.0%.
Directionally useful at the margins, not yet a filter.

**Lab 5 — Joint EV Impact: +3.9% modulated vs baseline.** Encouraging,
simulation only, explicitly NOT a live size authorization.

### Recommendation to owner (AI advises, owner authorizes)
Lock dist_to_wall / move_potential thresholds as Phase 2b's surviving
result; keep the Second Engine advisory-only (log overall_score per
signal) until an out-of-sample period confirms. Structure-quality
definition to be re-derived with evidence before any lock attempt.
