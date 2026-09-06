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
WIN_TARGET_PCT = 0.04 # a full TP should net ~4% of balance: $0.10 today, ~$0.20 by $5
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
    c1m = fetch_candles("1m", 20)
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

    # Position open -> check TP/SL
    if state.get("position_open"):
        is_long = state.get("side") == "long"

        # Gap-aware: scan 1m candle highs/lows for TP/SL touches, not just current price.
        # Catches hits that occur between polls or during schedule gaps.
        hit_tp = price >= state["tp_price"] if is_long else price <= state["tp_price"]
        hit_sl = price <= state["sl_price"] if is_long else price >= state["sl_price"]
        if not hit_tp and not hit_sl:
            scan = fetch_candles("1m", 15)
            # Only candles that formed AFTER entry count as our risk. Pre-entry
            # candles routinely bracket the SL level on pullback entries —
            # scanning them killed positions in <30s (2026-09-06 postmortem:
            # avg hold 24s, 11 instant SLs). The live price check covers the
            # entry candle; this scan covers between-poll gaps only.
            opened_ms = None
            if state.get("opened_at"):
                try:
                    opened_ms = int(datetime.fromisoformat(state["opened_at"]).timestamp() * 1000)
                except (ValueError, TypeError):
                    opened_ms = None
            for candle in scan:
                if opened_ms is not None and candle["ts"] < opened_ms:
                    continue  # candle closed before we even entered — history, not risk
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
            reason = "TP" if hit_tp else "SL"
            trade, new_balance, streak_count, closed_side = finalize_close(state, exit_price, reason, now, su)
            sync(state_update=su, trade=trade)
            pnl_str = f"+${trade['net_pnl']:.4f}" if trade["net_pnl"] >= 0 else f"-${abs(trade['net_pnl']):.4f}"
            streak_note = ""
            if reason == "SL" and streak_count >= SL_STREAK_REVERSAL:
                streak_note = f"\n⚠️ {streak_count}x consecutive {closed_side.upper()} SL — next signal will auto-reverse"
            send_telegram(
                f"🔄 *Closed* {state['side'].upper()} ({reason})\n"
                f"Entry ${state['entry_price']:.4f} → Exit ${exit_price:.4f}\n"
                f"PnL {pnl_str} | Balance ${new_balance:.4f}"
                f"{streak_note}"
            )
            return "closed", f"{state['side']} {reason} @ ${exit_price:.4f} PnL {pnl_str}"

        # Trailing stop: once the move is TRAIL_TRIGGER of the way to TP, the
        # stop ratchets behind price — breakeven first, then a locked profit.
        # It only tightens, never loosens.
        entry = state["entry_price"]
        tp_d = abs(state["tp_price"] - entry)
        if tp_d > 0:
            if is_long:
                if price - entry >= TRAIL_TRIGGER * tp_d:
                    new_sl = max(state["sl_price"], price - TRAIL_DIST * tp_d)
                    if new_sl > state["sl_price"] + 1e-9:
                        su["sl_price"] = new_sl
            else:
                if entry - price >= TRAIL_TRIGGER * tp_d:
                    new_sl = min(state["sl_price"], price + TRAIL_DIST * tp_d)
                    if new_sl < state["sl_price"] - 1e-9:
                        su["sl_price"] = new_sl

        # Time stop: a scalp that goes nowhere for TIME_STOP_MIN minutes gets
        # recycled at market — dead capital is a loss of opportunity, not a position.
        opened = state.get("opened_at")
        if opened:
            try:
                held_min = (datetime.now(timezone.utc) - datetime.fromisoformat(opened)).total_seconds() / 60
            except (ValueError, TypeError):
                held_min = 0
            if held_min >= TIME_STOP_MIN:
                trade, new_balance, _sc, _cs = finalize_close(state, price, "TIME", now, su)
                sync(state_update=su, trade=trade)
                pnl_str = f"+${trade['net_pnl']:.4f}" if trade["net_pnl"] >= 0 else f"-${abs(trade['net_pnl']):.4f}"
                send_telegram(
                    f"⏱️ *Time-Stop* {state['side'].upper()} recycled after {int(held_min)}m\n"
                    f"PnL {pnl_str} | Balance ${new_balance:.4f}"
                )
                return "closed", f"{state['side']} TIME @ ${price:.4f} PnL {pnl_str}"

        # No hit — update market fields only (plus any tightened trail stop)
        sync(state_update=su)
        return "none", f"position open ({state['side']}) price=${price:.4f}"

    # Flat -> check entry
    if state.get("status") == "running":
        # ── circuit breakers: never let chop bleed the account to zero ──
        bal = state.get("balance") or START_BALANCE
        dsb = state.get("_day_start_balance") or bal
        if dsb > 0 and bal < dsb * (1 - DAILY_LOSS_LIMIT):
            sync(state_update=su)
            return "none", f"RISK-OFF: daily loss limit ({(bal/dsb-1)*100:.1f}% today) — done until tomorrow"
        if (state.get("streak_count") or 0) >= MAX_STREAK and state.get("streak_side") in ("long", "short"):
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["_last_close"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                elapsed = 0  # unknown timing — pause anyway, the streak is the signal
            if elapsed < STREAK_PAUSE_SEC:
                sync(state_update=su)
                return "none", f"RISK-OFF: {state.get('streak_count')}x {state.get('streak_side')} SL streak — {int(STREAK_PAUSE_SEC-elapsed)}s pause left"

        # ── regime detection: is the market consolidating? ──
        zone_high = max(c["high"] for c in c15m[:-1])
        zone_low = min(c["low"] for c in c15m[:-1])
        zone_range = zone_high - zone_low
        consolidating = zone_range <= CONSOL_MAX_RANGE * price

        if consolidating:
            # ── RANGE MODE: trade like a range trader. Buy the floor, sell the
            # ceiling, size to the room left in the zone, and get out BEFORE the
            # wall. The middle of the zone is a chop trap — no entries there.
            pos_in_zone = (price - zone_low) / zone_range if zone_range > 0 else 0.5
            atr = compute_atr(c1m[:-1])
            zone_rsi = compute_rsi([c["close"] for c in c1m[:-1]])
            want = None
            if pos_in_zone <= ZONE_ENTRY_POS and (zone_rsi is None or zone_rsi < 52):
                want = "long"   # at the floor — buy the discount
            elif pos_in_zone >= 1 - ZONE_ENTRY_POS and (zone_rsi is None or zone_rsi > 48):
                want = "short"  # at the ceiling — sell the premium
            if want and state.get("_last_reason") == "SL" and state.get("_last_close"):
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["_last_close"])).total_seconds()
                    if elapsed < SL_COOLDOWN_SECONDS:
                        want = None
                except (ValueError, TypeError):
                    pass
            if want is None:
                sync(state_update=su)
                return "none", f"consolidating zone {zone_low:.4f}-{zone_high:.4f} — waiting at edge (pos {pos_in_zone:.2f})"
            is_long = want == "long"
            buffer = max(0.6 * (atr or 0.003), 0.004)
            if is_long:
                tp = zone_high - ZONE_TP_HAIRCUT * zone_range
                sl = zone_low - buffer
            else:
                tp = zone_low + ZONE_TP_HAIRCUT * zone_range
                sl = zone_high + buffer
            tp_dist = abs(tp - price)
            sl_dist = abs(price - sl)
            if tp_dist < 1.2 * sl_dist or tp_dist < MIN_SL_DIST:
                sync(state_update=su)
                return "none", f"zone entry skipped — poor R:R (tp {tp_dist:.4f} vs sl {sl_dist:.4f})"
            balance = state.get("balance") or START_BALANCE
            win_target = balance * WIN_TARGET_PCT
            per_unit = tp_dist / price - FEE_RATE * 2
            if per_unit <= 0:
                sync(state_update=su)
                return "none", f"zone TP too small to clear fees (tp_dist={tp_dist:.4f})"
            notional = min(win_target / per_unit, balance * LEVERAGE * 0.95, 40.0)
            margin = notional / LEVERAGE
            if margin > balance:
                sync(state_update=su)
                return "none", "insufficient balance for margin"
            entry = price
            su.update({
                "position_open": True, "side": want,
                "entry_price": entry, "tp_price": tp, "sl_price": sl,
                "notional": notional, "margin": margin, "opened_at": now,
                "last_error": "",
            })
            sync(state_update=su)
            send_telegram(
                f"🟦 *Opened {want.upper()} (zone fade)*\n"
                f"Zone ${zone_low:.4f} - ${zone_high:.4f}\n"
                f"Entry ${entry:.4f}\n"
                f"TP ${tp:.4f} | SL ${sl:.4f}\n"
                f"Notional ${notional:.2f} | Balance ${balance:.4f}"
            )
            return "opened", f"{want.upper()} ZONE-FADE @ ${entry:.4f} (zone pos {pos_in_zone:.2f})"

        elif bias != "none":
            # RSI pullback trigger: buy weakness in uptrends, sell strength in
            # downtrends. The old 1m color-flip trigger chased breakouts and bought
            # local tops (15.9% win rate over 208 trades) — this is the fix for that.
            # Uses CLOSED candles only; the forming candle is excluded.
            closed_closes = [c["close"] for c in c1m[:-1]]
            cur_rsi = compute_rsi(closed_closes)
            prev_rsi = compute_rsi(closed_closes[:-1]) if len(closed_closes) > RSI_PERIOD + 1 else None

            just_turned = False
            if cur_rsi is not None and prev_rsi is not None:
                if bias == "bull":
                    # cross: dip just ended — buy the discount
                    if prev_rsi < RSI_LONG_ENTRY <= cur_rsi:
                        just_turned = True
                    # early: RSI still low but swinging up fast — catch the turn
                    # before the cross completes (more chances, same direction logic)
                    elif cur_rsi < RSI_LONG_ENTRY and cur_rsi - prev_rsi >= RSI_EARLY_VELOCITY:
                        just_turned = True
                elif bias == "bear":
                    if prev_rsi > RSI_SHORT_ENTRY >= cur_rsi:
                        just_turned = True
                    elif cur_rsi > RSI_SHORT_ENTRY and prev_rsi - cur_rsi >= RSI_EARLY_VELOCITY:
                        just_turned = True

            # ── liquidity-sweep trigger (the pro lesson: obvious levels get hunted
            # BEFORE they prove out). Real sweeps take MINUTES against a multi-hour
            # swing level: some candle in the recent window pierces the 3h swing
            # extreme (where stops cluster), then the latest closed candle rejects
            # and closes back inside. The trapped orders are the fuel. Trade WITH
            # the hunt, not as it.
            sweep_long = False
            sweep_short = False
            sweep_wick = None
            closed_15m = c15m[:-1]
            # swing structure EXCLUDES the most recent closed 15m candle — the
            # sweep must pierce a level that was already resting BEFORE the last
            # quarter-hour, otherwise the level is self-referential (the recent
            # candle IS the low being tested).
            structure = closed_15m[-(SWEEP_15M_LOOKBACK + 1):-1]
            if SWEEP_ENABLED and len(structure) >= SWEEP_15M_LOOKBACK:
                swing_low = min(c["low"] for c in structure)
                swing_high = max(c["high"] for c in structure)
                window = c1m[-(SWEEP_WINDOW + 1):-1]  # last N CLOSED 1m candles
                last_c = c1m[-2] if len(c1m) >= 2 else c1m[-1]
                swept_low = any(c["low"] < swing_low for c in window)
                swept_high = any(c["high"] > swing_high for c in window)
                rearm_ok = True
                if state.get("_last_close"):
                    try:
                        since = (datetime.now(timezone.utc) - datetime.fromisoformat(state["_last_close"])).total_seconds()
                        if since < SWEEP_REARM_SEC:
                            rearm_ok = False
                    except (ValueError, TypeError):
                        pass
                if rearm_ok and bias == "bull" and swept_low and last_c["close"] > swing_low and price > swing_low:
                    sweep_long = True
                    sweep_wick = min(c["low"] for c in window)
                elif rearm_ok and bias == "bear" and swept_high and last_c["close"] < swing_high and price < swing_high:
                    sweep_short = True
                    sweep_wick = max(c["high"] for c in window)

            # SL cooldown: after a stop-out, wait before re-entering. The 2026-09-05
            # postmortem showed rapid-fire re-entries losing 9x in a row in chop.
            if (just_turned or sweep_long or sweep_short) and state.get("_last_reason") == "SL" and state.get("_last_close"):
                try:
                    last_sl = datetime.fromisoformat(state["_last_close"])
                    elapsed = (datetime.now(timezone.utc) - last_sl).total_seconds()
                    if elapsed < SL_COOLDOWN_SECONDS:
                        sync(state_update=su)
                        return "none", f"SL cooldown {int(elapsed)}/{SL_COOLDOWN_SECONDS}s"
                except (ValueError, TypeError):
                    pass  # unparsable timestamp — skip the cooldown check

            if just_turned or sweep_long or sweep_short:
                balance = state.get("balance") or START_BALANCE
                if balance < 0.30:
                    sync(state_update=su)
                    return "none", "balance too low to trade safely"

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

                # Volatility-scaled targets: wide when the market actually moves,
                # tight when it's dozing. Never trade a dead market.
                atr = compute_atr(c1m[:-1])
                hour = datetime.now(timezone.utc).hour
                night = hour >= 21 or hour < 7
                atr_gate = NIGHT_ATR_MIN if night else ATR_MIN
                if atr is None or atr < atr_gate:
                    sync(state_update=su)
                    return "none", f"entry skipped - thin market (atr={atr:.4f} < gate {atr_gate:.4f}{' night' if night else ''})"
                sl_dist = max(SL_ATR_MULT * atr, MIN_SL_DIST)
                # sweep entries: stop sits under the hunt wick (structural
                # invalidation) — a second pierce means the reversal failed.
                if sweep_long and sweep_wick is not None:
                    sl_dist = max(price - (sweep_wick - max(0.3 * atr, 0.002)), 0.0015)
                elif sweep_short and sweep_wick is not None:
                    sl_dist = max((sweep_wick + max(0.3 * atr, 0.002)) - price, 0.0015)
                tp_dist = TP_SL_RATIO * sl_dist

                # Target-based sizing (owner directive 09-06): a full TP should net
                # WIN_TARGET_PCT of balance — $0.10 at $2.65, ~$0.20 at $5. Solve the
                # notional backwards from the actual TP distance so the target holds
                # at any volatility. Capped by margin (10x isolated) and an absolute
                # ceiling.
                win_target = balance * WIN_TARGET_PCT
                per_unit = tp_dist / price - FEE_RATE * 2  # net fraction of notional per full TP
                if per_unit <= 0:
                    sync(state_update=su)
                    return "none", f"TP too small to clear fees (tp_dist={tp_dist:.4f})"
                notional = min(win_target / per_unit, balance * LEVERAGE * 0.95, 40.0)
                margin = notional / LEVERAGE
                if margin > balance:
                    sync(state_update=su)
                    return "none", "insufficient balance for margin"
                entry = price
                tp = entry + tp_dist if is_long else entry - tp_dist
                sl = entry - sl_dist if is_long else entry + sl_dist

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
