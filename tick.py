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
WIN_TARGET_DOLLARS = 0.15  # OWNER 09-06: each TP aims for $0.15 (margin caps scale it down)
SCALP_TP_ATR = 1.2   # TP distance = 1.2x 1m ATR (adaptive to live volatility)
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "4"))  # owner 09-06: account too small to split 8 ways —
# lab confirmed 4 slots strictly better: realized +3.84 vs +3.58, equity +0.51 vs -0.30, half the wedges    # hedge scalper: multiple concurrent positions — wedged trades don't stop the chopping
MARGIN_BUDGET = float(os.environ.get("MARGIN_BUDGET", "0.40"))  # owner 09-07: halved.
# Lab (same fresh week, 4 slots): 0.80 → realized +63% but equity -$2.94 (wedge
# cluster ate the grind). 0.40 → realized +35% and equity +$0.19 — the WORST
# observed week still ends green. Halves wins, halves wedge damage.  # total margin across all open positions <= 80% of balance
ENTRY_COOLDOWN_SEC = 90  # min seconds between entries — one signal cluster can't fill all slots
HEARTBEAT_SEC = 3600  # if no open/close events for an hour, ping Telegram so silence never looks like downtime

# ── REGIME HARDENING (owner 09-06: "build against this kind of regime") ──
# Failure mode observed live + in lab: one-way vertical moves wedge every
# counter-trend slot (longs at the local top, shorts at the local bottom).
# ENTRY THROTTLE — inventory cap (owner 09-06: "build against this kind of regime").
# NOT a stop loss: nothing ever closes, no loss is realized. It only stops ADDING
# new positions while floating damage exceeds this share of balance, so a one-way
# regime can't stack unlimited wedges. The grind resumes as floating recovers.
INVENTORY_CAP = float(os.environ.get("INVENTORY_CAP", "999"))  # OFF by default — lab 09-06: every throttle variant starved the grind that pays for wedges (raw equity -$0.26 vs -$3.2 to -$5.3 hardened)
# Slot recycling: a position stuck for hours relaxes its TP toward entry.
# It NEVER crosses entry — no loss is ever realized. It just stops demanding
# full profit to free the slot. Fees are always covered (keep > fee buffer).
TP_AGING = os.environ.get("TP_AGING", "1") == "1"
TP_AGING_TIERS = [(6, 0.5), (24, 0.25), (72, 0.10)]  # (hours stuck, fraction of TP distance kept)
MIN_TP_DIST = 0.003  # TP floor: ~0.13% — below this, fees eat the scalp alive
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


