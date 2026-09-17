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
- **Circuit breaker** (2026-09-17): 3 consecutive losing closes trips a
  hard lockout on ALL new entries (account-wide) — the bot NEVER clears it
  itself. Only the "Unlock Trading" workflow can, and only with a logged
  reason. Audit trail: `data/circuit_breaker_log.jsonl`.
- **Trading mode** (2026-09-17): LIVE/PAPER flag persisted in
  `state/state.json` (`account.mode`, default PAPER). Switching via the
  "Set Trading Mode" workflow force-closes every open v5 position at market
  (tagged `MODE_SWITCH_FORCE_CLOSE`) first; it refuses if a sovereign pair
  still holds a position. The flag is an ADDITIONAL gate on top of
  LIVE_TRADING/LIVE_PAIRS/keys — sovereign real orders require all of them.
  Audit trail: `data/mode_log.jsonl`. Every trade records `mode` and
  `session_regime` (ASIAN/LONDON_EXPANSION/NY_OVERLAP/PACIFIC_MAINTENANCE)
  in both the ledger and the UI trade store.

## Workflows

- `tick.yml` — perpetual tick loop (~5 min cadence): run engine → commit state → chain next run.
- `reset.yml` — logged account/pair reset through the engine's own sync path.
- `diagnose.yml` — network + state health checks.
- `set_mode.yml` — founder-authenticated LIVE/PAPER switch: force-closes all
  open v5 positions, then flips the mode flag (Sec 5.3).
- `unlock_trading.yml` — founder-authenticated manual circuit-breaker clear
  (Sec 5.2); requires a reason, logs to the audit trail.

## Research

`backtest.py` drives the real `tick.py` on historical 1m candles.
`research/engine_comparison_2026-09-10.md` compares the pre-v4 wedge, v4 and
v5 engines — including the 2026-09-11 erratum on the wins/losses miscount.
