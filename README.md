# Multi-Pair Scalper Bot

Paper trading bot for 5 liquid Bitget USDT perpetuals (NEAR, BTC, ETH, SOL, XRP).
Runs 24/7 via a self-chaining GitHub Actions tick (watchdog cron at :07).

## Architecture (multi-pair v5)

- **Engine** (`tick.py`): per-pair v5 decision layer — EV-after-costs entry
  gate (per-pair p̂ blended from a 0.85 prior + observed W/L), regime
  classifier (runaway/spike/chop), 8-slot / 80%-margin **global** budget
  across all pairs, shared paper account.
- **State**: `state/state.json` — committed to git by the tick workflow every
  run. Git history is the audit trail; `raw.githubusercontent.com` is the
  public dashboard feed. No secrets, no external state backend.
- **Trade ledger**: `data/trades.jsonl` (append-only, immutable) + a 60-trade
  working window inside `state/state.json`.
- **Params**: locked a priori in `param_registry.json` (verified at runtime by
  `registry_check()`). Never tuned on results — see `research/` for the v4
  discipline and the engine-generation comparison.
- **Dashboard**: https://cozanethq.github.io/near-scalper-bot/ — live
  multi-pair terminal (balance, equity, per-pair regimes/WAITs, ledger).
- **Resets**: v4 Sec 10.1 discipline — logged in `data/reset_log.jsonl`,
  dispatched via the "Reset Account" workflow (queues behind any running tick).

## Workflows

- `tick.yml` — perpetual tick loop (~5 min cadence): run engine → commit state → chain next run.
- `reset.yml` — logged account/pair reset through the engine's own sync path.
- `diagnose.yml` — network + state health checks.

## Research

`backtest.py` drives the real `tick.py` on historical 1m candles.
`research/engine_comparison_2026-09-10.md` compares the pre-v4 wedge, v4 and
v5 engines — including the 2026-09-11 erratum on the wins/losses miscount.
