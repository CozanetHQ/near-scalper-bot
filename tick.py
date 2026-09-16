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

# ── SOVEREIGN V6 (owner 2026-09-15): top-down MTF entry engine, ported from the
# lab (sovereign-v6-lab branch). Off unless BOTH env flags are set. When active
# for a pair, engine.sovereign.process_pair replaces this v5 tick entirely for
# that pair; every other pair keeps running v5 untouched.
SOVEREIGN_V6 = os.environ.get("SOVEREIGN_V6", "0") == "1"
SOVEREIGN_PAIRS = {p.strip().upper() for p in os.environ.get("SOVEREIGN_PAIRS", "").split(",") if p.strip()}
# LIVE trading (owner 2026-09-15): only sovereign pairs in LIVE_PAIRS trade real
# orders; everything else stays paper. Requires Bitget API keys in env.
LIVE_TRADING = os.environ.get("LIVE_TRADING", "0") == "1"
LIVE_PAIRS = {p.strip().upper() for p in os.environ.get("LIVE_PAIRS", "").split(",") if p.strip()}
_REPO = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(_REPO, "state", "state.json"))
TRADES_FILE = os.environ.get("TRADES_FILE", os.path.join(_REPO, "data", "trades.jsonl"))
ATR_PERIOD = 14
SL_ATR_MULT = 4.0     # GRID SEARCH WINNER (1152 configs): wide stop, rarely hit
TP_SL_RATIO = 1.5     # most exits are 20-min drift-capture time stops
WIN_TARGET_DOLLARS = 0.15  # OWNER 09-15: flat $0.15 per-slot win target. Set WIN_TARGET_PCT>0 to
                           # switch to percentage-compounding (1.67% of balance: $0.05 at $3, $0.15 at $9)
WIN_TARGET_PCT = float(os.environ.get("WIN_TARGET_PCT", "0"))  # 0 = flat WIN_TARGET_DOLLARS (owner 09-15)
SLIP_PCT = float(os.environ.get("SLIP_PCT", "0"))  # owner 09-08 stress lab: extra per-side execution cost (slippage/spread/failed fills)
TRANSITION_GATE = os.environ.get("TRANSITION_GATE", "0")  # owner 09-08: yellow-state — closed 4H vs forming 4H+15m disagree = pause OLD-direction entries  # OWNER 09-08: speak percentages — target = 1.67% of balance (=$0.05 at $3), auto-compounds with the account; 0 = fixed dollars
LIQ_MODEL = os.environ.get("LIQ_MODEL", "0")
TREND_MULT = float(os.environ.get("TREND_MULT", "1.0"))
TREND_CHASE = os.environ.get("TREND_CHASE", "0")  # OWNER 09-08 hypothesis: during clear 4H+15m bias, ALSO enter WITH the trend (momentum candle, no pullback)  # OWNER 09-08: multiply win target when trade aligns with clear 4H+15m bias  # 1 = model EXCHANGE LIQUIDATION (lab only):
# a position whose adverse move crosses 1/leverage is force-closed at the liq
# price for a realized loss of its margin. Paper default OFF (sim floats wedges
# forever); ON it exposes the true tail of high-leverage configs.
SCALP_TP_ATR = 1.6  # OWNER 09-08: two-week lab verdict — 1.6x ATR is the robust center (capital-time 0.0070 $/(cap.h) IDENTICAL on both regime weeks; hostile-week maxDD halved -$0.76 vs -$5.20 at 1.2x; 2.0x hits the recycle cliff)   # TP distance = 1.2x 1m ATR (adaptive to live volatility)
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "30"))  # V7-A 2026-09-14: ABSOLUTE sanity ceiling only. Real slot count is dynamic: N_SLOTS = floor(balance / SLOT_MARGIN_USD) (V7-C owner model: $10 -> 3 slots). Registry-locked.
CONCENTRATED = os.environ.get("CONCENTRATED", "0")  # OWNER 09-11: 0 — slot sizing (each position ~1/8 of budget, losses sliced small). 1 = whole-account deployment. Registry-locked.
# lab confirmed 4 slots strictly better: realized +3.84 vs +3.58, equity +0.51 vs -0.30, half the wedges    # hedge scalper: multiple concurrent positions — wedged trades don't stop the chopping
MARGIN_BUDGET = float(os.environ.get("MARGIN_BUDGET", "0.99"))  # V7-A 2026-09-14: owner model — every slot costs SLOT_COST_MARGIN ($3.30) of balance; 99% commitment, 1% float for fees. Supersedes 0.85.
# Lab (same fresh week, 4 slots): 0.80 → realized +63% but equity -$2.94 (wedge
# cluster ate the grind). 0.40 → realized +35% and equity +$0.19 — the WORST
# observed week still ends green. Halves wins, halves wedge damage.  # total margin across all open positions <= 80% of balance

