#!/usr/bin/env python3
"""
NEAR Scalper Bot — Tick Engine (runs inside GitHub Actions).

Paper trading only. Virtual $3 balance. No real orders.
Deterministic rules only — no LLM in the hot path.

Flow (every 15s for ~4.4 min):
  1. GET current state from Base44 (dashboard endpoint)
  2. Fetch forming 4H/15M/1M candles + ticker from Bitget
  3. Bias = 4H color == 15M color
  4. If position open -> check TP ($0.02) / SL ($0.015)
  5. If flat + bias + 1M just turned to bias color -> open
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
TP_DOLLAR = 0.02
SL_DOLLAR = 0.015
SL_STREAK_REVERSAL = 3  # after N consecutive SLs on one side, flip the next entry
SL_COOLDOWN_SECONDS = 180  # after a stop-out, wait this long before re-entering (chop protection)
MIN_BODY_RATIO = 0.35  # turning candle body must be >= 35% of its range (filters doji noise)
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
    c15m = fetch_candles("15m", 2)
    c1m = fetch_candles("1m", 4)
    ticker = fetch_ticker()

    forming_4h = c4h[-1]
    forming_15m = c15m[-1]
    forming_1m = c1m[-1]
    prev_1m = c1m[-2] if len(c1m) > 2 else forming_1m

    color_4h = candle_color(forming_4h)
    color_15m = candle_color(forming_15m)
    color_1m = candle_color(forming_1m)
    prev_color_1m = candle_color(prev_1m)
    # Closed-candle confirmation data: the forming candle can flicker color
    # mid-minute, so entries must be confirmed on candles that already closed.
    last_closed_1m = c1m[-2]
    prior_closed_1m = c1m[-3] if len(c1m) > 3 else last_closed_1m
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

    # Position open -> check TP/SL
    if state.get("position_open"):
        is_long = state.get("side") == "long"

        # Gap-aware: scan 1m candle highs/lows for TP/SL touches, not just current price.
        # Catches hits that occur between polls or during schedule gaps.
        hit_tp = price >= state["tp_price"] if is_long else price <= state["tp_price"]
        hit_sl = price <= state["sl_price"] if is_long else price >= state["sl_price"]
        if not hit_tp and not hit_sl:
            scan = fetch_candles("1m", 15)
            for candle in scan:
                if is_long:
                    c_sl = candle["low"] <= state["sl_price"]
                    c_tp = candle["high"] >= state["tp_price"]
                else:
                    c_sl = candle["high"] >= state["sl_price"]
                    c_tp = candle["low"] <= state["tp_price"]
                if c_sl:  # conservative: assume SL first within a candle
                    hit_sl = True
                    break
                if c_tp:
                    hit_tp = True
                    break

        if hit_tp or hit_sl:
            exit_price = state["tp_price"] if hit_tp else state["sl_price"]
            notional = state["notional"]
            margin = state["margin"]
            entry = state["entry_price"]
            diff = (exit_price - entry) if is_long else (entry - exit_price)
            gross = diff * (notional / entry)
            fees = notional * FEE_RATE * 2
            net = gross - fees
            new_balance = state["balance"] + net  # margin is virtual sizing only, never reserved from balance
            reason = "TP" if hit_tp else "SL"

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
            # A TP (a real win) or a switch to the other side resets the streak.
            closed_side = state["side"]
            if reason == "SL":
                if state.get("streak_side") == closed_side:
                    streak_count = (state.get("streak_count") or 0) + 1
                else:
                    streak_count = 1
                streak_side = closed_side
            else:  # TP — the market rewarded this direction, streak is over
                streak_count = 0
                streak_side = "none"

            su.update({
                "position_open": False, "side": "none", "entry_price": 0,
                "tp_price": 0, "sl_price": 0, "notional": 0, "margin": 0,
                "opened_at": None, "balance": round(new_balance, 6),
                "total_trades": total_trades, "wins": wins, "losses": losses,
                "last_error": "", "streak_side": streak_side, "streak_count": streak_count,
            })

            fresh = sync(state_update=su, trade=trade)
            pnl_str = f"+${net:.4f}" if net >= 0 else f"-${abs(net):.4f}"
            streak_note = ""
            if reason == "SL" and streak_count >= SL_STREAK_REVERSAL:
                streak_note = f"\n⚠️ {streak_count}x consecutive {closed_side.upper()} SL — next signal will auto-reverse"
            send_telegram(
                f"🔄 *Closed* {state['side'].upper()} ({reason})\n"
                f"Entry ${entry:.4f} → Exit ${exit_price:.4f}\n"
                f"PnL {pnl_str} | Balance ${new_balance:.4f}"
                f"{streak_note}"
            )
            return "closed", f"{state['side']} {reason} @ ${exit_price:.4f} PnL {pnl_str}"

        # No hit — update market fields only
        sync(state_update=su)
        return "none", f"position open ({state['side']}) price=${price:.4f}"

    # Flat -> check entry
    if bias != "none" and state.get("status") == "running":
        # Entry signal confirmed on CLOSED 1m candles only — no mid-candle flicker entries.
        just_turned = (candle_color(prior_closed_1m) != candle_color(last_closed_1m)
                       and candle_color(last_closed_1m) == bias)

        # Body-strength filter: the turning candle must have a real directional body,
        # not a doji — filters noise flips in chop.
        body = abs(last_closed_1m["close"] - last_closed_1m["open"])
        rng = last_closed_1m["high"] - last_closed_1m["low"]
        if rng > 0 and body < MIN_BODY_RATIO * rng:
            just_turned = False

        # SL cooldown: after a stop-out, wait before re-entering. The 2026-09-05
        # postmortem showed rapid-fire re-entries losing 9x in a row in chop.
        if just_turned and state.get("_last_reason") == "SL" and state.get("_last_close"):
            try:
                last_sl = datetime.fromisoformat(state["_last_close"])
                elapsed = (datetime.now(timezone.utc) - last_sl).total_seconds()
                if elapsed < SL_COOLDOWN_SECONDS:
                    sync(state_update=su)
                    return "none", f"SL cooldown {int(elapsed)}/{SL_COOLDOWN_SECONDS}s"
            except (ValueError, TypeError):
                pass  # unparsable timestamp — skip the cooldown check

        if just_turned:
            balance = state.get("balance") or START_BALANCE
            notional = min(balance * LEVERAGE * 0.8, 25)
            margin = notional / LEVERAGE
            if margin > balance:
                sync(state_update=su)
                return "none", "insufficient balance for margin"

            natural_is_long = bias == "bull"
            natural_side = "long" if natural_is_long else "short"

            # Anti-whipsaw reversal: the technical signal (bias) has been wrong
            # SL_STREAK_REVERSAL times in a row on this exact side. Instead of
            # trusting it again and eating a 4th stop-out, take the opposite side.
            reversed_entry = (
                state.get("streak_side") == natural_side
                and (state.get("streak_count") or 0) >= SL_STREAK_REVERSAL
            )
            is_long = (not natural_is_long) if reversed_entry else natural_is_long

            entry = price
            tp = entry + TP_DOLLAR if is_long else entry - TP_DOLLAR
            sl = entry - SL_DOLLAR if is_long else entry + SL_DOLLAR

            su.update({
                "position_open": True, "side": "long" if is_long else "short",
                "entry_price": entry, "tp_price": tp, "sl_price": sl,
                "notional": notional, "margin": margin, "opened_at": now,
                "last_error": "",
            })
            if reversed_entry:
                # Fresh start post-flip — don't let the old streak carry over.
                su.update({"streak_side": "none", "streak_count": 0, "last_reversal_at": now})
            sync(state_update=su)

            if reversed_entry:
                send_telegram(
                    f"🔁 *Auto-Reversal* — {natural_side.upper()} signal ignored after "
                    f"{SL_STREAK_REVERSAL}x consecutive SL\n"
                    f"🟢 *Opened {'LONG' if is_long else 'SHORT'}* (reversed)\n"
                    f"Entry ${entry:.4f}\n"
                    f"TP ${tp:.4f} | SL ${sl:.4f}\n"
                    f"Notional ${notional:.2f} | Balance ${balance:.4f}"
                )
            else:
                send_telegram(
                    f"🟢 *Opened {'LONG' if is_long else 'SHORT'}*\n"
                    f"Entry ${entry:.4f}\n"
                    f"TP ${tp:.4f} | SL ${sl:.4f}\n"
                    f"Notional ${notional:.2f} | Balance ${balance:.4f}"
                )
            return "opened", f"{'LONG' if is_long else 'SHORT'} @ ${entry:.4f}" + (" (reversed)" if reversed_entry else "")

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
