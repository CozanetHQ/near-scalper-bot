#!/usr/bin/env python3
"""
Sovereign v6 Entry Engine — CozySovereignAI top-down restructuring (owner spec 2026-09-15).

Structure (a priori, locked — NOT tuned on results):
  4H  : Directional bias  — Close > EMA50 > EMA200 (long) / Close < EMA50 < EMA200 (short)
  1H  : Point of interest  — active Fair Value Gap zone, price trading within it
  15m : Liquidity sweep    — wick below fractal swing low, close back above (long) [mirrored short]
  2m  : Micro-CHOCH        — close above most recent confirmed 2m lower high
       + L2 order-book imbalance gate (I_L2 >= 1.5) — LIVE-ONLY, cannot be replayed from
         candles; implemented as an engine hook, stubbed True in the lab (documented).
  Risk: SL = 1 x ATR14(2m), TP = 2 x SL (1:2 RR), 1.5% fixed capital risk,
        TP exit POST_ONLY (maker), entry + SL exit taker.

Friction model (Bitget USDT perp):
  taker 0.06%, maker 0.02%, assumed slippage 0.03% per market leg.
  Hybrid C_win  = 0.06 + 0.03 + 0.02           = 0.11%   (taker entry + slip + maker TP)
  All-taker     = 0.06 x 2 + 0.03              = 0.15%   (spec baseline)
  C_loss        = 0.06 + 0.03 + 0.06 + 0.03    = 0.18%   (taker entry + slip + taker SL + slip)
  Note: spec text says the maker exit "reclaims 0.06%"; exact arithmetic gives 0.04%
  (0.06 taker TP leg replaced by 0.02 maker). The engine uses the exact 0.04%.

No-lookahead discipline: every gate is evaluated on the LAST COMPLETED candle of its
timeframe at each 1m step. Entries fill at the NEXT 1m open. If TP and SL both fall
inside one 1m candle, the SL is assumed to fill first (conservative).
"""
import json
import os
import bisect

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_sovereign")

# ── Locked parameters (spec) ────────────────────────────────────────────────
EMA_FAST, EMA_SLOW = 50, 200            # 4H dual EMA
FRACTAL_K = 2                            # fractal confirmation neighbours each side (15m/2m)
ATR_PERIOD = 14                          # 2m ATR14 (Wilder)
RR = 2.0                                 # TP = 2 x SL
RISK_FRAC = 0.015                        # 1.5% fixed capital risk per trade
LEV_CAP = 10                             # max notional / equity
MIN_NOTIONAL = 5.0                       # Bitget min order (USDT)
SWEEP_VALID_H = 3.0                      # 15m sweep stays READY for 3h
READY_TIMEOUT_H = 3.0                    # armed chain expires after 3h without trigger
L2_IMBALANCE_MIN = 1.5                   # live-only gate (stub True in lab)
START_EQUITY = 10.0

TAKER = 0.0006
MAKER = 0.0002
SLIP = 0.0003

# ── Data loading / resampling ───────────────────────────────────────────────
def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)