# ── V7-A (owner 2026-09-14): fixed clips + balance-scaled slots ──────────────
# Owner model (research/v7_proposal_2026-09-14.md §9): every slot trades
# ~$3.30 of margin ($33 notional at 10x) and hunts 10–15 cents (a 0.42–0.57%
# TP — the engine's TP geometry already delivers this on full exits).
# Slot count = floor(balance / $3.30): $10 → 3 slots; every +$3.3 of
# balance earns one more slot; balance falling shrinks slots automatically.
FIXED_NOTIONAL_USD = float(os.environ.get("FIXED_NOTIONAL_USD", "33.0"))
SLOT_COST_MARGIN = float(os.environ.get("SLOT_COST_MARGIN", "3.30"))
N_SLOTS_MIN = int(os.environ.get("N_SLOTS_MIN", "1"))
# ── V7-C (owner 2026-09-16): $3 margin seats × ADJUSTABLE leverage ──────────
# Owner directive: "hunt 0.15 per trade entering with $3 adjustable
# leverage, mathematically aware of price movements." Every slot occupies the
# same $3.00 of balance; the pair's live leverage tier (position_leverage —
# conclusive 14d evidence only) now COMMANDS the notional: 5x→$15, 10x→$30,
# 15x→$45, 20x→$60. Proven pairs scale exposure on the same $3 seat;
# re-tiered-down pairs shrink automatically. TP stays a flat $0.15 HUNT net
# of fees, so a 20x clip needs only ~0.37% of price vs ~0.62% at 10x (ledger
# evidence 2026-09-16, n=157: median BE_STOP trade runs 67% of the TP
# distance then reverses — closer targets convert those half-runners).
# The ATR floor (SCALP_TP_ATR × 1m ATR) still binds in fast tape, and
# HARD_SL_BY_LEV tightens with the tier (20x → 3% price = 60% of slot
# margin), so added notional never buys added tail risk.
SLOT_MARGIN_USD = float(os.environ.get("SLOT_MARGIN_USD", "3.0"))
# Correlated-majors cluster: BTC/ETH/SOL move together (~0.8–0.9). Three
# aligned $33 clips in one −6% candle = −59% of a $10 account. Cap same-
# direction cluster exposure at 2 clips (proposal §2 risk math).
CORR_DIR_CAP = int(os.environ.get("CORR_DIR_CAP", "2"))
CORR_CLUSTER = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
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
MIN_TP_DIST_FRAC = float(os.environ.get("MIN_TP_DIST_FRAC", "0.0025"))  # V7-C RE-LOCK 2026-09-16 (owner hunt-$0.15 directive, BEFORE any results observed under it): the 0.6% deadlock-release floor vetoed the dollar hunt — at 15-20x tiers the $0.15 target is 0.25-0.33% + fees. New floor 0.25% = the dollar target at the 20x tier ($60 clip); anything closer is fee-starved. The ATR floor (1.6x 1m ATR) still binds in fast tape. Old 0.6% was an owner release after the 2026-09-11 zero-trade deadlock.  # TP floor as a FRACTION of price (~0.13%): below this, fees eat the scalp alive. Was an absolute $0.003 in the single-pair NEAR era ($2.4 price); multi-pair demands price-proportionate ($0.003/$2.42 ≈ 0.125%). Registry-locked.
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
# ── HYBRID LEVERAGE SYSTEM (owner design, authorized 2026-09-13) ──────────
# Per-position leverage: zone table (eight 3h UTC buckets) > per-pair registry
# override > global. Tiers 5/10/15/20x. Promotion needs PROOF (bootstrap CI>0
# on trailing data, see compute_leverage_zones); a pair proven bad in ALL
# zones drops to 5x everywhere (owner rule 1). HARD_SL scales with leverage so
# the exchange can never liquidate before our own stop (0.63x-liq rule, the
# 2026-09-10 lock rationale generalized).
HARD_SL_BY_LEV = {5: 0.12, 10: 0.06, 15: 0.04, 20: 0.03}
# ── PROFIT PROTECTION ENGINE (owner spec 2026-09-13; full 27-config sweep
# study, audit §18). Once MFE >= PROTECT_ARM_FRAC of the TP distance the
# position enters TP_PROTECTION_ARMED; the exit floor locks at
# PROTECT_FLOOR_FRAC of the TP distance — a reversal exits there (PROTECTED)
# with a kept profit instead of scraping back to BE_STOP. Config 70/55 won
# the sweep (activation 30-70% x retracement 10-30%): PF 0.84->0.96,
# maxDD 40.7%->36.5%, BE_STOPs 607->328, 2/3 folds >= baseline. The owner's
# 40% hypothesis was REJECTED — early activation cuts eventual winners.
# IDEMPOTENT BY CONSTRUCTION: arming is a pure function of (MFE, TP distance)
# so repeated price updates cannot re-arm; the exit fires once and removes
# the position from the book, so it cannot re-trigger. The floor is always
# above the fee buffer -> protection can never lock a guaranteed loss after
# costs (verified: 0.55*tp >= 2*fee+2*slip+buffer for all live tp distances).
DYN_FLOOR = os.environ.get("DYN_FLOOR", "1")  # study-authorized: ON
PROTECT_ARM_FRAC = float(os.environ.get("PROTECT_ARM_FRAC", "0.70"))
PROTECT_FLOOR_FRAC = float(os.environ.get("PROTECT_FLOOR_FRAC", "0.55"))
START_BALANCE = 3.0
FEE_RATE = 0.0006
BE_BUFFER_FRAC = float(os.environ.get("BE_BUFFER_FRAC", "0.0027"))  # V7-B 2026-09-14: owner 0.2% fee allowance (RT) + 2x assumed slip (0.02%) + crumb (0.05%). BE_STOP exits lock ~0.14% of notional per clip — double the old crumb. Supersedes 0.0019.

