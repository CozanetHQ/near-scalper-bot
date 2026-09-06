#!/usr/bin/env python3
"""
NEAR Scalper Bot — Tick Engine (runs inside GitHub Actions).

Paper trading only. Virtual $3 balance. No real orders.
Deterministic rules only — no LLM in the hot path.

Flow (every 15s for ~4.4 min):
  1. GET current state from Base44 (dashboard endpoint)
  2. Fetch forming 4H/15M/1M candles + ticker from Bitget
  3. Bias = 4H color == 15M color
  4. If position open -> ATR-scaled TP/SL, ratcheting trailing stop, time-stop
  5. If flat + bias + 1M RSI pullback trigger (dip-buy / spike-sell) -> open
  6. POST updates/trades to Base44 sync endpoint (secret protected)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

BITGET = "https://api.bitget.com/api/v2/mix/market"
SYMBOL = "NEARUSDT"
PRODUCT = "USDT-FUTURES"
ATR_PERIOD = 14
SL_ATR_MULT = 4.0     # GRID SEARCH WINNER (1152 configs): wide stop, rarely hit
TP_SL_RATIO = 1.5     # most exits are 20-min drift-capture time stops
WIN_TARGET_DOLLARS = 0.15  # OWNER 09-06: every TP nets $0.15 flat
SCALP_TP_ATR = 1.2   # TP distance = 1.2x 1m ATR (adaptive to live volatility)
MIN_TP_DIST = 0.002  # TP floor: never closer than this (fee math)
EMA_FAST = 9         # scalper momentum: EMA9 vs EMA21 on 1m closes
EMA_SLOW = 21
ATR_MIN = 0.0008      # GRID SEARCH WINNER: low gate — more shots on goal
NIGHT_ATR_MIN = 0.0020  # 21:00-07:00 UTC thin-session chop needs a much bigger move to be worth fees
MIN_SL_DIST = 0.006   # absolute floor so SL is never absurdly tight
TRAIL_TRIGGER = 99.0  # GRID SEARCH VERDICT: the trail CUT every winner early — OFF
TRAIL_DIST = 99.0     # (values > 1 disable the trail entirely)
TIME_STOP_MIN = 20    # recycle a stale position at market after N minutes
SL_STREAK_REVERSAL = 3  # after N consecutive SLs on one side, flip the next entry
SL_COOLDOWN_SECONDS = 240  # after a stop-out, wait before re-entering (chop protection)
SWEEP_ENABLED = False  # liquidity-sweep entries: tested, no added edge next to grid winner
SWEEP_15M_LOOKBACK = 12  # swing extreme = high/low of the last 12 CLOSED 15m candles (3h of structure)
SWEEP_WINDOW = 8         # the pierce may span up to 8 recent 1m candles (real sweeps take minutes)
SWEEP_REARM_SEC = 900    # after any close, wait before another sweep entry (no re-fire loops)
CONSOL_MAX_RANGE = 0.004   # last 8x15m range <= 0.4% of price -> consolidation zone
ZONE_ENTRY_POS = 0.25      # fade only within 25% of a zone edge — the middle of a zone is a chop trap
ZONE_TP_HAIRCUT = 0.25     # zone TP lands 25% of the range inside the far edge (don't get greedy at the wall)
DAILY_LOSS_LIMIT = 0.12    # RISK-OFF for the rest of the UTC day after losing 12% from day-start balance
MAX_STREAK = 4             # 4 consecutive SLs on one side -> extended pause
STREAK_PAUSE_SEC = 3600     # ... for one hour
RSI_PERIOD = 14
RSI_LONG_ENTRY = 50    # GRID SEARCH WINNER: loose gate — more entries, more edge
RSI_SHORT_ENTRY = 50
RSI_EARLY_VELOCITY = 3  # early entry: RSI still below/above gate but swinging this fast
LEVERAGE = 10
START_BALANCE = 3.0
FEE_RATE = 0.0006
POLL_INTERVAL = 15
MAX_RUNTIME = int(os.environ.get('MAX_RUNTIME', 240))  # ~4 min loop; next run chains immediately

BASE44_DASHBOARD = "https://superagent-ae0aaf02.base44.app/functions/nearScalperDashboard"
BASE44_SYNC = "https://superagent-ae0aaf02.base44.app/functions/nearScalperSync"
TICK_SECRET = os.environ.get("TICK_SECRET", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode())


def http_post(url, payload, headers=None, timeout=8):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **HEADERS, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode())


def fetch_candles(granularity, limit=5):
    url = f"{BITGET}/candles?symbol={SYMBOL}&productType={PRODUCT}&granularity={granularity}&limit={limit}"
    data = http_get(url)
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget candles error: {data.get('msg')}")
    return [
        {"ts": int(d[0]), "open": float(d[1]), "high": float(d[2]),
         "low": float(d[3]), "close": float(d[4]), "vol": float(d[5])}
        for d in data["data"]
    ]


def fetch_ticker():
    url = f"{BITGET}/ticker?symbol={SYMBOL}&productType={PRODUCT}"
    data = http_get(url)
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget ticker error: {data.get('msg')}")
    d = data["data"][0]
    return {"last": float(d["lastPr"]), "mark": float(d["markPrice"])}


def candle_color(c):
    return "bull" if c["close"] >= c["open"] else "bear"


def compute_rsi(closes, period=RSI_PERIOD):
    """Cutler's RSI on a list of closes (oldest -> newest). Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    window = deltas[-period:]
    gains = sum(d for d in window if d > 0) / period
    losses = sum(-d for d in window if d < 0) / period
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def compute_ema(closes, period):
    """Standard EMA over a list of closes (oldest -> newest)."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def compute_atr(candles, period=ATR_PERIOD):
    """Simple ATR on CLOSED candles (oldest -> newest). Needs period+1 candles."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    return sum(trs[-period:]) / period


