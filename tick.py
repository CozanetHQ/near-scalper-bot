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

# Phase 3 module pipeline (owner production path 2026-09-11): pure computation
# lives in the engine package; tick.py orchestrates fetch/sync/execution.
from engine.features import candle_color, compute_atr, compute_ema
from engine.positions import serialize_positions, parse_positions, classify_trade_state
from engine.second_engine import score_signal  # Phase 5: advisory-only (validated dist_to_wall; no gating)
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

BITGET = "https://api.bitget.com/api/v2/mix/market"
SYMBOL = "NEARUSDT"
PRODUCT = "USDT-FUTURES"
# ── MULTI-PAIR: one shared account, per-pair intelligence, global slot budget.
# Locked a priori (registry: PAIRS) — deepest-liquidity Bitget USDT perps,
# NEAR retained for continuity with the single-pair era.
PAIRS = [p.strip().upper() for p in os.environ.get(
    "PAIRS", "NEARUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT").split(",") if p.strip()]
_REPO = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(_REPO, "state", "state.json"))
TRADES_FILE = os.environ.get("TRADES_FILE", os.path.join(_REPO, "data", "trades.jsonl"))
ATR_PERIOD = 14
SL_ATR_MULT = 4.0     # GRID SEARCH WINNER (1152 configs): wide stop, rarely hit
TP_SL_RATIO = 1.5     # most exits are 20-min drift-capture time stops
WIN_TARGET_DOLLARS = 0.05  # OWNER 09-07: each TP aims for $0.05 (slot cap still binds in low vol)
WIN_TARGET_PCT = float(os.environ.get("WIN_TARGET_PCT", "0.0167"))
SLIP_PCT = float(os.environ.get("SLIP_PCT", "0"))  # owner 09-08 stress lab: extra per-side execution cost (slippage/spread/failed fills)
TRANSITION_GATE = os.environ.get("TRANSITION_GATE", "0")  # owner 09-08: yellow-state — closed 4H vs forming 4H+15m disagree = pause OLD-direction entries  # OWNER 09-08: speak percentages — target = 1.67% of balance (=$0.05 at $3), auto-compounds with the account; 0 = fixed dollars
LIQ_MODEL = os.environ.get("LIQ_MODEL", "0")
TREND_MULT = float(os.environ.get("TREND_MULT", "1.0"))
TREND_CHASE = os.environ.get("TREND_CHASE", "0")  # OWNER 09-08 hypothesis: during clear 4H+15m bias, ALSO enter WITH the trend (momentum candle, no pullback)  # OWNER 09-08: multiply win target when trade aligns with clear 4H+15m bias  # 1 = model EXCHANGE LIQUIDATION (lab only):
# a position whose adverse move crosses 1/leverage is force-closed at the liq
# price for a realized loss of its margin. Paper default OFF (sim floats wedges
# forever); ON it exposes the true tail of high-leverage configs.
SCALP_TP_ATR = 1.6  # OWNER 09-08: two-week lab verdict — 1.6x ATR is the robust center (capital-time 0.0070 $/(cap.h) IDENTICAL on both regime weeks; hostile-week maxDD halved -$0.76 vs -$5.20 at 1.2x; 2.0x hits the recycle cliff)   # TP distance = 1.2x 1m ATR (adaptive to live volatility)
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "8"))  # OWNER 09-11: back to 8 slots after the concentration review (whole-account trial: one MAE_KILL outweighed a month of wins). Registry-locked.
CONCENTRATED = os.environ.get("CONCENTRATED", "0")  # OWNER 09-11: 0 — slot sizing (each position ~1/8 of budget, losses sliced small). 1 = whole-account deployment. Registry-locked.
# lab confirmed 4 slots strictly better: realized +3.84 vs +3.58, equity +0.51 vs -0.30, half the wedges    # hedge scalper: multiple concurrent positions — wedged trades don't stop the chopping
MARGIN_BUDGET = float(os.environ.get("MARGIN_BUDGET", "0.85"))  # owner 09-07 17:40: raised so $0.05 TPs actually materialize at $3 balance (watch-period experiment; live plan stays 40%).
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
INVENTORY_CAP = float(os.environ.get("INVENTORY_CAP", "999"))
SAFE_DD = float(os.environ.get("SAFE_DD", "0.25"))
RECOVERY_SIZE_FRAC = float(os.environ.get("RECOVERY_SIZE_FRAC", "0.25"))  # OWNER 2026-09-11: below the SAFE_DD wall, entries are no longer PAUSED (a paused account can never recover — Phase 4 finding). Slots run at 1/4 size until balance recovers above the boundary; full size returns automatically. Never below the $1 dust floor so the account keeps trading.  # owner 09-08 Engine 10: HARD realized-DD wall — pause ALL new entries at -25% from peak balance; the intelligence may NEVER override (0 disables for lab only)  # OFF by default — lab 09-06: every throttle variant starved the grind that pays for wedges (raw equity -$0.26 vs -$3.2 to -$5.3 hardened)

# ── v4 FALSIFICATION-HARDENED SPEC — LOCKED PROTECTIVE KILL (owner 09-10) ──
# HONEST SCOPE NOTE: this is a single Python process polling Bitget's public
# REST API from GitHub Actions. It has no real order routing, no exchange-side
# resting stop order, no independent second host/network/API-credential
# watchdog, no hedge instrument, and no venue-level dead-man's switch. Per the
# spec's own Section 16 (Absolute Prohibitions) and Section 9 ("monitor-only
# protection remains explicitly insufficient"), this bot CANNOT claim Tier 0-3
# kill-switch infrastructure and does not pretend to. What it CAN honestly do,
# and now does: replace the prior "TP only, no SL, no time-stop, no loss ever
# realized" design with an always-on SOFTWARE MONITOR that force-closes any
# position at market past a hard adverse-move or hard age ceiling. This is a
# deliberate behavior change — this bot WILL now realize real (paper) losses
# it previously let float indefinitely. Values are locked HERE, dated, and
# registered in param_registry.json BEFORE being run against new results —
# they are not to be tuned by grid search against backtest performance
# (that is precisely the unfalsifiable practice Section 1.1 prohibits).
HARD_SL_FRAC = float(os.environ.get("HARD_SL_FRAC", "0.06"))   # RE-LOCKED 2026-09-10 (v5, BEFORE any results observed under it): 0.06. Rationale, a priori: at 10x isolated leverage the exchange force-liquidates at ~9.5% adverse — the prior locked 0.12 sat BEYOND that point, so in live trading it could never be the first exit (the exchange would liquidate first); 0.06 sits well inside it, caps tail loss at 60% of slot margin, and makes the scalp EV structure honest: breakeven win rate = 0.06/(0.06+~0.014 net TP frac) ~= 0.81, below the 93.8% backtest win rate. Not tuned on new results.

