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
| Take Profit | +$0.20 |
| Stop Loss | -$0.15 |
| Leverage | 10x Isolated |
| Max Positions | 1 |
| Start Balance | $3.00 virtual |

## Manual trigger

Go to Actions tab → "NEAR Scalper Tick" → "Run workflow"