def finalize_close(state, exit_price, reason, now, su):
    """Close the open position at exit_price. Mutates su with the full close update.
    Returns (trade, new_balance, streak_count, closed_side)."""
    is_long = state["side"] == "long"
    notional = state["notional"]
    margin = state["margin"]
    entry = state["entry_price"]
    diff = (exit_price - entry) if is_long else (entry - exit_price)
    gross = diff * (notional / entry)
    fees = notional * FEE_RATE * 2
    net = gross - fees
    new_balance = state["balance"] + net  # margin is virtual sizing only, never reserved from balance

    trade = {
        "side": state["side"],
        "entry_price": entry,
        "exit_price": exit_price,
        "notional": notional,
        "margin": margin,
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "balance_after": round(new_balance, 6),
        "opened_at": state.get("opened_at"),
        "closed_at": now,
    }

    total_trades = (state.get("total_trades") or 0) + 1
    wins = (state.get("wins") or 0) + (1 if net > 0 else 0)
    losses = (state.get("losses") or 0) + (1 if net <= 0 else 0)

    # Anti-whipsaw streak tracking: count consecutive SLs on the SAME side.
    # A TP (or neutral time-stop exit) resets the streak.
    closed_side = state["side"]
    if reason == "SL":
        if state.get("streak_side") == closed_side:
            streak_count = (state.get("streak_count") or 0) + 1
        else:
            streak_count = 1
        streak_side = closed_side
    else:
        streak_count = 0
        streak_side = "none"

    su.update({
        "position_open": False, "side": "none", "entry_price": 0,
        "tp_price": 0, "sl_price": 0, "notional": 0, "margin": 0,
        "opened_at": None, "balance": round(new_balance, 6),
        "total_trades": total_trades, "wins": wins, "losses": losses,
        "last_error": "", "streak_side": streak_side, "streak_count": streak_count,
    })
    return trade, new_balance, streak_count, closed_side


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "dummy":
        return
    try:
        http_post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )
        log(f"Telegram sent: {text[:60]}...")
    except Exception as e:
        log(f"Telegram send failed: {e}")


def get_state():
    data = http_get(BASE44_DASHBOARD) or {}
    state = data.get("state") or {}
    # Attach the latest CLOSED trade (used for the post-SL cooldown).
    trades = data.get("trades") or []
    if trades:
        state["_last_close"] = trades[0].get("closed_at")
        state["_last_reason"] = trades[0].get("reason")
    # Day-start balance (UTC) for the daily loss circuit breaker: balance after
    # the last trade that closed before today's midnight.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = None
    for t in trades:
        if (t.get("closed_at") or "").startswith(today):
            continue
        day_start = t.get("balance_after")
        break
    if day_start is None:
        day_start = state.get("balance") or START_BALANCE
    state["_day_start_balance"] = day_start
    return state