# ── v5 MARKET-INTELLIGENCE DECISION LAYER (owner 09-10, Market Intelligence
# Research Report): EV-after-costs entry gate, regime classifier, WAIT as a
# real position. All values below are LOCKED a priori in param_registry.json
# BEFORE the first run under them (v4 Sec 1.1 discipline).
EV_PRIOR_WINRATE = float(os.environ.get("EV_PRIOR_WINRATE", "0.85"))   # pseudo-observed win rate blended into p_win. Conservative vs 93.8% backtest; above the ~0.81 EV breakeven.
EV_PRIOR_WEIGHT = float(os.environ.get("EV_PRIOR_WEIGHT", "20"))      # pseudo-trade weight of the prior vs observed W/L since reset.
EV_MARGIN_REQ = float(os.environ.get("EV_MARGIN_REQ", "0.0005"))
EV_ASSUMED_LOSS_FRAC = float(os.environ.get("EV_ASSUMED_LOSS_FRAC", "0.02"))
# ── P2 RISK ENGINE (owner spec 2026-09-11, params locked a priori from the
# mfe_mae_seed_NEAR_30d.json empirical harvest — see research/position_management_spec.md)
DYN_SL = os.environ.get("DYN_SL", "1")   # breakeven protection once MFE reaches BE_TRIGGER of TP distance
BE_TRIGGER_FRAC = float(os.environ.get("BE_TRIGGER_FRAC", "0.5"))   # 141/146 trades reach 50% of TP dist; of those 91% hit TP — the cohort worth making risk-free
MAE_CEIL_FRAC = float(os.environ.get("MAE_CEIL_FRAC", "0.04"))      # 4%: winners MAE p90 = 2.89%, ~5/128 winners ever bled past 4%, all 7 HARD_SL deaths ride through it — caps tail kills by a third   # OWNER RELEASE 2026-09-11: the EV gate plans against a 2% adverse move, not the full 6% HARD_SL. HARD_SL_FRAC (0.06) is unchanged as the ACTUAL backstop exit; this is only the gate's planning assumption, re-locked as an explicit owner override after 20h of zero-trade deadlock.
SLIP_ASSUMED_PCT = float(os.environ.get("SLIP_ASSUMED_PCT", "0.0001")) # EV-model slippage per side (live paper SLIP_PCT stays 0; the EV math must still pay for friction).
TREND_RUNAWAY_CANDLES = int(os.environ.get("TREND_RUNAWAY_CANDLES", "6"))  # last N closed 15m candles ALL one color => runaway regime: counter-trend entries blocked (wedge lesson).
SPIKE_RANGE_MULT = float(os.environ.get("SPIKE_RANGE_MULT", "3.0"))    # live 1m candle range > N x 1m ATR => spike regime: block entries this tick.
MAX_POS_AGE_HOURS = float(os.environ.get("MAX_POS_AGE_HOURS", "48"))  # locked 2026-09-10: hard-close at market if a position has been open >= 48h regardless of TP-aging tier. TP_AGING relaxes the TARGET; it never forces an exit — this does.

# ── PER-PAIR PARAMETERS (owner standing rule 2026-09-11: every risk/execution
# rule is tested and locked PER PAIR, never as one global setting — pairs have
# different behavior; the 8h age-cap grid proved one number cannot fit five
# pairs). The registry's "per_pair" section is the LOCKED source of truth:
# {"<PARAM>": {"<PAIR>": {"value": x, "locked_at": ..., "rationale": ...}}}.
# A pair absent from per_pair uses the global default above. Lab overrides:
# env PER_PAIR_OVERRIDE='{"MAX_POS_AGE_HOURS": {"ETHUSDT": 4}}' (lab only —
# the live workflow never sets it, so live always runs locked values).
_PER_PAIR_OVERRIDES = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "param_registry.json")) as _f:
        _PER_PAIR_OVERRIDES = json.load(_f).get("per_pair") or {}
except Exception:
    _PER_PAIR_OVERRIDES = {}
try:
    _PAIR_ENV_OVERRIDES = json.loads(os.environ.get("PER_PAIR_OVERRIDE") or "{}")
except (ValueError, TypeError):
    _PAIR_ENV_OVERRIDES = {}

def pair_param(name, symbol, default):
    """Effective per-pair parameter: lab env override > registry per_pair > global default."""
    env_ov = _PAIR_ENV_OVERRIDES.get(name, {}).get(symbol)
    if env_ov is not None:
        try:
            return type(default)(env_ov)
        except (TypeError, ValueError):
            pass
    entry = _PER_PAIR_OVERRIDES.get(name, {}).get(symbol)
    if isinstance(entry, dict):
        v = entry.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return type(default)(v) if isinstance(v, float) or isinstance(default, float) else v
    return default
# Slot recycling: a position stuck for hours relaxes its TP toward entry.
# It NEVER crosses entry — no loss is ever realized. It just stops demanding
# full profit to free the slot. Fees are always covered (keep > fee buffer).
TP_AGING = os.environ.get("TP_AGING", "1") == "1"
TP_AGING_TIERS = [(6, 0.5), (24, 0.25), (72, 0.10)]  # (hours stuck, fraction of TP distance kept)

# ── TEAM COORDINATION (owner 09-07: "if 3 traders are losing, work together") ──
# COORD_MODE: 0 = off (each slot trades its own signal)
#             1 = HELP: when one side's book is wounded (floating < -1% of
#                 balance), only entries in THAT direction are allowed — the
#                 team pushes toward the wounded side's recovery.
#             2 = HEDGE: the opposite — when one side is wounded, entries in
#                 that direction are skipped so the book's net exposure to
#                 its own wound can't grow.
COORD_MODE = int(os.environ.get("COORD_MODE", "0"))
COORD_WOUND = 0.01  # side is "wounded" when its floating < -1% of balance

# ── SIGNAL PURITY FILTERS (owner 09-07: "pick moves from signal, not noise") ──
# Each independently togglable; all default OFF until lab-proven.
SIG_VOL_CONFIRM = os.environ.get("SIG_VOL_CONFIRM", "0")   # 1 = pullback candle must be LOW volume
SIG_EMA_SLOPE = os.environ.get("SIG_EMA_SLOPE", "0")       # 1 = EMA slope must agree with momentum
SIG_NO_CHASE = os.environ.get("SIG_NO_CHASE", "0")         # 1 = don't enter when price is extended from EMA fast