def resample(c1m, minutes):
    """UTC-aligned resample of 1m candles (ts = bucket open)."""
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
    """EMA seeded with SMA of the first `period` closes (needs >= period bars)."""
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
    """Wilder ATR, seeded with mean TR of first `period` bars."""
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
    """Confirmed swing lows: low[i] is the minimum of [i-k, i+k] — known only at i+k."""
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
    """Active FVG zones from completed 1H candles.
    Bullish FVG at i: high[i-2] < low[i]  -> zone (high[i-2], low[i])   (demand, longs)
    Bearish FVG at i: low[i-2]  > high[i] -> zone (high[i], low[i-2])   (supply, shorts)
    A zone dies when a COMPLETED 1H close trades fully through it:
      bullish dies on close < zone_low; bearish dies on close > zone_high."""
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
    c4h = load("near_4h.json")
    c1h_native = load("near_1h.json")
    c1m = load("near_1m.json")

    closes4 = [c["close"] for c in c4h]
    ema50_4h = ema_series(closes4, EMA_FAST)
    ema200_4h = ema_series(closes4, EMA_SLOW)

    c2m = resample(c1m, 2)
    c15m = resample(c1m, 15)
    atr2m = atr_series(c2m, ATR_PERIOD)

    bull_zones, bear_zones = fvg_zones(c1h_native)
    swing_lows15 = fractal_lows(c15m, FRACTAL_K)
    swing_highs15 = fractal_highs(c15m, FRACTAL_K)
    fhighs2 = fractal_highs(c2m, FRACTAL_K)
    flows2 = fractal_lows(c2m, FRACTAL_K)

    def closes_at(candles, tf_ms):
        return [c["ts"] + tf_ms for c in candles]

    idx4_close = closes_at(c4h, 4 * 3600_000)
    idx15_close = closes_at(c15m, 15 * 60_000)
    idx2_close = closes_at(c2m, 2 * 60_000)

    def last_idx(closes_at_list, ts):
        i = bisect.bisect_right(closes_at_list, ts) - 1
        return i if i >= 0 else None

    equity = START_EQUITY
    trades = []
    position = None

    state = {"phase": "FLAT", "dir": None, "armed_ts": None, "sweep_ts": None,
             "swept_low": None, "swept_high": None}

    for n in range(len(c1m) - 1):
        c = c1m[n]
        nxt = c1m[n + 1]
        ts = c["ts"]

        # ── open-position exit management (walked on this candle) ──
        if position:
            p, side = position, position["side"]
            hit_sl = c["low"] <= p["sl"] if side == "long" else c["high"] >= p["sl"]
            hit_tp = c["high"] >= p["tp"] if side == "long" else c["low"] <= p["tp"]
            exit_px = reason = None
            if hit_sl:  # conservative: SL first if both in same candle
                exit_px, reason = p["sl"], "SL"
            elif hit_tp:
                exit_px, reason = p["tp"], "TP_MAKER"
            if exit_px:
                gross = (exit_px - p["entry"]) * p["qty"] * (1 if side == "long" else -1)
                if reason == "TP_MAKER":
                    fee = p["entry"] * p["qty"] * (TAKER + SLIP) + exit_px * p["qty"] * MAKER
                else:
                    fee = p["entry"] * p["qty"] * (TAKER + SLIP) + exit_px * p["qty"] * (TAKER + SLIP)
                net = gross - fee
                equity += net
                trades.append({
                    "side": side, "entry": p["entry"], "exit": exit_px,
                    "sl": p["sl"], "tp": p["tp"], "atr_frac": p["atr_frac"],
                    "qty": p["qty"], "notional": p["qty"] * p["entry"],
                    "gross": round(gross, 6), "fees": round(fee, 6), "net": round(net, 6),
                    "reason": reason, "opened_ts": p["opened_ts"], "closed_ts": ts,
                    "held_min": round((ts - p["opened_ts"]) / 60000, 1),
                    "equity": round(equity, 6),
                })
                position = None
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

        # ── gate 4: 2m micro-CHOCH — close beyond last confirmed 2m fractal ──
        i2 = last_idx(idx2_close, ts)
        if i2 is None or i2 < ATR_PERIOD or atr2m[i2] is None:
            continue
        trig = False
        # the CHOCH level is the pullback swing that forms AFTER the liquidity sweep
        sweep_ts = state.get("sweep_ts") or 0
        if d == "bull":
            fh = [f for f in fhighs2 if f["confirmed_ts"] <= c2m[i2]["ts"] and f["ts"] > sweep_ts]
            if fh and c2m[i2]["close"] > fh[-1]["price"]:
                trig = True
        else:
            fl = [f for f in flows2 if f["confirmed_ts"] <= c2m[i2]["ts"] and f["ts"] > sweep_ts]
            if fl and c2m[i2]["close"] < fl[-1]["price"]:
                trig = True
        if not trig:
            continue

        # ── L2 imbalance gate: live-only (websocket top-10 depth, I_L2 >= 1.5).
        #    Not replayable from candles — stub True in the lab. ──
        # I_L2 = l2_imbalance(); if I_L2 < L2_IMBALANCE_MIN: continue   # live hook

        # ── execute: market entry at next 1m open ──
        atr = atr2m[i2]
        entry = nxt["open"]
        atr_frac = atr / entry
        dp = atr
        if d == "bull":
            sl, tp = entry - dp, entry + RR * dp
        else:
            sl, tp = entry + dp, entry - RR * dp
        qty = (RISK_FRAC * equity) / dp
        notional = qty * entry
        if notional < MIN_NOTIONAL:
            continue
        if notional > LEV_CAP * equity:
            notional = LEV_CAP * equity
            qty = notional / entry
        position = {"side": "long" if d == "bull" else "short", "entry": entry, "qty": qty, "sl": sl, "tp": tp,
                    "atr_frac": atr_frac, "opened_ts": nxt["ts"]}
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
    print(f"trades={n}  wins={len(wins)}  losses={len(losses)}  win_rate={wr*100:.1f}%")
    print(f"equity: {START_EQUITY:.2f} -> {equity:.2f}  (net {tot:+.4f})")
    print(f"avg_win={aw:+.4f}  avg_loss={al:+.4f}  payoff={abs(aw/al) if al else float('inf'):.2f}:1")
    if n:
        ev = wr * aw + (1 - wr) * al
        print(f"expectancy per trade: {ev:+.4f}  ({ev/START_EQUITY*100:+.3f}% of equity)")
    from collections import Counter
    print("exits:", dict(Counter(t["reason"] for t in trades)))
    fr = sorted(t["atr_frac"] for t in trades)
    if fr:
        med = fr[len(fr)//2]
        print(f"SL distance (ATR14 2m): median {med*100:.3f}%  min {fr[0]*100:.3f}%  max {fr[-1]*100:.3f}%")
        dP = med
        w_star = (dP + 0.0018) / (3 * dP + 0.0007)
        print(f"breakeven win rate at median dP: {w_star*100:.1f}%  (realized {wr*100:.1f}%)")
        below = sum(1 for x in fr if 2 * x - 0.0011 <= 0)
        print(f"trades with TP_net <= 0 (ATR below friction floor): {below}/{len(fr)}")
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
    with open(os.path.join(HERE, "sovereign_v6_trades.json"), "w") as f:
        json.dump(trades, f, indent=1)
    print("\ntrade log -> lab/sovereign_v6_trades.json")