def http_get(url, timeout=8, retries=3):
    """GET with backoff on transient errors (429 rate-limit, 5xx). A single
    hiccup — common on shared GitHub-runner IPs hitting public exchange
    APIs — must not surface as an alert or cost a whole tick."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                if attempt < retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s
                    continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
    raise last_err


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


def aged_tp(pos, now):
    """TP after aging: a stuck position relaxes its target toward entry.
    Never crosses entry (no realized loss), always covers fees."""
    tp = pos["tp_price"]
    if not TP_AGING or not pos.get("opened_at"):
        return tp
    try:
        age_h = (datetime.fromisoformat(now) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
    except (ValueError, TypeError):
        return tp
    if age_h < TP_AGING_TIERS[0][0]:
        return tp
    dist = abs(tp - pos["entry_price"])
    if dist <= 0:
        return tp
    fee_buffer = pos["entry_price"] * (FEE_RATE * 2 + 0.0006)  # round-trip fees + crumb
    keep = dist
    for hours, frac in TP_AGING_TIERS:
        if age_h >= hours:
            keep = max(dist * frac, fee_buffer * 1.05)
    if pos["side"] == "long":
        return pos["entry_price"] + keep
    return pos["entry_price"] - keep


def close_position(pos, exit_price, reason, now, balance):
    """Close ONE position. Returns (trade_record, new_balance)."""
    is_long = pos["side"] == "long"
    notional = pos["notional"]
    entry = pos["entry_price"]
    diff = (exit_price - entry) if is_long else (entry - exit_price)
    gross = diff * (notional / entry)
    fees = notional * FEE_RATE * 2
    net = gross - fees
    new_balance = balance + net
    trade = {
        "side": pos["side"],
        "entry_price": entry,
        "exit_price": exit_price,
        "notional": notional,
        "margin": pos["margin"],
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "balance_after": round(new_balance, 6),
        "opened_at": pos["opened_at"],
        "closed_at": now,
    }
    return trade, new_balance


def serialize_positions(positions):
    """Store the positions list as compact JSON inside the legacy `last_error`
    string field — the ONLY writable field proven to round-trip the sync
    endpoint's schema whitelist at full length (689+ chars, exact match).
    SAD BUT NECESSARY: the sync endpoint silently drops any state field not
    in its original 2025 schema, so pos_*/equity/open_positions fields all
    vanished. If you change this format, probe the round-trip first."""
    compact = [
        {"s": p["side"], "e": p["entry_price"], "t": p["tp_price"],
         "n": p["notional"], "m": p["margin"], "o": p["opened_at"]}
        for p in positions[:MAX_POSITIONS]
    ]
    return {"last_error": json.dumps(compact, separators=(",", ":"))}


def parse_positions(state):
    """Rebuild the positions list from the JSON in `last_error`."""
    raw = state.get("last_error") or ""
    if not isinstance(raw, str) or not raw.startswith("["):
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for p in data[:MAX_POSITIONS]:
        try:
            if p.get("s") in ("long", "short"):
                out.append({
                    "side": p["s"],
                    "entry_price": float(p["e"]),
                    "tp_price": float(p["t"]),
                    "notional": float(p["n"]),
                    "margin": float(p["m"]),
                    "opened_at": p["o"],
                })
        except (KeyError, TypeError, ValueError):
            continue
    return out


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

    # ── HEDGE SCALPER POSITION MANAGEMENT: TP only, no SL, no time-stop.
    # Every open position runs until its own TP hits. Wedged positions simply
    # sit and consume margin budget; the bot keeps chopping with the rest.
    positions = parse_positions(state)
    bal_raw = state.get("balance")
    balance = bal_raw if isinstance(bal_raw, (int, float)) and bal_raw > 0 else START_BALANCE
    scan = fetch_candles("1m", 15)  # gap-aware TP scan (shared)
    closed_any = []
    still_open = []
    for pos in positions:
        tp = aged_tp(pos, now)
        if tp != (pos.get("tp_price") or 0):
            pos["tp_price"] = tp  # aged — persisted via serialize
        if tp <= 0:
            continue
        is_long = pos["side"] == "long"
        hit_tp = price >= tp if is_long else price <= tp
        if not hit_tp:
            opened_ms = None
            if pos.get("opened_at"):
                try:
                    opened_ms = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
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
            trade, balance = close_position(pos, tp, "TP", now, balance)
            closed_any.append(trade)
            send_telegram(
                f"\u2705 *Closed {pos['side'].upper()} (TP)*\n"
                f"Entry ${pos['entry_price']:.4f} → TP ${tp:.4f}\n"
                f"PnL +${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
        else:
            still_open.append(pos)

    # ── ENTRY: chop the CURRENT move. Pullback candle in live momentum = entry.
    if state.get("status") == "running":
        atr = compute_atr(c1m[:-1])
        hour = datetime.now(timezone.utc).hour
        atr_gate = NIGHT_ATR_MIN if (hour < 7 or hour >= 21) else ATR_MIN
        closes = [c["close"] for c in c1m[:-1]]
        ema_fast = compute_ema(closes, EMA_FAST)
        ema_slow = compute_ema(closes, EMA_SLOW)
        last_closed = c1m[-2] if len(c1m) >= 2 else c1m[-1]
        last_color = candle_color(last_closed)
        want = None
        regime_ok = True
        if atr is not None and ema_fast is not None and ema_slow is not None:
            if atr >= atr_gate:
                momentum = "bull" if ema_fast > ema_slow else "bear"
                if momentum == "bull" and last_color == "bear" and price > ema_slow:
                    want = "long"   # uptrend pullback — buy the dip candle
                elif momentum == "bear" and last_color == "bull" and price < ema_slow:
                    want = "short"  # downtrend rally — sell the rip candle
        if want:
            # inventory cap: how much are the open positions floating right now?
            floating_now = 0.0
            for p in still_open:
                u = (price - p["entry_price"]) * (p["notional"] / p["entry_price"]) if p["side"] == "long" \
                    else (p["entry_price"] - price) * (p["notional"] / p["entry_price"])
                floating_now += u
            if floating_now < -abs(INVENTORY_CAP) * balance:
                want = None  # wedge inventory too deep — pause new entries, let TPs work
        entry_cooldown_ok = True
        if still_open:
            try:
                last_open = max(datetime.fromisoformat(p["opened_at"]) for p in still_open if p.get("opened_at"))
                if (datetime.now(timezone.utc) - last_open).total_seconds() < ENTRY_COOLDOWN_SEC:
                    entry_cooldown_ok = False
            except (ValueError, TypeError):
                pass
        opened_this_tick = False
        can_open = (
            want is not None
            and entry_cooldown_ok
            and len(still_open) < MAX_POSITIONS
        )
        if can_open:
            used_margin = sum(p["margin"] for p in still_open)
            margin_left = balance * MARGIN_BUDGET - used_margin
            tp_dist = max(SCALP_TP_ATR * (atr or 0.003), MIN_TP_DIST)
            per_unit = tp_dist / price - FEE_RATE * 2
            # per-slot cap: 8 slots x balance notional = exactly the 80% margin
            # budget at 10x — a single trade can never hog the whole budget and
            # freeze the bot (the 2026-09-06 wedge lesson).
            slot_cap = balance * MARGIN_BUDGET * LEVERAGE / MAX_POSITIONS
            if per_unit > 0 and margin_left > 0.05:
                notional = min(WIN_TARGET_DOLLARS / per_unit, slot_cap, margin_left * LEVERAGE, 40.0)
                if notional >= 1.0:  # don't open dust positions
                    margin = notional / LEVERAGE
                    entry = price
                    tp = entry + tp_dist if want == "long" else entry - tp_dist
                    still_open.append({
                        "side": want, "entry_price": entry, "tp_price": tp,
                        "notional": notional, "margin": margin, "opened_at": now,
                    })
                    opened_this_tick = True
                    send_telegram(
                        f"\u26a1\ufe0f *Opened {want.upper()} (scalp)*\n"
                        f"Entry ${entry:.4f} → TP ${tp:.4f} | NO SL\n"
                        f"Notional ${notional:.2f} ({len(still_open)}/{MAX_POSITIONS} slots)"
                    )

    # ── sync state: positions flattened + compatibility fields for the dashboard
    unrealized = 0.0
    for p in still_open:
        u = (price - p["entry_price"]) * (p["notional"] / p["entry_price"]) if p["side"] == "long" \
            else (p["entry_price"] - price) * (p["notional"] / p["entry_price"])
        unrealized += u
    su.update({
        "position_open": len(still_open) > 0,
        "balance": round(balance, 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(balance + unrealized, 6),
        "open_positions": len(still_open),
        "total_trades": (state.get("total_trades") or 0) + len(closed_any),
        "wins": (state.get("wins") or 0) + len(closed_any),  # TP-only: every close is a win
        "losses": state.get("losses") or 0,
    })
    if still_open:
        latest = still_open[-1]
        su.update({
            "side": latest["side"], "entry_price": latest["entry_price"],
            "tp_price": latest["tp_price"], "notional": latest["notional"],
            "margin": latest["margin"], "opened_at": latest["opened_at"],
        })
    else:
        su.update({"side": "none", "entry_price": 0, "tp_price": 0, "notional": 0,
                   "margin": 0, "opened_at": None, "position_open": False})
    # positions JSON LAST — it lives in last_error and must not be clobbered
    su.update(serialize_positions(still_open))
    # ── HEARTBEAT: silence (all slots full, nothing closing) must not look
    # like the bot died. Ping Telegram once an hour with no open/close events.
    hb_prev = None
    try:
        snap = json.loads(state.get("last_reversal_at") or "{}")
        hb_prev = snap.get("hb") if isinstance(snap, dict) else None
    except (ValueError, TypeError):
        hb_prev = None
    event_happened = bool(closed_any) or opened_this_tick
    hb_new = now
    if not event_happened and hb_prev:
        try:
            silent_for = (datetime.fromisoformat(now) - datetime.fromisoformat(hb_prev)).total_seconds()
            if silent_for < HEARTBEAT_SEC:
                hb_new = hb_prev
            else:
                send_telegram(
                    "\U0001F4A3 Scalper ALIVE — just quiet: every position waits on TP (no stop loss)\n"
                    f"{len(still_open)}/{MAX_POSITIONS} slots full | Balance ${balance:.2f} | "
                    f"Equity ${balance + unrealized:.2f} (floating {unrealized:+.2f})"
                )
        except (ValueError, TypeError):
            hb_new = now
    # equity + heartbeat snapshot in another legacy string field for observability
    su["last_reversal_at"] = json.dumps(
        {"eq": round(balance + unrealized, 4), "u": round(unrealized, 4),
         "n": len(still_open), "hb": hb_new},
        separators=(",", ":"))
    sync(state_update=su, trade=closed_any[0] if closed_any else None)
    for t in closed_any[1:]:
        sync(trade=t)
    if closed_any:
        return "closed", f"closed {len(closed_any)} TP(s), {len(still_open)} open"
    if want is not None and not can_open:
        return "none", f"signal {want} skipped — slots/margin full ({len(still_open)}/{MAX_POSITIONS})"
    return "none", f"{len(still_open)} open | price ${price:.4f}"


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
            if not locals().get("_alerted"):
                _alerted = True  # one alert per run — a crash-looping engine must not look like quiet grinding
                try:
                    send_telegram(f"\u26a0\ufe0f Scalper ENGINE ERROR (tick paused this cycle): {str(e)[:150]}")
                except Exception:
                    pass
            try:
                # CRITICAL: last_error now stores the open-positions JSON.
                # A transient error (Bitget timeout, network blip) must NEVER
                # wipe it — that would orphan real open positions.
                cur = get_state()
                if not parse_positions(cur):
                    sync(state_update={"last_error": str(e)[:200]})
                else:
                    log("positions intact — error logged to Actions log only")
            except Exception as e2:
                log(f"ERROR updating state: {e2}")

        elapsed = time.time() - start
        if elapsed < MAX_RUNTIME:
            time.sleep(max(1, POLL_INTERVAL - (time.time() - start - elapsed) % POLL_INTERVAL))

    log(f"Run complete: {ticks} ticks, {trades} trades, {time.time()-start:.0f}s")


if __name__ == "__main__":
    main()