# ── STRUCTURE-AWARE TP (owner 09-07: "a human sees swing highs on the chart
# and puts the TP where the market already reacts") ──
SWING_TP = os.environ.get("SWING_TP", "0")                 # 1 = TP at nearest swing level, else ATR formula
SWING_MAX = float(os.environ.get("SWING_MAX", "1.6"))      # swing TP allowed up to this x the ATR distance
SWING_FRONT = 0.85                                         # front-run the level by 15% of the distance

def nearest_swing_tp(closed15m, side, price):
    """Nearest PROVEN swing high (for longs) / swing low (for shorts) in the
    trade's favor, from the last 40 closed 15m candles. Fractal rule: a level
    must dominate the 2 candles on each side — the same swing highs a human
    marks on the chart."""
    cs = closed15m[-40:]
    if len(cs) < 6:
        return None
    highs = [c["high"] for c in cs]
    lows = [c["low"] for c in cs]
    levels = {"hi": [], "lo": []}
    for j in range(2, len(cs) - 2):
        if highs[j] > max(highs[j-2:j]) and highs[j] > max(highs[j+1:j+3]):
            levels["hi"].append(highs[j])
        if lows[j] < min(lows[j-2:j]) and lows[j] < min(lows[j+1:j+3]):
            levels["lo"].append(lows[j])
    if side == "long":
        above = [h for h in levels["hi"] if h > price]
        return min(above) if above else None
    below = [l for l in levels["lo"] if l < price]
    return max(below) if below else None
MIN_TP_DIST_FRAC = float(os.environ.get("MIN_TP_DIST_FRAC", "0.006"))  # TP floor as a FRACTION of price (~0.13%): below this, fees eat the scalp alive. Was an absolute $0.003 in the single-pair NEAR era ($2.4 price); multi-pair demands price-proportionate ($0.003/$2.42 ≈ 0.125%). Registry-locked.
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
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "0.12"))  # registry-locked 0.12 — RISK-OFF for the rest of the UTC day after losing 12% from day-start balance. OWNER 2026-09-11: WIRED (was defined, never read — Phase 4 finding 2).
MAX_STREAK = 4             # 4 consecutive SLs on one side -> extended pause
STREAK_PAUSE_SEC = 3600     # ... for one hour
RSI_PERIOD = 14
RSI_LONG_ENTRY = 50    # GRID SEARCH WINNER: loose gate — more entries, more edge
RSI_SHORT_ENTRY = 50
RSI_EARLY_VELOCITY = 3  # early entry: RSI still below/above gate but swinging this fast
LEVERAGE = 10
START_BALANCE = 3.0
FEE_RATE = 0.0006
BE_BUFFER_FRAC = 2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005   # P2: BE_STOP exit covers round-trip costs + crumb
POLL_INTERVAL = 15
MAX_RUNTIME = int(os.environ.get('MAX_RUNTIME', 240))  # ~4 min loop; next run chains immediately

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


def fetch_candles(granularity, limit=5, symbol=SYMBOL):
    url = f"{BITGET}/candles?symbol={symbol}&productType={PRODUCT}&granularity={granularity}&limit={limit}"
    data = http_get(url)
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget candles error: {data.get('msg')}")
    return [
        {"ts": int(d[0]), "open": float(d[1]), "high": float(d[2]),
         "low": float(d[3]), "close": float(d[4]), "vol": float(d[5])}
        for d in data["data"]
    ]


