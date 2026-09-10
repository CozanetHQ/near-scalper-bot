# NEAR Scalper Bot

Paper trading bot for NEARUSDT on Bitget Futures. Runs 24/7 via GitHub Actions cron.

## How it works

- GitHub Actions fires every 5 minutes (free, no external billing)
- Each run calls the Base44 backend function `nearScalperTick`
- The function internally polls Bitget every 15s for ~4.4 minutes
- Strategy: 4H + 15M bias alignment → 1M entry signal → $0.20 TP / $0.15 SL

## Architecture

- **Scheduler**: GitHub Actions (this repo, `.github/workflows/tick.yml`)
- **Engine**: Base44 backend function `nearScalperTick`
- **State**: Base44 entities `NearScalperState` + `NearScalperTrade`
- **Dashboard**: Base44 artifact / `nearScalperDashboard` function
- **Market data**: Bitget public REST API (no key needed)

## Trading Rules (locked)

| Item | Rule |
|---|---|
| Symbol | NEARUSDT (Bitget USDT-M perpetual) |
| Higher TF Bias | 4H + 15M must be same color |
| Entry | 1M candle turns to bias color |
| Win target | ~4% of balance per full TP ($0.10 at $2.65 → $0.19 at $5+) |
| Take Profit | 2.2 × SL distance (ATR-scaled, target-solved sizing) |
| Stop Loss | 2.2 × 1m ATR, floor $0.006 |
| ATR gate | 0.0015 day / 0.0020 night (21:00–07:00 UTC) |
| Leverage | 10x Isolated |
| Max Positions | 1 |
| Start Balance | $3.00 virtual |

## Manual trigger

Go to Actions tab → "NEAR Scalper Tick" → "Run workflow"

## v4 Falsification-Hardened Spec — What Was Actually Implemented (2026-09-10)

The owner supplied a full institutional kill-switch/falsification spec (Tier 0-3
kill infra, Hypothesis Registry, Independent Uncertainty Engine, Vault retirement,
regime probability engine, etc.). Honest scope note: **this bot is a single Python
process polling Bitget's public REST API from GitHub Actions with no real order
routing** — it cannot legitimately claim exchange-side resting stops, an
independent second-host watchdog, a hedge instrument, or a venue-level dead-man's
switch. Implementing believable versions of those would require real infra this
project doesn't have. See `param_registry.json` → `known_gaps_not_claimed` for the
full list of spec sections not implemented, and why.

What **is** implemented, load-bearing, and real:
- **`param_registry.json`** (Section 1.1) — every locked risk/execution parameter
  now carries a timestamped, version-locked definition. `tick.py` checks its live
  constants against this file every tick; a mismatch hard-blocks new entries
  (existing positions still ride their kills normally) and alerts Telegram.
- **HARD_SL_FRAC (12%) + MAX_POS_AGE_HOURS (48h)** — a real, always-on protective
  kill on every open position. This replaces the prior design ("TP only, no SL,
  no time-stop... no loss is ever realized") with forced exits. **This is a
  deliberate behavior change**: the bot will now realize real (paper) losses it
  previously let float indefinitely as unrealized wedges.
- **Reset discipline (Section 10.1)** — `reset.yml` now requires a `reason` input
  and commits every reset to `data/reset_log.jsonl` as an immutable audit record
  before resetting state.

Not implemented (see registry for the full, honest list): Tiers 1-3 kill infra,
regime/probability/independent-uncertainty engines, Hypothesis Registry with
N≥200 independent events per regime, Vault retirement, block-bootstrap CPCV
validation. `sweep.py`/`research.py` currently grid-search parameters directly
against backtest results — the exact practice v4 Section 1.1 treats as
unfalsifiable, not a passing validation protocol.

## v5 Market-Intelligence Decision Layer (2026-09-10) — Market Intelligence Research Report

The owner's Market Intelligence Research Report's core decision equation is
`EV(trade|S) = E[PNL] - fees - spread - slippage - funding - adverse_selection -
failure_risk`, executed "only when EV is above a dynamic threshold that itself
depends on uncertainty, market regime and recent model degradation... If this
number is not positive by a safety margin, the correct trade is WAIT."

Scoped honestly to what a single-process Bitget paper bot can measure, v5 adds:

- **EV-after-costs entry gate** — `EV = p*win − (1−p)*kill − fees − assumed slip`,
  per unit of notional. `p` blends a locked prior (0.85) with observed W/L since
  reset. Below the locked `EV_MARGIN_REQ`, the engine WAITs instead of trading.
  This is a degradation brake: when realized win rate drops below the geometry's
  breakeven (~0.81 at the 6% kill vs ~1.4% net TP), new entries stop.
- **HARD_SL_FRAC re-locked 0.12 → 0.06 (a priori)** — the old value sat beyond
  the ~9.5% liquidation point at 10x leverage, so live it could never be the
  first exit. 0.06 caps tail loss at 60% of slot margin and makes the scalp
  geometry viable (breakeven p ≈ 0.81 < 93.8% backtest win rate).
- **Regime classifier** — deterministic, from data already fetched: `runaway`
  (6+ same-color 15m candles → counter-trend entries blocked, the wedge lesson),
  `spike` (live 1m range > 3x ATR → entries blocked that tick), else `chop`.
- **WAIT as a real position** — the wait reason persists to state (snapshot
  `w` key), shows on the dashboard, and alerts Telegram once per reason change.
  A silent flat book is now an explained decision, not a mystery.

**Honest backtest verdict (same window as v4's):** v4 = 48 trades, 93.8% win
rate, net −$1.17, maxDD −$2.09. v5 = 2 trades, net +$0.09, maxDD −$0.04. The
gate identifies that low-vol weeks make the TP-vs-kill geometry structurally
EV-negative and declines to grind. Values were locked BEFORE this backtest ran
(v4 Sec 1.1) and are not re-tuned on its results.