def sync(state_update=None, trade=None):
    payload = {"state": state_update or {}, "trade": trade}
    data = http_post(BASE44_SYNC, payload, headers={"X-Tick-Secret": TICK_SECRET})
    if not data.get("ok"):
        raise RuntimeError(f"Sync failed: {data.get('error')}")
    return data.get("state") or {}


def process_tick(state):
    """One poll cycle. Returns (action, details)."""
    c4h = fetch_candles("4H", 2)
    c15m = fetch_candles("15m", 14)  # closed candles for zone + sweep structure detection
    c1m = fetch_candles("1m", 24)  # 23 closed candles: EMA21 needs 21
    ticker = fetch_ticker()

    forming_4h = c4h[-1]
    forming_15m = c15m[-1]
    forming_1m = c1m[-1]
    prev_1m = c1m[-2] if len(c1m) > 2 else forming_1m

    color_4h = candle_color(forming_4h)
    color_15m = candle_color(forming_15m)
    color_1m = candle_color(forming_1m)
    prev_color_1m = candle_color(prev_1m)
    price = ticker["last"]
    bias = color_4h if color_4h == color_15m else "none"

    now = datetime.now(timezone.utc).isoformat()
    su = {
        "last_1m_color": color_1m,
        "last_15m_color": color_15m,
        "last_4h_color": color_4h,
        "last_bias": bias,
        "last_price": price,
        "last_tick_at": now,
    }

    # Position open -> TP ONLY (owner 09-06: stop loss removed 100%).
    # The position runs until TP hits — no SL, no trail, no time-stop.
    if state.get("position_open"):
        is_long = state.get("side") == "long"
        tp = state.get("tp_price") or 0
        if tp > 0:
            hit_tp = price >= tp if is_long else price <= tp
            if not hit_tp:
                # gap-aware scan: any candle since entry touching TP counts
                scan = fetch_candles("1m", 15)
                opened_ms = None
                if state.get("opened_at"):
                    try:
                        opened_ms = int(datetime.fromisoformat(state["opened_at"]).timestamp() * 1000)
                    except (ValueError, TypeError):
                        opened_ms = None
                for candle in scan:
                    if opened_ms is not None and candle["ts"] < opened_ms:
                        continue
                    if is_long and candle["high"] >= tp:
                        hit_tp = True
                        break
                    if not is_long and candle["low"] <= tp:
                        hit_tp = True
                        break
            if hit_tp:
                trade, new_balance, _sc, _cs = finalize_close(state, tp, "TP", now, su)
                sync(state_update=su, trade=trade)
                pnl_str = f"+${trade['net_pnl']:.4f}" if trade['net_pnl'] >= 0 else f"-${abs(trade['net_pnl']):.4f}"
                held = ""
                try:
                    held = f" after {(datetime.now(timezone.utc)-datetime.fromisoformat(state['opened_at'])).total_seconds()/60:.0f}m"
                except Exception:
                    pass
                send_telegram(
                    f"✅ *Closed* {state['side'].upper()} (TP){held}\n"
                    f"Entry ${state['entry_price']:.4f} → TP ${tp:.4f}\n"
                    f"PnL {pnl_str} | Balance ${new_balance:.4f}"
                )
                return "closed", f"{state['side']} TP @ ${tp:.4f} PnL {pnl_str}"
        # No hit — update market fields only; the position runs until TP
        sync(state_update=su)
        return "none", f"position open ({state['side']}) price=${price:.4f}"

    # Flat -> check entry. SCALPER MODE (owner 09-06): chop moves as they go.
    # Up-move -> buy the pullback candles; down-move -> sell the rally candles.
    if state.get("status") == "running":
        # circuit breakers stay (harmless in TP-only mode, but kept for safety)
        bal = state.get("balance") or START_BALANCE
        dsb = state.get("_day_start_balance") or bal
        if dsb > 0 and bal < dsb * (1 - DAILY_LOSS_LIMIT):
            sync(state_update=su)
            return "none", f"RISK-OFF: daily loss limit ({(bal/dsb-1)*100:.1f}% today)"
        if (state.get("streak_count") or 0) >= MAX_STREAK and state.get("streak_side") in ("long", "short"):
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["_last_close"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                elapsed = STREAK_PAUSE_SEC
            if elapsed < STREAK_PAUSE_SEC:
                sync(state_update=su)
                return "none", f"RISK-OFF: SL streak pause ({int(STREAK_PAUSE_SEC-elapsed)}s left)"

        atr = compute_atr(c1m[:-1])
        if atr is None:
            sync(state_update=su)
            return "none", "not enough data"
        # dead-market gate stays (fees kill dead-minute scalps)
        hour = datetime.now(timezone.utc).hour
        atr_gate = NIGHT_ATR_MIN if (hour < 7 or hour >= 21) else ATR_MIN
        if atr < atr_gate:
            sync(state_update=su)
            return "none", f"thin market (atr {atr:.4f} < gate {atr_gate:.4f})"

        # scalper momentum: EMA9 vs EMA21 on CLOSED 1m closes
        closes = [c["close"] for c in c1m[:-1]]
        ema_fast = compute_ema(closes, EMA_FAST)
        ema_slow = compute_ema(closes, EMA_SLOW)
        if ema_fast is None or ema_slow is None:
            sync(state_update=su)
            return "none", "not enough data for EMA"
        momentum = "bull" if ema_fast > ema_slow else "bear"
        su["last_bias"] = momentum

        # pullback chopper: in bull momentum, a RED closed candle above the slow
        # EMA is a discount — buy it. In bear momentum, a GREEN candle below the
        # slow EMA is a premium — sell it. One rule, both directions, every move.
        last_closed = c1m[-2] if len(c1m) >= 2 else c1m[-1]
        last_color = candle_color(last_closed)
        want = None
        ema_fast_prev = compute_ema(closes[:-3], EMA_FAST)
        if momentum == "bull" and last_color == "bear" and price > ema_slow and (ema_fast_prev is None or ema_fast > ema_fast_prev):
            want = "long"   # uptrend pullback — buy the dip candle
        elif momentum == "bear" and last_color == "bull" and price < ema_slow and (ema_fast_prev is None or ema_fast < ema_fast_prev):
            want = "short"  # downtrend rally — sell the rip candle
        if want is None:
            sync(state_update=su)
            return "none", f"flat (momentum {momentum}) — waiting for pullback candle"

        # TP distance adaptive to volatility; notional solved so a full TP nets
        # $0.15 (owner's flat target). NO STOP LOSS.
        tp_dist = max(SCALP_TP_ATR * atr, MIN_TP_DIST)
        per_unit = tp_dist / price - FEE_RATE * 2
        if per_unit <= 0:
            sync(state_update=su)
            return "none", "tp too small to clear fees"
        notional = min(WIN_TARGET_DOLLARS / per_unit, bal * LEVERAGE * 0.95, 40.0)
        margin = notional / LEVERAGE
        if margin > bal:
            sync(state_update=su)
            return "none", "insufficient balance for margin"
        entry = price
        tp = entry + tp_dist if want == "long" else entry - tp_dist
        su.update({
            "position_open": True, "side": want,
            "entry_price": entry, "tp_price": tp, "sl_price": 0,
            "notional": notional, "margin": margin, "opened_at": now,
            "last_error": "",
        })
        sync(state_update=su)
        send_telegram(
            f"⚡️ *Opened {want.upper()} (scalp)*\n"
            f"Momentum {momentum} | Entry ${entry:.4f}\n"
            f"TP ${tp:.4f} | NO SL\n"
            f"Notional ${notional:.2f} | Balance ${bal:.4f}"
        )
        return "opened", f"{want.upper()} SCALP @ ${entry:.4f} momentum={momentum}"

    # No action
    sync(state_update=su)
    return "none", f"flat bias={bias} 1m={color_1m}"


def main():
    start = time.time()
    ticks = 0
    trades = 0
    log("NEAR Scalper tick run starting")

    # Reset any stale error at run start
    state = get_state()
    log(f"Initial state: balance=${state.get('balance', 0):.4f} position_open={state.get('position_open')}")

    while time.time() - start < MAX_RUNTIME:
        try:
            state = get_state()
            action, details = process_tick(state)
            ticks += 1
            log(f"tick#{ticks}: {action} — {details}")
            if action in ("opened", "closed"):
                trades += 1
        except Exception as e:
            log(f"ERROR: {e}")
            try:
                sync(state_update={"last_error": str(e)[:200]})
            except Exception as e2:
                log(f"ERROR updating state: {e2}")

        elapsed = time.time() - start
        if elapsed < MAX_RUNTIME:
            time.sleep(max(1, POLL_INTERVAL - (time.time() - start - elapsed) % POLL_INTERVAL))

    log(f"Run complete: {ticks} ticks, {trades} trades, {time.time()-start:.0f}s")


if __name__ == "__main__":
    main()