# ── MAKER-EXIT FEE MODEL (owner paper authorization 2026-09-13, audit §22-23) ──
# TP and PROTECTED exits are calm resting-limit fills -> maker fee 0.02%/leg.
# BE_STOP / MAE_KILL / HARD_SL / SL / MAX_AGE / EOD are urgent risk exits ->
# taker 0.06%/leg (guaranteed fill). ENTRY leg stays taker market order (owner:
# enter that second; post_only entry fills measured to miss the immediate
# winners — adverse selection, lab §23). Paper-validated: blended model flips
# act70_r15 expectancy -0.0086% -> +0.0731%/trade (§22). MAKER_EXITS=0 restores
# pure taker everywhere.
MAKER_EXITS = os.environ.get("MAKER_EXITS", "1") == "1"
MAKER_FEE_RATE = 0.0002
MAKER_EXIT_REASONS = {"TP", "PROTECTED"}
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


# zone table loaded from the LOCKED registry (params.LEVERAGE_ZONES.value)
_LEVERAGE_ZONES = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "param_registry.json")) as _f:
        _LEVERAGE_ZONES = (json.load(_f).get("params", {}).get("LEVERAGE_ZONES", {}) or {}).get("value") or {}
except Exception:
    _LEVERAGE_ZONES = {}
_RUN_LEV_ZONES = {}   # rolling re-tier, recomputed fresh each run (no state storage)

def _boot_ci(vals, n_boot=2000, seed=17):
    """Tiny pure-python bootstrap 95% CI (no numpy in the live runner)."""
    import random as _random
    rng = _random.Random(seed)
    n = len(vals)
    if n < 5:
        return None
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _i in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot)]