def fetch_ticker(symbol=SYMBOL):
    url = f"{BITGET}/ticker?symbol={symbol}&productType={PRODUCT}"
    data = http_get(url)
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget ticker error: {data.get('msg')}")
    d = data["data"][0]
    return {"last": float(d["lastPr"]), "mark": float(d["markPrice"])}






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
    net = gross - fees - (notional * SLIP_PCT * 2)  # stress: slippage both sides
    new_balance = state["balance"] + net  # margin is virtual sizing only, never reserved from balance

    trade = {
        "pair": state.get("_pair") or SYMBOL,
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
        "aligned": pos.get("aligned", None),
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


def load_master():
    """The engine's single source of truth: state/state.json, committed to git
    every tick by the workflow. git history IS the audit trail."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"account": {}, "pairs": {}, "trades": []}


def fresh_pair_state(pair):
    return {
        "pair": pair, "status": "running", "side": "none", "entry_price": 0,
        "tp_price": 0, "sl_price": 0, "notional": 0, "margin": 0,
        "position_open": False, "opened_at": None, "last_error": "[]",
        "last_reversal_at": "", "last_price": 0, "wins": 0, "losses": 0,
        "total_trades": 0, "streak_side": "none", "streak_count": 0,
        "last_bias": "none", "last_1m_color": "flat", "last_15m_color": "flat",
        "last_4h_color": "flat", "last_tick_at": "",
    }


def get_state(pair):
    """Merged view for ONE pair: account fields (shared balance/peak) + the
    pair's own scalping state + cross-pair budget context + trade context."""
    master = load_master()
    acct = master.get("account") or {}
    ps = (master.get("pairs") or {}).get(pair) or fresh_pair_state(pair)
    state = dict(ps)
    state["_pair"] = pair
    bal = acct.get("balance")
    state["balance"] = bal if isinstance(bal, (int, float)) and bal > 0 else START_BALANCE
    peak = acct.get("peak_balance")
    state["peak_bal"] = peak if isinstance(peak, (int, float)) and peak > 0 else state["balance"]
    state["status"] = ps.get("status") or "running"
    trades = [t for t in (master.get("trades") or [])]
    pair_trades = [t for t in trades if (t.get("pair") or SYMBOL) == pair]
    if pair_trades:
        state["_last_close"] = pair_trades[0].get("closed_at")
        state["_last_reason"] = pair_trades[0].get("reason")
    # Day-start balance (UTC) for the daily loss circuit breaker — account-level.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = None
    for t in pair_trades:
        if (t.get("closed_at") or "").startswith(today):
            continue
        day_start = t.get("balance_after")
        break
    if day_start is None:
        day_start = state["balance"]
    state["_day_start_balance"] = day_start
    # Cross-pair budget context: slots + margin are GLOBAL, not per-pair.
    other_open, other_margin = 0, 0.0
    for op, ops in (master.get("pairs") or {}).items():
        if op == pair:
            continue
        for p in parse_positions(ops, ops.get("pair") or SYMBOL):
            other_open += 1
            other_margin += float(p.get("margin") or 0)
    state["_other_open"] = other_open
    state["_other_margin"] = round(other_margin, 6)
    return state


def sync(pair, state_update=None, trade=None):
    """Persist ONE pair's tick: shared account fields + pair fields to
    state/state.json, and append any closed trade to the immutable ledger."""
    master = load_master()
    acct = master.setdefault("account", {})
    su = state_update or {}
    if "balance" in su:
        acct["balance"] = su["balance"]
        try:
            acct["peak_balance"] = max(float(acct.get("peak_balance") or 0), float(su["balance"]))
        except (ValueError, TypeError):
            acct["peak_balance"] = float(su["balance"])
        acct["status"] = su.get("status") or acct.get("status") or "running"
        acct["last_tick_at"] = su.get("last_tick_at") or acct.get("last_tick_at")
        acct["max_positions"] = MAX_POSITIONS  # dashboard reads this — no hardcoded slot counts anywhere
    ps = master.setdefault("pairs", {}).setdefault(pair, fresh_pair_state(pair))
    for k, v in su.items():
        if k == "balance":
            continue
        ps[k] = v
    if trade:
        tr = dict(trade)
        tr.setdefault("pair", pair)
        master.setdefault("trades", []).insert(0, tr)
        master["trades"] = master["trades"][:60]  # working window; full ledger → trades.jsonl
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(tr, separators=(",", ":")) + "\n")
    master["updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(master, f, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)
    return {"ok": True}


SWING_AGE = os.environ.get("SWING_AGE", "0")  # 1 = aged TPs ALSO relax to the nearest
# proven swing level price is recovering toward (owner: "exit where the chart reacts")

def aged_tp(pos, now, closed15m=None):
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
    # structure-aware aging: if a PROVEN swing level sits between the aged TP
    # and entry, exit AT that level — price is far likelier to touch the level
    # than travel all the way home to entry.
    if SWING_AGE == "1" and closed15m:
        lvl = nearest_swing_tp(closed15m, pos["side"], pos["entry_price"])
        if lvl is not None:
            # level must be in the recovery zone: past the aged TP, not past entry
            if pos["side"] == "long" and pos["entry_price"] + keep < lvl < pos["entry_price"] + dist:
                keep = lvl - pos["entry_price"]
            elif pos["side"] == "short" and pos["entry_price"] - dist < lvl < pos["entry_price"] - keep:
                keep = pos["entry_price"] - lvl
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
    net = gross - fees - (notional * SLIP_PCT * 2)  # stress: slippage both sides
    new_balance = balance + net
    # Owner spec 2026-09-11, principle 2: trade failure (immediately wrong) and
    # target failure (moved substantially toward TP, then failed) are DIFFERENT
    # situations and get different management downstream. Classify at close:
    #   target_success — TP hit
    #   target_failure — MFE >= 50% of TP distance but never hit
    #   trade_failure  — barely moved favorably before exit (MFE < 25% of TP dist)
    tp_price = pos.get("tp_price") or 0
    tp_frac = abs(tp_price - entry) / entry if (tp_price and entry) else 0.0
    mfe = pos.get("mfe_frac") or 0.0
    t_state = classify_trade_state(reason, mfe, tp_frac)
    trade = {
        "pair": pos.get("pair") or state.get("_pair") or SYMBOL,
        "side": pos["side"],
        "trade_state": t_state,
        "mfe_frac": round(mfe, 6),
        "mae_frac": round(pos.get("mae_frac") or 0.0, 6),
        "minutes_held": (int((datetime.fromisoformat(now) - datetime.fromisoformat(pos["opened_at"])).total_seconds() // 60)
                          if pos.get("opened_at") else None),
        "tp_frac": round(tp_frac, 6),
        "entry_price": entry,
        "exit_price": exit_price,
        "notional": notional,
        "margin": pos["margin"],
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "balance_after": round(new_balance, 6),
        "aligned": pos.get("aligned", None),
        "advisory_score": pos.get("advisory_score"),
        "wall_ratio": pos.get("wall_ratio"),
        "opened_at": pos["opened_at"],
        "closed_at": now,
    }
    return trade, new_balance




def registry_check():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "param_registry.json")) as f:
            reg = json.load(f)
        live = {
            "HARD_SL_FRAC": HARD_SL_FRAC,
            "MAX_POS_AGE_HOURS": MAX_POS_AGE_HOURS,
            "LEVERAGE": LEVERAGE,
            "MAX_POSITIONS": MAX_POSITIONS,
            "EV_PRIOR_WINRATE": EV_PRIOR_WINRATE,
            "EV_MARGIN_REQ": EV_MARGIN_REQ,
            "SLIP_ASSUMED_PCT": SLIP_ASSUMED_PCT,
            "TREND_RUNAWAY_CANDLES": TREND_RUNAWAY_CANDLES,
            "SPIKE_RANGE_MULT": SPIKE_RANGE_MULT,
            "MIN_TP_DIST_FRAC": MIN_TP_DIST_FRAC,
            "EV_ASSUMED_LOSS_FRAC": EV_ASSUMED_LOSS_FRAC,
            "BE_TRIGGER_FRAC": BE_TRIGGER_FRAC,
            "MAE_CEIL_FRAC": MAE_CEIL_FRAC,
            "BE_BUFFER_FRAC": BE_BUFFER_FRAC,
            "CONCENTRATED": CONCENTRATED,
            "PAIRS": ",".join(PAIRS),
            "RECOVERY_SIZE_FRAC": RECOVERY_SIZE_FRAC,
            "DAILY_LOSS_LIMIT": DAILY_LOSS_LIMIT,
        }
        mismatches = []
        for k, v in live.items():
            locked = reg.get("params", {}).get(k, {}).get("value")
            if locked is None:
                continue
            if isinstance(v, str):
                if locked != v:
                    mismatches.append(f"{k}: live={v} locked={locked}")
            elif float(locked) != float(v):
                mismatches.append(f"{k}: live={v} locked={locked}")
        for pname, pmap in (reg.get("per_pair") or {}).items():
            if not isinstance(pmap, dict):
                mismatches.append(f"per_pair {pname}: not a dict")
                continue
            for pair, entry in pmap.items():
                if not isinstance(entry, dict) or "value" not in entry or "locked_at" not in entry \
                        or "rationale" not in entry:
                    mismatches.append(f"per_pair {pname}.{pair}: missing value/locked_at/rationale")
        return (len(mismatches) == 0), mismatches
    except Exception as e:
        return False, [f"registry read failed: {e}"]


