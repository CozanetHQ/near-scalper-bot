#!/usr/bin/env python3
"""
Sovereign v6 Entry Engine — CozySovereignAI top-down restructuring (owner spec 2026-09-15).
Update 2026-09-15 (owner): EXECUTION TIMELINE SWITCHED TO 2m — entry fills, exits and
holds are walked on completed 2m candles (gates unchanged: 4H bias / 1H FVG / 15m sweep /
2m CHOCH / live-only L2).

Modes:
  EXEC_TF_MIN = 2        execution timeframe (2m per owner)
  ENTRY_MODE  = taker    market entry at next 2m open (spec baseline)
               maker     POST_ONLY retest limit at the CHOCH level (EV lever #1):
                        placed after the CHOCH close, waits for pullback to the
                        level, fills as maker (0.02%, no slip). POST_ONLY semantics:
                        rests passively; never crosses. Expires after SWEEP_VALID_H.
  SYMBOL      = near|btc dataset prefix in lab/data_sovereign/

Friction model (Bitget USDT perp):
  taker 0.06%, maker 0.02%, assumed slippage 0.03% per market leg.
  Taker entry: C_win = 0.11% (taker+slip entry, maker TP), C_loss = 0.18%.
  Maker entry: C_win = 0.04% (maker entry, maker TP), C_loss = 0.11%.
  Spec's "reclaims 0.06%" for the maker TP leg is exactly 0.04% (0.06->0.02).

No-lookahead: every gate evaluated on the last COMPLETED candle of its TF at each
2m step; entries fill at the next 2m open (taker) or on a pullback touch (maker).
If TP and SL both fall inside one 2m candle, SL is assumed first (conservative).
"""
import json
import os
import bisect

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_sovereign")

# ── Locked parameters (spec + owner updates) ────────────────────────────────
EMA_FAST, EMA_SLOW = 50, 200
FRACTAL_K = 2
ATR_PERIOD = 14
RR = 2.0
RISK_FRAC = 0.015
LEV_CAP = 10
MIN_NOTIONAL = 5.0
SWEEP_VALID_H = 3.0
READY_TIMEOUT_H = 3.0
L2_IMBALANCE_MIN = 1.5          # live-only (stubbed True in lab)
START_EQUITY = 10.0

EXEC_TF_MIN = int(os.environ.get("EXEC_TF_MIN", "2"))     # owner 2026-09-15: 2m execution
ENTRY_MODE = os.environ.get("ENTRY_MODE", "taker")        # taker | maker
EXIT_ORDER = os.environ.get("EXIT_ORDER", "sl_first")      # sl_first (conservative) | tp_first (optimistic bound)
SYMBOL = os.environ.get("SYMBOL", "near")                 # near | btc
DATA_SUFFIX = os.environ.get("DATA_SUFFIX", "")           # e.g. _bear (out-of-sample window)

TAKER = 0.0006
MAKER = 0.0002
SLIP = 0.0003

# ── Data loading / resampling ───────────────────────────────────────────────
def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)

def resample(c1m, minutes):
    ms = minutes * 60_000
    out = []
    for c in c1m:
        b = c["ts"] // ms * ms
        if not out or out[-1]["ts"] != b:
            out.append({"ts": b, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "vol": c["vol"]})
        else:
            o = out[-1]
            o["high"] = max(o["high"], c["high"])
            o["low"] = min(o["low"], c["low"])
            o["close"] = c["close"]
            o["vol"] += c["vol"]
    return out