def compute_leverage_zones():
    """Rolling re-tier (owner 2026-09-13): evidence from the trailing 14 days
    of closed trades, recomputed fresh each run (state storage is impossible —
    the sync whitelist drops unknown keys). Only CONCLUSIVE cells override the
    locked registry table: pair proven bad in ALL zones (pooled CI<0, n>=30)
    -> 5x everywhere; a zone with n>=25 and CI<0 -> 5x; CI>0 -> 15x (20x if
    mean >= +0.10%/trade). Cells without conclusive evidence keep the registry
    tier / per-pair override / global. Result capped 5..20x."""
    zones = {}
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - 14 * 86400
        per = {}
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trades.jsonl")) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    t = json.loads(line)
                    ca = (t.get("closed_at") or "").replace("Z", "+00:00")
                    if not ca or datetime.fromisoformat(ca).timestamp() < cutoff:
                        continue
                    r = (t.get("net_pnl") or 0.0) / max(t.get("balance_after") or 1.0, 1e-9)
                    per.setdefault(t.get("pair") or "", []).append((ca, r))
                except Exception:
                    continue
    except Exception:
        return zones
    for pair, rows in per.items():
        vals = [r for _, r in rows]
        ci = _boot_ci(vals) if len(vals) >= 30 else None
        pair_bad = bool(ci and ci[1] < 0)
        row_zones = []
        for b in range(8):
            tier = None
            if pair_bad:
                tier = 5
            else:
                bv = [r for ca, r in rows if datetime.fromisoformat(ca).hour // 3 == b]
                if len(bv) >= 25:
                    ci = _boot_ci(bv)
                    if ci:
                        if ci[1] < 0:
                            tier = 5
                        elif ci[0] > 0:
                            tier = 20 if (sum(bv) / len(bv)) >= 0.0010 else 15
            row_zones.append(tier)
        zones[pair] = row_zones
    return zones

def position_leverage(symbol, now_iso):
    """Per-position leverage: computed zone (conclusive cells only) > registry
    LEVERAGE_ZONES > per-pair LEVERAGE override > global. Capped 5..20x."""
    try:
        bucket = datetime.fromisoformat(now_iso).astimezone(timezone.utc).hour // 3
    except Exception:
        bucket = None
    for source in (_RUN_LEV_ZONES.get(symbol), _LEVERAGE_ZONES.get(symbol)):
        if isinstance(source, list) and len(source) == 8 and bucket is not None:
            v = source[bucket]
            if isinstance(v, int) and 5 <= v <= 20:
                return v
    try:
        lv = int(pair_param("LEVERAGE", symbol, LEVERAGE))
    except Exception:
        lv = LEVERAGE
    return max(5, min(20, lv))

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
    # V7-A correlation context: same-direction position counts in the
    # BTC/ETH/SOL cluster (for CORR_DIR_CAP gating).
    corr_long, corr_short = 0, 0
    for op, ops in (master.get("pairs") or {}).items():
        if op == pair or op not in CORR_CLUSTER:
            continue
        for p in parse_positions(ops, ops.get("pair") or SYMBOL):
            if p.get("side") == "long":
                corr_long += 1
            elif p.get("side") == "short":
                corr_short += 1
    state["_corr_long"] = corr_long
    state["_corr_short"] = corr_short
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
        try:
            # V7-A: dashboard shows the LIVE balance-scaled slot count
            acct["max_positions"] = max(N_SLOTS_MIN, min(int(float(acct.get("balance") or 0) // SLOT_MARGIN_USD), MAX_POSITIONS))
        except (TypeError, ValueError):
            acct["max_positions"] = MAX_POSITIONS
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
        try:  # UI trade store (owner spec 09-15): chart overlay history
            from engine import ui_store
            ui_store.record_trade(tr)
        except Exception:
            pass
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
    # §22 fee model: taker entry leg always; exit leg maker on TP/PROTECTED
    # (resting limit), taker on all urgent/risk exits.
    exit_fee_rate = MAKER_FEE_RATE if (MAKER_EXITS and reason in MAKER_EXIT_REASONS) else FEE_RATE
    fees = notional * (FEE_RATE + exit_fee_rate)
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
        "tp_price": tp_price,          # UI trade store (owner spec 09-15)
        "sl_price": pos.get("sl_price") or 0,
        "exit_price": exit_price,
        "notional": notional,
        "margin": pos["margin"],
        "leverage": pos.get("leverage") or LEVERAGE,
        "hard_sl": pos.get("hard_sl") or HARD_SL_FRAC,
        "gross_pnl": round(gross, 6),
        "fees": round(fees, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "balance_after": round(new_balance, 6),
        "aligned": pos.get("aligned", None),
        "advisory_score": pos.get("advisory_score"),
        "wall_ratio": pos.get("wall_ratio"),
        "regime_at_entry": pos.get("regime_at_entry"),
        "atr_frac_at_entry": pos.get("atr_frac_at_entry"),
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
            "PROTECT_ARM_FRAC": PROTECT_ARM_FRAC,
            "PROTECT_FLOOR_FRAC": PROTECT_FLOOR_FRAC,
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
            "FIXED_NOTIONAL_USD": FIXED_NOTIONAL_USD,
            "SLOT_COST_MARGIN": SLOT_COST_MARGIN,
            "N_SLOTS_MIN": N_SLOTS_MIN,
            "CORR_DIR_CAP": CORR_DIR_CAP,
            "MARGIN_BUDGET": MARGIN_BUDGET,
            "SLOT_MARGIN_USD": SLOT_MARGIN_USD,
            "WIN_TARGET_DOLLARS": WIN_TARGET_DOLLARS,
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
    if SOVEREIGN_V6 and symbol in SOVEREIGN_PAIRS:
        from engine.sovereign import process_pair
        return process_pair(state)
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
            liq_frac = (1.0 / float(pos.get("leverage") or LEVERAGE)) - 0.005  # maintenance buffer
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
        _hs = float(pos.get("hard_sl") or HARD_SL_FRAC)
        if _hs > 0 and hs_adverse >= _hs:
            trade, balance = close_position(pos, price, "HARD_SL", now, balance)
            closed_any.append(trade)
            _pt(
                f"\U0001F6D1 *HARD_SL — {pos['side'].upper()} force-closed*\n"
                f"Entry ${pos['entry_price']:.4f} → ${price:.4f} (adverse {hs_adverse*100:.1f}% >= locked {_hs*100:.0f}% @ {pos.get('leverage') or LEVERAGE}x)\n"
                f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
            )
            continue
        # ── P2 RISK ENGINE — MAE ceiling: beyond the empirical recovery band,
        # the ride stops paying. Caps tail loss at MAE_CEIL_FRAC instead of
        # letting doomed trades rot to the 6% HARD_SL (median 15.2h in the seed).
        _mae_ceil = pair_param("MAE_CEIL_FRAC", symbol, MAE_CEIL_FRAC)
        if _mae_ceil > 0 and (pos.get("mae_frac") or 0.0) >= _mae_ceil:
            trade, balance = close_position(pos, price, "MAE_KILL", now, balance)
            closed_any.append(trade)
            _pt(
                f"\U0001F6A8 *MAE_KILL — {pos['side'].upper()} exit at ceiling*\n"
                f"Adverse {(pos.get('mae_frac') or 0.0)*100:.1f}% >= locked {_mae_ceil*100:.1f}% — recovery odds gone\n"
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
            # ── PROFIT PROTECTION state machine (study §18, 70/55) ──
            # NORMAL -> TP_PROTECTION_ARMED (MFE >= 70% of TP dist)
            #       -> PROTECTED exit (retrace through the 55% floor) or FULL_TP.
            # Events: PROTECTION_ARMED logged once per run per position;
            # PROTECTION_TRIGGERED/PROTECTION_EXIT logged at close with the
            # full evidence trail the owner spec requires.
            if DYN_FLOOR == "1" and _tp_frac > 0 and _mfe >= PROTECT_ARM_FRAC * _tp_frac:
                # clamp to the cost floor (lab floor_frac_of): protection can
                # NEVER lock a guaranteed loss once fees+slip are paid — at very
                # tight TP distances the 55% floor sits below costs, so it rides
                # the cost buffer instead (same clamp the sweep was run with).
                _floor_frac = max(PROTECT_FLOOR_FRAC * _tp_frac,
                                  2 * FEE_RATE + 2 * SLIP_ASSUMED_PCT + 0.0005)
                if not pos.get("prot_armed"):
                    pos["prot_armed"] = True
                    log(
                        "PROTECTION_ARMED " + symbol + " " + pos["side"] +
                        " entry=" + str(pos["entry_price"]) + " price=" + str(price) +
                        " tp=" + str(tp) + " tp_progress=" + str(round(_mfe / _tp_frac * 100, 1)) + "%" +
                        " protection_level=" + str(round(_floor_frac * 100, 3)) + "%of_price" +
                        " mfe=" + str(round(_mfe, 6)) + " mae=" + str(round(pos.get("mae_frac") or 0.0, 6)) +
                        " ts=" + now)
                if is_long:
                    _floor = pos["entry_price"] * (1 + _floor_frac)
                    if price <= _floor:
                        trade, balance = close_position(pos, _floor, "PROTECTED", now, balance)
                        closed_any.append(trade)
                        log(
                            "PROTECTION_EXIT " + symbol + " " + pos["side"] +
                            " entry=" + str(pos["entry_price"]) + " exit=" + str(_floor) +
                            " tp=" + str(tp) + " tp_progress=" + str(round(_mfe / _tp_frac * 100, 1)) + "%" +
                            " protection_level=" + str(round(_floor_frac * 100, 3)) + "%" +
                            " mfe=" + str(round(_mfe, 6)) + " mae=" + str(round(pos.get("mae_frac") or 0.0, 6)) +
                            " realized=" + str(trade["net_pnl"]) +
                            " fees=" + str(trade["fees"]) + " ts=" + now)
                        _pt(
                            f"\U0001F9F1 *PROTECTED — {pos['side'].upper()} profit locked at the 55% floor*\n"
                            f"MFE {(_mfe/_tp_frac)*100:.0f}% of TP armed protection; reversal exit kept {PROTECT_FLOOR_FRAC*100:.0f}% of TP distance\n"
                            f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
                        )
                        continue
                else:
                    _floor = pos["entry_price"] * (1 - _floor_frac)
                    if price >= _floor:
                        trade, balance = close_position(pos, _floor, "PROTECTED", now, balance)
                        closed_any.append(trade)
                        log(
                            "PROTECTION_EXIT " + symbol + " " + pos["side"] +
                            " entry=" + str(pos["entry_price"]) + " exit=" + str(_floor) +
                            " tp=" + str(tp) + " tp_progress=" + str(round(_mfe / _tp_frac * 100, 1)) + "%" +
                            " protection_level=" + str(round(_floor_frac * 100, 3)) + "%" +
                            " mfe=" + str(round(_mfe, 6)) + " mae=" + str(round(pos.get("mae_frac") or 0.0, 6)) +
                            " realized=" + str(trade["net_pnl"]) +
                            " fees=" + str(trade["fees"]) + " ts=" + now)
                        _pt(
                            f"\U0001F9F1 *PROTECTED — {pos['side'].upper()} profit locked at the 55% floor*\n"
                            f"MFE {(_mfe/_tp_frac)*100:.0f}% of TP armed protection; reversal exit kept {PROTECT_FLOOR_FRAC*100:.0f}% of TP distance\n"
                            f"PnL ${trade['net_pnl']:.4f} | Balance ${balance:.4f}"
                        )
                        continue
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
        _slots_cap = pair_param("MAX_SLOTS_PER_PAIR", symbol, MAX_POSITIONS)
        # V7-C: GLOBAL slots are balance-scaled — floor(balance/$3.00), capped
        # at MAX_POSITIONS. $10 account -> 3 slots; growth buys slots.
        n_slots = max(N_SLOTS_MIN, min(int(balance // SLOT_MARGIN_USD), MAX_POSITIONS))
        # V7-A correlation cap: max CORR_DIR_CAP same-direction clips in the
        # correlated BTC/ETH/SOL cluster (aligned $33 clips share one tail).
        if want is not None and symbol in CORR_CLUSTER and CORR_DIR_CAP > 0:
            _other_dir = (state.get("_corr_long") or 0) if want == "long" else (state.get("_corr_short") or 0)
            _here_dir = sum(1 for p in still_open if p.get("side") == want)
            if _other_dir + _here_dir >= CORR_DIR_CAP:
                want = None
                wait_reason = (f"corr cap: {_other_dir + _here_dir} same-direction cluster clips "
                               f"(cap {CORR_DIR_CAP}) — BTC/ETH/SOL share one tail")
        can_open = (
            want is not None
            and entry_cooldown_ok
            and len(still_open) < _slots_cap
            and len(still_open) + int(state.get("_other_open") or 0) < n_slots
            and not daily_risk_off
        )
        if can_open:
            used_margin = sum(p["margin"] for p in still_open) + float(state.get("_other_margin") or 0)
            margin_left = balance * MARGIN_BUDGET - used_margin
            # ── OWNER 2026-09-15: WIN TARGET WIRED (was defined 09-07, never read —
            # wins were landing ~$0.05 instead of the target). The entry TP must
            # NET the target: WIN_TARGET_PCT x balance (auto-compounds: $0.05 at
            # $3, ~$0.13 at $8, $0.15 at $9); WIN_TARGET_PCT=0 -> fixed
            # WIN_TARGET_DOLLARS. Only the normal $33 clip chases the target;
            # safe-mode recovery clips keep the adaptive ATR TP (a $1 clip would
            # need a 13% move — pointless). ATR/TP floors still bind below it.
            # V7-C: the clip is the $3 seat × the pair's leverage tier, computed
            # BEFORE the TP math so the $0.15 hunt adapts to the commanded
            # notional (bigger clip → closer price target).
            _lev = position_leverage(symbol, now)
            notional = SLOT_MARGIN_USD * _lev
            if safe_on:
                notional = max(notional * RECOVERY_SIZE_FRAC, 1.0)
            target_usd = WIN_TARGET_PCT * balance if WIN_TARGET_PCT > 0 else WIN_TARGET_DOLLARS
            target_frac = (target_usd / notional) + FEE_RATE * 2 if not safe_on else 0.0
            tp_dist = max(SCALP_TP_ATR * (atr or 0.003 * price), MIN_TP_DIST_FRAC * price,
                          target_frac * price)
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
            if per_unit > 0 and margin_left > 0.05 and ev_frac >= EV_MARGIN_REQ:
                # ── V7-C (owner 2026-09-16): the clip is the $3 margin seat ×
                # the pair's leverage tier, computed above before the TP math:
                # 5x → $15, 10x → $30, 15x → $45, 20x → $60. Proven pairs
                # COMMAND more notional on the same $3 seat; re-tiered-down
                # pairs shrink automatically. HARD_SL_BY_LEV tightens with the
                # tier, so added notional never buys added tail risk. Below the
                # SAFE_DD wall the clip shrinks to RECOVERY_SIZE_FRAC (dust
                # floor $1) so the account keeps trading at quarter size.
                # Full clip or nothing: if the remaining margin cannot seat the
                # clip, WAIT for a slot to close — partial clips break the
                # owner's per-slot economics ($3 in, $0.15 out).
                if notional > margin_left * _lev:
                    notional = 0.0
                aligned = (bias != "none" and want == ("long" if bias == "bull" else "short"))
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
                    margin = notional / _lev
                    entry = price
                    tp = entry + entry_tp_dist if want == "long" else entry - entry_tp_dist
                    still_open.append({
                        "side": want, "entry_price": entry, "tp_price": tp,
                        "notional": notional, "margin": margin, "opened_at": now,
                        "leverage": _lev, "hard_sl": HARD_SL_BY_LEV.get(_lev, HARD_SL_FRAC),
                        # Position telemetry (owner spec 2026-09-11: the empirical
                        # MFE/MAE/duration record every management decision reads).
                        "mfe_frac": 0.0, "mae_frac": 0.0,
                        "aligned": aligned, "pair": symbol,
                        "advisory_score": (advisory or {}).get("overall_score"),
                        "wall_ratio": (advisory or {}).get("wall_ratio"),
                        # Regime study (owner 2026-09-12): record the classifier's
                        # label + ATR context at ENTRY so per-regime outcome studies
                        # (chop entry gate) have clean data. rg in {"chop","runaway"}.
                        "regime_at_entry": regime,
                        "atr_frac_at_entry": (round(atr / price, 6) if atr else None),
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
               else f"slots/margin full ({len(still_open) + int(state.get('_other_open') or 0)}/{n_slots} V7 slots)")
        return "none", f"signal {want} skipped — {why}"
    if wait_reason:
        return "none", f"WAIT ({regime}) — {wait_reason} | {len(still_open)} open"
    return "none", f"regime:{regime} | {len(still_open)} open | price ${price:.4f}"


def main():
    global _RUN_LEV_ZONES
    _RUN_LEV_ZONES = compute_leverage_zones()
    if _RUN_LEV_ZONES:
        log("hybrid leverage zones (trailing 14d evidence): " + json.dumps(_RUN_LEV_ZONES))
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

    try:  # UI export (owner spec 09-15): trades_ui.json for the chart frontend
        from engine import ui_store
        ui_store.export_ui_json()
    except Exception:
        pass
    log(f"Run complete: {ticks} ticks, {trades} trades, {time.time()-start:.0f}s")


if __name__ == "__main__":
    main()
