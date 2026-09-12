# Replacement Signal — Entry Requirements & Falsification Bar
Status: DRAFT for owner review · 2026-09-12 · derives entirely from lab/AUDIT_LOG_2026-09-12.md

## 1. Why this bar exists
Three levers were tested against the EMA-pullback scalp under the full multi-slot
live machinery (8 slots, NEAR 3-cap, 90s cooldown, 12% daily limit, 25% DD wall
with 1/4 recovery sizing, per-pair MAE walls, level fills, live costs):
  - chop gate (ER>=0.38, 15m): NO measurable benefit (CI crosses zero); regime label
    carries no outcome-discriminative power in the window
  - wall-based dynamic TP: no isolated benefit
  - bigger TPs (k=1.6..8 ATR): monotonically worse; gross edge negative by k=5
  - cheaper execution (maker + zero slip): still indistinguishable from zero
Conclusion: gross edge +0.08–0.10%/trade < round-trip cost ~0.14%. The strategy is
fee-limited. A replacement signal must therefore clear a bar tuned to that lesson.

## 2. The bar (ALL must pass — a candidate fails on any single miss)
B1. FEE-FIRST: Gross expectancy per trade ≥ 2x realistic round-trip cost (≥0.28%)
    before any gating/sizing refinement is considered. A signal whose winners move
    ~0.1–0.2% cannot amortize costs at any tuning — reject at the gate.
B2. MULTI-SLOT REALISM: the entire evaluation runs under the 8-slot live machinery
    with every locked risk rule active. Single-slot results are inadmissible
    (proven: they overstate via slot-freeing artifacts).
B3. NET, WITH INTERVAL: Net expectancy after 0.06% fee + 0.01% slip, 10k bootstrap
    95% CI entirely above zero (not merely a positive point estimate).
B4. WALK-FORWARD: positive net expectancy in EVERY chronological fold (≥3 folds).
    Fold-dependent edges are regime artifacts — see §4.
B5. PER-PAIR: every parameter locked per pair (owner standing rule 2026-09-11);
    no universal re-locks. A pair that fails its own bar stays on the bench.
B6. A PRIORI: parameters fixed from distributional evidence (MFE/MAE seeds, swing
    statistics) BEFORE the backtest runs; no post-hoc tuning on results. Registry
    lock discipline (Sec 1.1) applies from the first experiment.
B7. OUT-OF-WINDOW: final validation on a fresh 30d window not used for development
    or tuning. Both windows must independently satisfy B3.
B8. SAMPLE: ≥100 completed trades per pair in the evaluation window at the
    candidate's own holding profile; fewer = "insufficient evidence," not "pass."

## 3. What kind of signal can clear B1 (design guidance, not requirements)
The fee math requires trades that capture MORE PRICE MOVEMENT per trade:
larger favorable excursions (multi-hour structure moves, not 1–2 bar scalps),
which points at the owner's Shot-2 family: liquidity sweeps, MSS/BOS on 15m/1m
structure, FVG/pullback entries targeting the next structural level, EMA only as
micro-trigger. Holding time grows -> fewer trades -> fee per unit of movement
falls. B1 is the arithmetic forcing function; trade count B8 must still hold.

## 4. Known traps the lab already caught (a candidate must dodge them)
- Slot-freeing artifacts: improvements that only appear when skipping trades frees
  a scarce slot (test in both single-slot and multi-slot; only multi-slot counts).
- Win-rate theater: 89–91% WR coexisting with negative expectancy (BE_STOP-heavy
  distributions). Only net expectancy after costs is a headline metric.
- Level-fill optimism: BE_STOP at the buffer level and walls at exact fractions
  overstate fills slightly; a candidate must survive slip stress (3x slip).
- In-window regime luck: fold1 + vs folds 2–3 − divergence killed the chop gate;
  fold-uniformity (B4) is the control.

## 5. Promotion path after the bar is passed
Stage 1 — lab pass (B1–B8, both windows) -> results committed to lab/ with code.
Stage 2 — registry lock proposal per pair, owner authorization (constitution: AI
advises, humans authorize).
Stage 3 — live paper shadow: candidate runs read-only alongside the incumbent for
≥50 trades/pair; live net expectancy CI must agree with the lab.
Stage 4 — incumbent retirement decision by owner; registry swap; telemetry
continues (regime_at_entry, atr_frac_at_entry already instrumented).

## 6. Candidate intake template (use for every proposal)
- premise in one sentence; expected favorable excursion per trade and why
- target regime(s); holding profile estimate
- parameters + the distributional evidence each is derived from (B6)
- planned evaluation windows (development + holdout)
- what would falsify it (stated before running)