# ── Indicators ──────────────────────────────────────────────────────────────
def ema_series(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    out = [None] * len(closes)
    out[period - 1] = e
    for i in range(period, len(closes)):
        e = closes[i] * k + e * (1 - k)
        out[i] = e
    return out

def atr_series(candles, period):
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    a = sum(trs[1 : period + 1]) / period
    out[period] = a
    for i in range(period + 1, len(candles)):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out

def fractal_lows(candles, k=2):
    out = []
    n = len(candles)
    for i in range(k, n - k):
        lo = candles[i]["low"]
        if all(candles[j]["low"] >= lo for j in range(i - k, i + k + 1) if j != i):
            out.append({"idx": i, "ts": candles[i]["ts"], "price": lo, "confirmed_ts": candles[i + k]["ts"]})
    return out

def fractal_highs(candles, k=2):
    out = []
    n = len(candles)
    for i in range(k, n - k):
        hi = candles[i]["high"]
        if all(candles[j]["high"] <= hi for j in range(i - k, i + k + 1) if j != i):
            out.append({"idx": i, "ts": candles[i]["ts"], "price": hi, "confirmed_ts": candles[i + k]["ts"]})
    return out

def fvg_zones(c1h):
    bull, bear = [], []
    for i in range(2, len(c1h)):
        a, c = c1h[i - 2], c1h[i]
        if a["high"] < c["low"]:
            bull.append({"created_ts": c["ts"], "low": a["high"], "high": c["low"], "dead_after": None})
        if a["low"] > c["high"]:
            bear.append({"created_ts": c["ts"], "low": c["high"], "high": a["low"], "dead_after": None})
    for z in bull:
        for h in c1h:
            if h["ts"] > z["created_ts"] and h["close"] < z["low"]:
                z["dead_after"] = h["ts"]
                break
    for z in bear:
        for h in c1h:
            if h["ts"] > z["created_ts"] and h["close"] > z["high"]:
                z["dead_after"] = h["ts"]
                break
    return bull, bear

def price_in_zone(zones, ts, price):
    return any(z["created_ts"] <= ts and (z["dead_after"] is None or z["dead_after"] > ts)
               and z["low"] <= price <= z["high"] for z in zones)

# ── Engine ───────────────────────────────────────────────────────────────────
def run():
    c4h = load(f"{SYMBOL}{DATA_SUFFIX}_4h.json")
    c1h_native = load(f"{SYMBOL}{DATA_SUFFIX}_1h.json")
    c1m = load(f"{SYMBOL}{DATA_SUFFIX}_1m.json")

    closes4 = [c["close"] for c in c4h]
    ema50_4h = ema_series(closes4, EMA_FAST)
    ema200_4h = ema_series(closes4, EMA_SLOW)

    # execution timeline = 2m (owner). Gates on 15m still from 1m-resample for
    # fractal independence; the exec walk itself uses the 2m series.
    exec_c = resample(c1m, EXEC_TF_MIN)
    tf_exec_ms = EXEC_TF_MIN * 60_000

    c15m = resample(c1m, 15)
    atr2m = atr_series(exec_c, ATR_PERIOD)   # ATR14 on the EXECUTION timeframe

    bull_zones, bear_zones = fvg_zones(c1h_native)
    swing_lows15 = fractal_lows(c15m, FRACTAL_K)
    swing_highs15 = fractal_highs(c15m, FRACTAL_K)
    fhighs_x = fractal_highs(exec_c, FRACTAL_K)
    flows_x = fractal_lows(exec_c, FRACTAL_K)

    idx4_close = [c["ts"] + 4 * 3600_000 for c in c4h]
    idx15_close = [c["ts"] + 15 * 60_000 for c in c15m]
    idxX_close = [c["ts"] + tf_exec_ms for c in exec_c]

    def last_idx(closes_at_list, ts):
        i = bisect.bisect_right(closes_at_list, ts) - 1
        return i if i >= 0 else None

    equity = START_EQUITY
    trades = []
    position = None
    pending = None  # POST_ONLY retest limit (maker mode)

    state = {"phase": "FLAT", "dir": None, "armed_ts": None, "sweep_ts": None,
             "swept_low": None, "swept_high": None, "choch_level": None}

    def open_position(side, entry_px, qty, i_atr, opened_ts):
        atr = atr2m[i_atr]
        dp = atr
        if side == "long":
            sl, tp = entry_px - dp, entry_px + RR * dp
        else:
            sl, tp = entry_px + dp, entry_px - RR * dp
        return {"side": side, "entry": entry_px, "qty": qty, "sl": sl, "tp": tp,
                "atr_frac": atr / entry_px, "opened_ts": opened_ts,
                "entry_fee_mode": "maker" if ENTRY_MODE == "maker" else "taker"}

    for n in range(len(exec_c) - 1):
        c = exec_c[n]
        nxt = exec_c[n + 1]
        ts = c["ts"] + tf_exec_ms          # close time of the just-completed exec candle

        # ── open-position exit management (walked on exec candles) ──
        if position:
            p, side = position, position["side"]
            hit_sl = c["low"] <= p["sl"] if side == "long" else c["high"] >= p["sl"]
            hit_tp = c["high"] >= p["tp"] if side == "long" else c["low"] <= p["tp"]
            exit_px = reason = None
            if hit_sl and (EXIT_ORDER == "sl_first" or not hit_tp):
                exit_px, reason = p["sl"], "SL"
            elif hit_tp:
                exit_px, reason = p["tp"], "TP_MAKER"
            if exit_px:
                gross = (exit_px - p["entry"]) * p["qty"] * (1 if side == "long" else -1)
                entry_fee = (p["entry"] * p["qty"] * MAKER if p["entry_fee_mode"] == "maker"
                             else p["entry"] * p["qty"] * (TAKER + SLIP))
                exit_fee = (exit_px * p["qty"] * MAKER if reason == "TP_MAKER"
                            else exit_px * p["qty"] * (TAKER + SLIP))
                fee = entry_fee + exit_fee
                net = gross - fee
                equity += net
                trades.append({
                    "side": side, "entry": p["entry"], "exit": exit_px,
                    "sl": p["sl"], "tp": p["tp"], "atr_frac": p["atr_frac"],
                    "qty": p["qty"], "notional": p["qty"] * p["entry"],
                    "entry_mode": p["entry_fee_mode"],
                    "gross": round(gross, 6), "fees": round(fee, 6), "net": round(net, 6),
                    "reason": reason, "opened_ts": p["opened_ts"], "closed_ts": ts,
                    "held_min": round((ts - p["opened_ts"]) / 60000, 1),
                    "equity": round(equity, 6),
                })
                position = None
                state = {"phase": "FLAT", "dir": None}
            continue

        # ── pending POST_ONLY retest limit (maker mode) ──
        if pending:
            if ts > pending["expires_ts"]:
                pending = None
                state = {"phase": "FLAT", "dir": None}
            else:
                side, lvl = pending["side"], pending["level"]
                filled = c["low"] <= lvl if side == "long" else c["high"] >= lvl
                if filled:
                    entry_px = lvl
                    i_atr = pending["i_atr"]
                    atr = atr2m[i_atr] or atr2m[max(0, i_atr)]
                    dp = atr
                    qty = (RISK_FRAC * equity) / dp
                    if qty * entry_px >= MIN_NOTIONAL:
                        position = open_position(side, entry_px, qty, i_atr, c["ts"])
                        if qty * entry_px > LEV_CAP * equity:
                            qty = LEV_CAP * equity / entry_px
                            position = open_position(side, entry_px, qty, i_atr, c["ts"])
                        # conservative: if the fill candle already breached the SL, die now
                        p = position
                        if (side == "long" and c["low"] <= p["sl"]) or (side == "short" and c["high"] >= p["sl"]):
                            gross = (p["sl"] - p["entry"]) * p["qty"] * (1 if side == "long" else -1)
                            fee = p["entry"] * p["qty"] * MAKER + p["sl"] * p["qty"] * (TAKER + SLIP)
                            net = gross - fee
                            equity += net
                            trades.append({"side": side, "entry": p["entry"], "exit": p["sl"],
                                           "sl": p["sl"], "tp": p["tp"], "atr_frac": p["atr_frac"],
                                           "qty": p["qty"], "notional": p["qty"] * p["entry"],
                                           "entry_mode": "maker", "gross": round(gross, 6),
                                           "fees": round(fee, 6), "net": round(net, 6),
                                           "reason": "SL", "opened_ts": c["ts"], "closed_ts": ts,
                                           "held_min": 0.0, "equity": round(equity, 6)})
                            position = None
                    pending = None
                    state = {"phase": "FLAT", "dir": None}
                continue

        # ── gate 1: 4H macro bias (last completed 4H candle) ──
        i4 = last_idx(idx4_close, ts)
        if i4 is None or i4 < EMA_SLOW or ema200_4h[i4] is None:
            continue
        bias = None
        if c4h[i4]["close"] > ema50_4h[i4] > ema200_4h[i4]:
            bias = "bull"
        elif c4h[i4]["close"] < ema50_4h[i4] < ema200_4h[i4]:
            bias = "bear"
        if bias is None:
            state = {"phase": "FLAT", "dir": None}
            continue
        d = bias

        # ── gate 2: 1H FVG zone — price trading inside active zone ──
        in_zone = price_in_zone(bull_zones if d == "bull" else bear_zones, ts, c["close"])
        if state["phase"] == "FLAT":
            if in_zone:
                state = {"phase": "ARMED", "dir": d, "armed_ts": ts}
            else:
                continue
        elif state["phase"] == "ARMED" and not in_zone and ts - state["armed_ts"] > READY_TIMEOUT_H * 3600_000:
            state = {"phase": "FLAT", "dir": None}
            continue

        # ── gate 3: 15m liquidity sweep (last completed 15m candle) ──
        i15 = last_idx(idx15_close, ts)
        if i15 is not None and i15 >= FRACTAL_K:
            c15 = c15m[i15]
            if d == "bull":
                sl15 = [s for s in swing_lows15 if s["confirmed_ts"] <= c15["ts"]]
                if sl15:
                    ref = sl15[-1]["price"]
                    if c15["low"] < ref and c15["close"] > ref:
                        state.update({"phase": "READY", "dir": d, "sweep_ts": ts, "swept_low": ref})
                    elif state.get("swept_low") is not None and c15["close"] < state["swept_low"]:
                        state.update({"phase": "ARMED", "swept_low": None})
            else:
                sh15 = [s for s in swing_highs15 if s["confirmed_ts"] <= c15["ts"]]
                if sh15:
                    ref = sh15[-1]["price"]
                    if c15["high"] > ref and c15["close"] < ref:
                        state.update({"phase": "READY", "dir": d, "sweep_ts": ts, "swept_high": ref})
                    elif state.get("swept_high") is not None and c15["close"] > state["swept_high"]:
                        state.update({"phase": "ARMED", "swept_high": None})

        if state["phase"] != "READY":
            continue
        if state.get("sweep_ts") and ts - state["sweep_ts"] > SWEEP_VALID_H * 3600_000:
            state.update({"phase": "ARMED", "sweep_ts": None, "swept_low": None, "swept_high": None})
            continue

        # ── gate 4: 2m micro-CHOCH — close beyond the post-sweep pullback fractal ──
        i2 = last_idx(idxX_close, ts)   # == n (the just-completed exec candle)
        if i2 is None or i2 < ATR_PERIOD or atr2m[i2] is None:
            continue
        sweep_ts = state.get("sweep_ts") or 0
        trig = False
        level = None
        if d == "bull":
            fh = [f for f in fhighs_x if f["confirmed_ts"] <= ts and f["ts"] > sweep_ts]
            if fh and c["close"] > fh[-1]["price"]:
                trig, level = True, fh[-1]["price"]
        else:
            fl = [f for f in flows_x if f["confirmed_ts"] <= ts and f["ts"] > sweep_ts]
            if fl and c["close"] < fl[-1]["price"]:
                trig, level = True, fl[-1]["price"]
        if not trig:
            continue

        # ── L2 imbalance gate: live-only (websocket top-10 depth, I_L2 >= 1.5).
        #    Not replayable from candles — stubbed True in the lab. ──

        # ── execute ──
        if ENTRY_MODE == "maker":
            # POST_ONLY retest limit at the CHOCH level: rests passively, waits
            # for the pullback; fills maker (0.02%, no slip). Expires in 3h.
            pending = {"side": "long" if d == "bull" else "short", "level": level,
                       "i_atr": i2, "placed_ts": ts,
                       "expires_ts": ts + SWEEP_VALID_H * 3600_000}
            state = {"phase": "FLAT", "dir": None, "armed_ts": None,
                     "sweep_ts": None, "swept_low": None, "swept_high": None}
        else:
            # taker: market entry at next exec-candle open
            entry = nxt["open"]
            atr = atr2m[i2]
            dp = atr
            qty = (RISK_FRAC * equity) / dp
            notional = qty * entry
            if notional < MIN_NOTIONAL:
                continue
            if notional > LEV_CAP * equity:
                qty = LEV_CAP * equity / entry
            position = open_position("long" if d == "bull" else "short", entry, qty, i2, nxt["ts"])
            state = {"phase": "FLAT", "dir": None, "armed_ts": None,
                     "sweep_ts": None, "swept_low": None, "swept_high": None}

    return trades, equity

# ── Report ─────────────────────────────────────────────────────────────────
def report(trades, equity):
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    tot = sum(t["net"] for t in trades)
    aw = sum(t["net"] for t in wins) / len(wins) if wins else 0
    al = sum(t["net"] for t in losses) / len(losses) if losses else 0
    wr = len(wins) / n if n else 0
    print(f"[{SYMBOL} exec={EXEC_TF_MIN}m entry={ENTRY_MODE}]")
    print(f"trades={n}  wins={len(wins)}  losses={len(losses)}  win_rate={wr*100:.1f}%")
    print(f"equity: {START_EQUITY:.2f} -> {equity:.2f}  (net {tot:+.4f})")
    print(f"avg_win={aw:+.4f}  avg_loss={al:+.4f}  payoff={abs(aw/al) if al else float('inf'):.2f}:1")
    if n:
        ev = wr * aw + (1 - wr) * al
        print(f"expectancy per trade: {ev:+.4f}  ({ev/START_EQUITY*100:+.3f}% of equity)")
        fees_tot = sum(t["fees"] for t in trades)
        print(f"total fees: {fees_tot:.4f}  ({fees_tot/abs(tot)*100 if tot else 0:.0f}% of |net|)")
    from collections import Counter
    print("exits:", dict(Counter(t["reason"] for t in trades)))
    fr = sorted(t["atr_frac"] for t in trades)
    if fr:
        med = fr[len(fr)//2]
        print(f"SL distance (ATR14 {EXEC_TF_MIN}m): median {med*100:.3f}%  min {fr[0]*100:.3f}%  max {fr[-1]*100:.3f}%")
        # breakeven with the active entry mode's friction
        if ENTRY_MODE == "maker":
            c_win, c_loss = 0.0002 + 0.0002, 0.0002 + 0.0006 + 0.0003
        else:
            c_win, c_loss = 0.0006 + 0.0003 + 0.0002, 0.0006 + 0.0003 + 0.0006 + 0.0003
        # exact: W*(2d - c_win) = (1-W)(d + c_loss)  ->  W* = (d + c_loss)/(3d + c_loss - c_win)
        w_star = (med + c_loss) / (3 * med + c_loss - c_win)
        print(f"breakeven win rate at median dP: {w_star*100:.1f}%  (realized {wr*100:.1f}%)")
    hd = sorted(t["held_min"] for t in trades)
    if hd:
        print(f"hold minutes: median {hd[len(hd)//2]:.0f}  max {hd[-1]:.0f}")
    for side in ("long", "short"):
        ts_ = [t for t in trades if t["side"] == side]
        if ts_:
            s = sum(t["net"] for t in ts_)
            w = sum(1 for t in ts_ if t["net"] > 0)
            print(f"{side:<5}: {len(ts_)} trades, {w} wins, net {s:+.4f}")

if __name__ == "__main__":
    trades, eq = run()
    report(trades, eq)
    suffix = f"{SYMBOL}{DATA_SUFFIX}_{EXEC_TF_MIN}m_{ENTRY_MODE}"
    with open(os.path.join(HERE, f"sovereign_v6_trades_{suffix}.json"), "w") as f:
        json.dump(trades, f, indent=1)
    print(f"\ntrade log -> lab/sovereign_v6_trades_{suffix}.json")