def process_tick(state):
    """One poll cycle for ONE pair. Returns (action, details)."""
    symbol = state.get("_pair") or SYMBOL
    PTAG = symbol.replace("USDT", "")
    def _pt(text):  # every alert from this pair carries its tag
        send_telegram(f"[{PTAG}] {text}")
    c4h = fetch_candles("4H", 2, symbol)
    c15m = fetch_candles("15m", 14, symbol)  # closed candles for zone + sweep structure detection
    c1m = fetch_candles("1m", 24, symbol)  # 23 closed candles: EMA21 needs 21
    ticker = fetch_ticker(symbol)

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
    positions = parse_positions(state, SYMBOL)
    bal_raw = state.get("balance")
    balance = bal_raw if isinstance(bal_raw, (int, float)) and bal_raw > 0 else START_BALANCE
    # ── OWNER 09-08 SELF-DIAGNOSTIC ENGINE: hard drawdown boundary.
    # Safety wall, not a strategy opinion: realized balance below (1-SAFE_DD)
    # of its running peak pauses ALL new entries until the account recovers.
    safe_prev = 0
    try:
        _snap = json.loads(state.get("last_reversal_at") or "{}")
        if isinstance(_snap, dict):
            safe_prev = int(_snap.get("sf") or 0)
    except (ValueError, TypeError):
        safe_prev = 0
    # Peak is ACCOUNT-level now (shared balance across pairs).
    peak_bal = max(balance, float(state.get("peak_bal") or 0))
    safe_on = SAFE_DD > 0 and balance < peak_bal * (1 - SAFE_DD)
    if safe_on and not safe_prev:
        try:
            _pt(
                "\U0001F6D1 SAFE MODE — hard risk boundary hit (non-negotiable):\n"
                f"balance ${balance:.2f} is below {(1-SAFE_DD)*100:.0f}% of peak ${peak_bal:.2f}\n"
                "New entries paused. Open positions ride to TP as normal.")
        except Exception:
            pass
    elif not safe_on and safe_prev:
        try:
            _pt("\u2705 RECOVERY COMPLETE — balance back above the drawdown boundary. Full slot size restored.")
        except Exception:
            pass
    # ── OWNER 2026-09-11: DAILY LOSS LIMIT — now WIRED (was defined, never read).
    # Realized balance down DAILY_LOSS_LIMIT from the UTC day-start close →
    # no NEW entries until the next UTC day. Open positions ride as normal.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = state.get("_day_start_balance") or balance
    riskoff_prev_day = None
    try:
        _snap2 = json.loads(state.get("last_reversal_at") or "{}")
        if isinstance(_snap2, dict):
            riskoff_prev_day = _snap2.get("drd")
    except (ValueError, TypeError):
        riskoff_prev_day = None
    daily_risk_off = DAILY_LOSS_LIMIT > 0 and riskoff_prev_day == today
    if (not daily_risk_off and DAILY_LOSS_LIMIT > 0 and day_start > 0
            and balance <= day_start * (1 - DAILY_LOSS_LIMIT)):
        daily_risk_off = True
        state["_riskoff_day"] = today
        try:
            _pt("\U0001F6D1 DAILY LOSS LIMIT — realized balance ${:.2f} is down {:.0f}% from the UTC day start ${:.2f}.\n"
                "No new entries until the next UTC day. Open positions ride to TP as normal.".format(balance, 100 * DAILY_LOSS_LIMIT, day_start))
        except Exception:
            pass
    scan = fetch_candles("1m", 15, symbol)  # gap-aware TP scan — 2026-09-11 LIVE BUG FIX (owner-authorized): the missing symbol arg defaulted to NEARUSDT, so every pair's MFE/MAE telemetry ran on NEAR candles — 20/120 live trades were bogus MAE_KILLs.
    closed_any = []
    still_open = []
    for pos in positions:
        tp = aged_tp(pos, now, c15m[:-1])
        if tp != (pos.get("tp_price") or 0):
            pos["tp_price"] = tp  # aged — persisted via serialize
        if tp <= 0:
            continue
        is_long = pos["side"] == "long"
        # ── Position telemetry: MFE/MAE from candle extremes since entry.
        # Favorable = toward TP. This is the raw feed for the empirical
        # MFE->retracement->P(TP) and MAE->recovery->P(further loss)
        # distributions (owner spec 2026-09-11, principle 3).
        _e = pos["entry_price"]
        opened_ms = None
        if pos.get("opened_at"):
            try:
                opened_ms = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
            except (ValueError, TypeError):
                opened_ms = None
        for candle in scan:
            if opened_ms is not None and candle["ts"] < opened_ms:
                continue
            if is_long:
                fav = (candle["high"] - _e) / _e
                adv = (_e - candle["low"]) / _e
            else:
                fav = (_e - candle["low"]) / _e
                adv = (candle["high"] - _e) / _e
            pos["mfe_frac"] = max(pos.get("mfe_frac") or 0.0, fav)
            pos["mae_frac"] = max(pos.get("mae_frac") or 0.0, adv)
        hit_tp = price >= tp if is_long else price <= tp
        if not hit_tp:
            for candle in scan:
                if opened_ms is not None and candle["ts"] < opened_ms:
                    continue
                if is_long and candle["high"] >= tp:
                    hit_tp = True
                    break
                if not is_long and candle["low"] <= tp:
                    hit_tp = True
                    break
        # liquidation check FIRST — in real isolated margin the exchange closes
        # the slot before any TP could matter.
        if LIQ_MODEL == "1" and pos.get("margin") and pos["margin"] > 0:
            adverse = (pos["entry_price"] - price) / pos["entry_price"] if pos["side"] == "long" else (price - pos["entry_price"]) / pos["entry_price"]
            liq_frac = (1.0 / LEVERAGE) - 0.005  # maintenance buffer
            if adverse >= liq_frac:
                liq_price = pos["entry_price"] * (1 - liq_frac) if pos["side"] == "long" else pos["entry_price"] * (1 + liq_frac)
                trade, balance = close_position(pos, liq_price, "LIQ", now, balance)
                closed_any.append(trade)
                _pt(
                    f"\u2620\ufe0f *LIQUIDATED {pos['side'].upper()}*\n"
                    f"Entry ${pos['entry_price']:.4f} → LiQ ${liq_price:.4f}\n"
                    f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
                )
                continue
        # HARD_SL — locked protective kill (v4 spec, software-monitor, honestly scoped).
        # Fires before liquidation math would, and independent of TP/TP-aging.
        hs_side = pos["side"]
        hs_adverse = (pos["entry_price"] - price) / pos["entry_price"] if hs_side == "long" else (price - pos["entry_price"]) / pos["entry_price"]
        if HARD_SL_FRAC > 0 and hs_adverse >= HARD_SL_FRAC:
            trade, balance = close_position(pos, price, "HARD_SL", now, balance)
            closed_any.append(trade)
            _pt(
                f"\U0001F6D1 *HARD_SL — {pos['side'].upper()} force-closed*\n"
                f"Entry ${pos['entry_price']:.4f} → ${price:.4f} (adverse {hs_adverse*100:.1f}% >= locked {HARD_SL_FRAC*100:.0f}%)\n"
                f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
            continue
        # ── P2 RISK ENGINE — MAE ceiling: beyond the empirical recovery band,
        # the ride stops paying. Caps tail loss at MAE_CEIL_FRAC instead of
        # letting doomed trades rot to the 6% HARD_SL (median 15.2h in the seed).
        if MAE_CEIL_FRAC > 0 and (pos.get("mae_frac") or 0.0) >= MAE_CEIL_FRAC:
            trade, balance = close_position(pos, price, "MAE_KILL", now, balance)
            closed_any.append(trade)
            _pt(
                f"\U0001F6A8 *MAE_KILL — {pos['side'].upper()} exit at ceiling*\n"
                f"Adverse {(pos.get('mae_frac') or 0.0)*100:.1f}% >= locked {MAE_CEIL_FRAC*100:.0f}% — recovery odds gone\n"
                f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
            continue
        # ── P2 RISK ENGINE — dynamic SL: once MFE reached BE_TRIGGER of the TP
        # distance (the 91%-TP cohort in the seed), the trade becomes risk-free:
        # exit at breakeven+costs if price retraces through it. NEVER averages
        # down (owner principle 1) — this only manages the existing position.
        if DYN_SL == "1" and tp > 0:
            _tp_frac = abs(tp - pos["entry_price"]) / pos["entry_price"]
            _mfe = pos.get("mfe_frac") or 0.0
            if _tp_frac > 0 and _mfe >= BE_TRIGGER_FRAC * _tp_frac:
                if is_long:
                    _be = pos["entry_price"] * (1 + BE_BUFFER_FRAC)
                    if price <= _be:
                        trade, balance = close_position(pos, _be, "BE_STOP", now, balance)
                        closed_any.append(trade)
                        _pt(
                            f"\U0001F6E1\uFE0F *BE_STOP — {pos['side'].upper()} protected at breakeven*\n"
                            f"MFE {_mfe*100:.2f}% armed protection; retracement capped at entry+costs\n"
                            f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
                        )
                        continue
                else:
                    _be = pos["entry_price"] * (1 - BE_BUFFER_FRAC)
                    if price >= _be:
                        trade, balance = close_position(pos, _be, "BE_STOP", now, balance)
                        closed_any.append(trade)
                        _pt(
                            f"\U0001F6E1\uFE0F *BE_STOP — {pos['side'].upper()} protected at breakeven*\n"
                            f"MFE {_mfe*100:.2f}% armed protection; retracement capped at entry+costs\n"
                            f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
                        )
                        continue
        # MAX_POS_AGE — locked hard age kill. TP_AGING only relaxes the target;
        # this is the actual forced exit the old design never had.
        pos_age_h = None
        if pos.get("opened_at"):
            try:
                pos_age_h = (datetime.fromisoformat(now) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600.0
            except (ValueError, TypeError):
                pos_age_h = None
        _age_cap = pair_param("MAX_POS_AGE_HOURS", symbol, MAX_POS_AGE_HOURS)
        if _age_cap > 0 and pos_age_h is not None and pos_age_h >= _age_cap:
            trade, balance = close_position(pos, price, "MAX_AGE", now, balance)
            closed_any.append(trade)
            _pt(
                f"\u23F0 *MAX_AGE — {pos['side'].upper()} force-closed*\n"
                f"Open {pos_age_h:.1f}h >= locked {_age_cap:.0f}h ceiling ({symbol})\n"
                f"Entry ${pos['entry_price']:.4f} → ${price:.4f} | PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
            continue
        if hit_tp:
            trade, balance = close_position(pos, tp, "TP", now, balance)
            closed_any.append(trade)
            _pt(
                f"\u2705 *Closed {pos['side'].upper()} (TP)*\n"
                f"Entry ${pos['entry_price']:.4f} → TP ${tp:.4f}\n"
                f"PnL +${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
        else:
            still_open.append(pos)

    # ── ENTRY: chop the CURRENT move. Pullback candle in live momentum = entry.
    regime = "chop"
    wait_reason = None
    # Hoisted so a registry mismatch (or non-running status) degrades gracefully:
    # positions ride, entries WAIT, the heartbeat fires — instead of crashing
    # with UnboundLocalError at the first post-block reference (found 2026-09-11
    # when a lab env override tripped the mismatch path).
    want = None
    can_open = False
    opened_this_tick = False
    reg_ok, reg_mismatches = registry_check()
    if not reg_ok:
        _pt(
            "\U0001F512 *PARAMETER LINEAGE MISMATCH — new entries WAIT\'d (v4 Sec 1.1)*\n"
            + "\n".join(reg_mismatches[:5])
            + "\nA locked constant changed without re-locking param_registry.json. Existing positions still ride their HARD_SL/MAX_AGE/TP kills normally."
        )
    if state.get("status") == "running" and reg_ok:
        atr = compute_atr(c1m[:-1], ATR_PERIOD)
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
                # OWNER 09-08 hypothesis: "with a clear direction, scalping is easy"
                # → also enter WITH a clear 4H+15m bias on a same-direction momentum candle
                if want is None and TREND_CHASE == "1" and bias != "none":
                    if momentum == bias and last_color == bias:
                        want = "long" if bias == "bull" else "short"
                if TRANSITION_GATE == "1" and want is not None and len(c4h) >= 2:
                    closed_c = candle_color(c4h[0]); forming_c = candle_color(forming_4h)
                    if (forming_c != closed_c and forming_c == candle_color(forming_15m)
                            and want == ("long" if closed_c == "bull" else "short")):
                        want = None  # reversal in progress — pause old-direction harvesting
        if want:
            # ── signal purity filters (lab-gated; all default OFF) ──
            if SIG_VOL_CONFIRM == "1":
                vols = [c["vol"] for c in c1m[:-1][-11:-1]]
                if vols and len(vols) >= 5:
                    avg_v = sum(vols) / len(vols)
                    if last_closed["vol"] > 1.2 * avg_v:   # heavy-volume "pullback" = possible reversal, skip
                        want = None
            if SIG_EMA_SLOPE == "1" and want and ema_fast is not None and ema_slow is not None:
                closes_x = [c["close"] for c in c1m[:-1]]
                if len(closes_x) >= EMA_SLOW + 3:
                    e_now = ema_fast
                    e_prev = compute_ema(closes_x[:-3], EMA_FAST)
                    if e_prev is not None:
                        if want == "long" and e_now <= e_prev:
                            want = None
                        elif want == "short" and e_now >= e_prev:
                            want = None
            if SIG_NO_CHASE == "1" and want and atr:
                ext = abs(price - ema_fast) if ema_fast is not None else 0
                if ext > 0.5 * atr:  # already extended — the move is old, don't chase
                    want = None
        if want:
            # inventory cap: how much are the open positions floating right now?
            long_float = 0.0
            short_float = 0.0
            for p in still_open:
                u = (price - p["entry_price"]) * (p["notional"] / p["entry_price"]) if p["side"] == "long" \
                    else (p["entry_price"] - price) * (p["notional"] / p["entry_price"])
                if p["side"] == "long":
                    long_float += u
                else:
                    short_float += u
            floating_now = long_float + short_float
            if floating_now < -abs(INVENTORY_CAP) * balance:
                want = None  # wedge inventory too deep — pause new entries, let TPs work
            elif COORD_MODE == 1 and want:
                # HELP the wounded side: skip entries that fight the team's book
                wound = -COORD_WOUND * balance
                if long_float < wound and want == "short":
                    want = None
                elif short_float < wound and want == "long":
                    want = None
            elif COORD_MODE == 2 and want:
                # HEDGE: skip entries that add to the wounded side's exposure
                wound = -COORD_WOUND * balance
                if long_float < wound and want == "long":
                    want = None
                elif short_float < wound and want == "short":
                    want = None
        entry_cooldown_ok = True
        if still_open:
            try:
                last_open = max(datetime.fromisoformat(p["opened_at"]) for p in still_open if p.get("opened_at"))
                if (datetime.now(timezone.utc) - last_open).total_seconds() < ENTRY_COOLDOWN_SEC:
                    entry_cooldown_ok = False
            except (ValueError, TypeError):
                pass
        # ── v5 REGIME CLASSIFIER (deterministic, from already-fetched data) ──
        c15m_closed = c15m[:-1]
        if len(c15m_closed) >= TREND_RUNAWAY_CANDLES:
            runaway_colors = [candle_color(x) for x in c15m_closed[-TREND_RUNAWAY_CANDLES:]]
            if all(col == runaway_colors[0] and col != "flat" for col in runaway_colors):
                regime = "runaway"
                run_dir = "long" if runaway_colors[0] == "bull" else "short"
                if want is not None and want != run_dir:
                    want = None
                    wait_reason = f"regime:runaway — no counter-trend vs {TREND_RUNAWAY_CANDLES}x 15m {run_dir}s"
        if want is not None and atr and atr > 0:
            spike = abs(forming_1m["high"] - forming_1m["low"]) / atr
            if spike > SPIKE_RANGE_MULT:
                want = None
                wait_reason = f"regime:spike — 1m range {spike:.1f}x ATR (manipulation/liquidation cascade risk)"
        opened_this_tick = False
        can_open = (
            want is not None
            and entry_cooldown_ok
            and len(still_open) + int(state.get("_other_open") or 0) < MAX_POSITIONS
            and not daily_risk_off
        )
        if can_open:
            used_margin = sum(p["margin"] for p in still_open) + float(state.get("_other_margin") or 0)
            margin_left = balance * MARGIN_BUDGET - used_margin
            tp_dist = max(SCALP_TP_ATR * (atr or 0.003 * price), MIN_TP_DIST_FRAC * price)
            entry_tp_dist = tp_dist
            if SWING_TP == "1":
                lvl = nearest_swing_tp(c15m[:-1], want, price)
                if lvl is not None:
                    d = (lvl - price) if want == "long" else (price - lvl)
                    d *= SWING_FRONT  # front-run the level — fill before the crowd at it
                    if MIN_TP_DIST_FRAC * price <= d <= tp_dist * SWING_MAX:
                        entry_tp_dist = d
            # ── v5 EV-AFTER-COSTS GATE (the report's decision equation, scoped
            # to what this paper bot can measure): EV = p*win - (1-p)*kill - fees
            # - assumed slip, per unit of notional. Below EV_MARGIN_REQ the
            # correct trade is WAIT, not a smaller trade.
            wins_obs = state.get("wins") or 0
            losses_obs = state.get("losses") or 0
            p_win = (EV_PRIOR_WEIGHT * EV_PRIOR_WINRATE + wins_obs) / (EV_PRIOR_WEIGHT + wins_obs + losses_obs)
            tp_frac = entry_tp_dist / price
            ev_frac = (p_win * (tp_frac - 2 * FEE_RATE)
                       - (1 - p_win) * EV_ASSUMED_LOSS_FRAC
                       - 2 * SLIP_ASSUMED_PCT)
            if ev_frac < EV_MARGIN_REQ:
                wait_reason = (f"EV gate: p={p_win:.2f} ev={ev_frac*100:+.2f}%/unit "
                               f"< req {EV_MARGIN_REQ*100:.2f}% — edge gone, WAIT")
            per_unit = entry_tp_dist / price - FEE_RATE * 2
            # per-slot cap: 8 slots x balance notional = exactly the 80% margin
            # budget at 10x — a single trade can never hog the whole budget and
            # freeze the bot (the 2026-09-06 wedge lesson).
            slot_cap = balance * MARGIN_BUDGET * LEVERAGE / MAX_POSITIONS
            if safe_on:
                # OWNER 2026-09-11 recovery sizing: 1/4 slots below the wall,
                # floored at the dust minimum so the account keeps trading.
                slot_cap = max(slot_cap * RECOVERY_SIZE_FRAC, 1.0)
            if per_unit > 0 and margin_left > 0.05 and ev_frac >= EV_MARGIN_REQ:
                aligned = (bias != "none" and want == ("long" if bias == "bull" else "short"))
                base = WIN_TARGET_PCT * balance if WIN_TARGET_PCT > 0 else WIN_TARGET_DOLLARS
                target = base * TREND_MULT if (aligned and TREND_MULT != 1.0) else base
                if CONCENTRATED == "1":
                    # OWNER 09-11: whole account straight into the trade — the
                    # single position gets the entire remaining budget; TP still
                    # >= 1.6x ATR (or the 0.6% fee-survival floor), whichever
                    # the market allows (math_spec §6).
                    notional = min(slot_cap, margin_left * LEVERAGE, 40.0)
                else:
                    notional = min(target / per_unit, slot_cap, margin_left * LEVERAGE, 40.0)
                if notional >= 1.0:  # don't open dust positions
                    # Phase 5 advisory: Second Engine scores on every entry.
                    # Logged for live out-of-sample accumulation — NEVER gates.
                    advisory = None
                    try:
                        advisory = score_signal(c15m[:-1], want, price,
                                                atr or 0.003 * price, entry_tp_dist,
                                                abs(forming_1m["high"] - forming_1m["low"]))
                    except Exception:
                        advisory = None
                    margin = notional / LEVERAGE
                    entry = price
                    tp = entry + entry_tp_dist if want == "long" else entry - entry_tp_dist
                    still_open.append({
                        "side": want, "entry_price": entry, "tp_price": tp,
                        "notional": notional, "margin": margin, "opened_at": now,
                        # Position telemetry (owner spec 2026-09-11: the empirical
                        # MFE/MAE/duration record every management decision reads).
                        "mfe_frac": 0.0, "mae_frac": 0.0,
                        "aligned": aligned, "pair": symbol,
                        "advisory_score": (advisory or {}).get("overall_score"),
                        "wall_ratio": (advisory or {}).get("wall_ratio"),
                    })
                    opened_this_tick = True
                    # OWNER 09-07: plain-dollar math on every entry — no percentages to decode.
                    _pt(
                        f"\u26a1\ufe0f *Opened {want.upper()} (scalp)*\n"
                        f"Entry ${entry:.4f} → TP ${tp:.4f} | NO SL\n"
                        f"Account ${balance:.2f} → this trader locks ${margin:.2f} ({margin/balance*100:.0f}%) and commands ${notional:.2f} ({notional/balance*100:.0f}% of account)\n"
                        f"TP pays ≈ ${notional * (abs(tp - entry) / entry - FEE_RATE * 2):.2f} (+{(notional/balance) * (abs(tp - entry) / entry - FEE_RATE * 2) * 100:.2f}% of account)"
                        f" | trader {len(still_open)}/{MAX_POSITIONS}"
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
        "wins": (state.get("wins") or 0) + sum(1 for t in closed_any if +t["net_pnl"] > 0),
        "losses": (state.get("losses") or 0) + sum(1 for t in closed_any if +t["net_pnl"] <= 0),
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
    su.update(serialize_positions(still_open, MAX_POSITIONS, SYMBOL))
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
            elif state.get("_hb_master"):
                # Portfolio heartbeat: only the first pair pings (one message,
                # not five) — the snapshot is still written for every pair.
                _pt(
                    "\U0001F4A3 Scalper ALIVE — just quiet: every position waits on TP (no stop loss)\n"
                    f"{len(still_open)}/{MAX_POSITIONS} slots full | Balance ${balance:.2f} | "
                    f"Equity ${balance + unrealized:.2f} (floating {unrealized:+.2f})"
                )
        except (ValueError, TypeError):
            hb_new = now
    # WAIT-as-a-strategy alert: fire only when the reason CHANGES (no spam).
    prev_wait = None
    try:
        _psnap = json.loads(state.get("last_reversal_at") or "{}")
        if isinstance(_psnap, dict):
            prev_wait = _psnap.get("w")
    except (ValueError, TypeError):
        prev_wait = None
    if wait_reason and wait_reason != prev_wait:
        _pt(
            "\u23F3 *WAIT — no new entry*\n"
            f"{wait_reason}\n"
            f"Slots {len(still_open)}/{MAX_POSITIONS} | Balance ${balance:.2f} — WAIT IS the trade here."
        )
    # equity + heartbeat snapshot in another legacy string field for observability
    su["last_reversal_at"] = json.dumps(
        {"eq": round(balance + unrealized, 4), "u": round(unrealized, 4),
         "n": len(still_open), "hb": hb_new, "pk": round(peak_bal, 4), "sf": 1 if safe_on else 0,
         "drd": state.get("_riskoff_day") or "",
         "rg": regime, "w": wait_reason},
        separators=(",", ":"))
    sync(symbol, state_update=su, trade=closed_any[0] if closed_any else None)
    for t in closed_any[1:]:
        sync(symbol, trade=t)
    if closed_any:
        return "closed", f"closed {len(closed_any)} TP(s), {len(still_open)} open"
    if want is not None and not can_open:
        why = ("daily risk-off (day loss limit)" if daily_risk_off
               else f"slots/margin full ({len(still_open)}/{MAX_POSITIONS})")
        return "none", f"signal {want} skipped — {why}"
    if wait_reason:
        return "none", f"WAIT ({regime}) — {wait_reason} | {len(still_open)} open"
    return "none", f"regime:{regime} | {len(still_open)} open | price ${price:.4f}"


def main():
    start = time.time()
    ticks = 0
    trades = 0
    log(f"Scalper tick run starting — {len(PAIRS)} pairs: {', '.join(PAIRS)}")
    alerted = set()

    while time.time() - start < MAX_RUNTIME:
        for pair in PAIRS:
            if time.time() - start >= MAX_RUNTIME:
                break
            try:
                state = get_state(pair)
                state["_hb_master"] = (pair == PAIRS[0])  # one portfolio heartbeat, not five
                action, details = process_tick(state)
                ticks += 1
                if action in ("opened", "closed"):
                    trades += 1
                log(f"[{pair}] tick: {action} — {details}")
            except Exception as e:
                log(f"[{pair}] ERROR: {e}")
                if pair not in alerted:
                    alerted.add(pair)  # one alert per pair per run — no crash-loop spam
                    try:
                        send_telegram(f"\u26a0\ufe0f Scalper ENGINE ERROR [{pair}] (cycle skipped): {str(e)[:150]}")
                    except Exception:
                        pass
                try:
                    # CRITICAL: last_error stores the open-positions JSON.
                    # A transient error must NEVER wipe it — that would orphan
                    # real open positions. Only write the error if the blob
                    # is already empty.
                    cur = get_state(pair)
                    if not parse_positions(cur):
                        sync(pair, state_update={"last_error": str(e)[:200]})
                    else:
                        log(f"[{pair}] positions intact — error logged to Actions log only")
                except Exception as e2:
                    log(f"[{pair}] ERROR updating state: {e2}")

        elapsed = time.time() - start
        if elapsed < MAX_RUNTIME:
            time.sleep(max(1, POLL_INTERVAL - elapsed % POLL_INTERVAL))

    log(f"Run complete: {ticks} ticks, {trades} trades, {time.time()-start:.0f}s")


if __name__ == "__main__":
    main()
