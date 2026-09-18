"""Sovereign V6 — top-down multi-timeframe entry engine (owner spec 2026-09-15).

Ported from lab/sovereign_engine.py (validated: NEAR 21d, maker retest entry,
net +0.33 on $10, breakeven 44.1% vs realized 50.0%).

Pipeline (a priori, locked — identical math to the lab engine):
  4H  EMA50/EMA200 alignment   -> directional bias lock
  1H  Fair Value Gap zone      -> arm (price inside active zone)
  15m fractal sweep + reclose  -> ready (liquidity absorption)
  2m  CHOCH beyond post-sweep  -> trigger
      L2 order-book imbalance  -> final gate (REST top-10 depth snapshot,
                                  I_L2 = sumV_bid/sumV_ask >= 1.5; fail-open
                                  with alert — a dead depth API must not
                                  deadlock the engine)
  Execution: POST_ONLY retest limit at the CHOCH level (owner-approved
  2026-09-15), 3h expiry. Maker 0.02% entry; maker TP 0.02%; SL = 1 x
  ATR14(2m) taker 0.06% + 0.03% slip; TP = 2 x SL (1:2 RR); 1.5% fixed
  capital risk; 10x notional cap; $5 Bitget minimum.

Friction floor: pairs with ATR14(2m) < SOVEREIGN_ATR_FLOOR (default 0.10%)
never enter — the lab proved sub-friction stops are mathematically dead
(BTC: 0.088% median ATR vs 0.11% round trip).

EVENT-DRIVEN REPLAY: each tick fetches fresh candles, then advances the
state machine through EVERY closed 2m candle since the last tick, in order
(lab-faithful walk; nothing is skipped if a tick is missed or late). Live
price is additionally checked between candle closes (paper fill at level).

State: full machine state in pairs[pair]["sovereign"]; the open position is
mirrored into the legacy last_error blob (serialize_positions) so the v5
cross-pair margin budget and dashboard see it unchanged.

Active ONLY when tick.SOVEREIGN_V6=1 and the pair is in tick.SOVEREIGN_PAIRS;
otherwise tick.py runs v5 untouched.
"""
import bisect
import json
import os
from datetime import datetime, timezone


def _zone_event(symbol, ts, kind, side, **kw):
    """Owner 2026-09-16: maker-zone lifecycle events -> execution chat."""
    try:
        from engine import ui_store
        ui_store.record_zone_event(symbol, ts, kind, side, **kw)
    except Exception:
        pass

from engine import live as LIVE_EXEC

# ── locked parameters (spec + lab report) ───────────────────────────────────
EMA_FAST, EMA_SLOW = 50, 200
FRACTAL_K = 2
RR = 2.0                       # TP = 2 x SL
RISK_FRAC = 0.015               # 1.5% fixed capital risk
LEV_CAP = 10                    # notional <= 10x balance
MIN_NOTIONAL = 5.0              # Bitget minimum
SWEEP_VALID_H = 3.0             # sweep READY validity
READY_TIMEOUT_H = 3.0           # armed chain expiry
L2_MIN = 1.5                    # I_L2 gate (spec)
ATR_FLOOR = float(os.environ.get("SOVEREIGN_ATR_FLOOR", "0.0010"))
L2_GATE = os.environ.get("SOVEREIGN_L2_GATE", "1") == "1"
TAKER, MAKER, SLIP = 0.0006, 0.0002, 0.0003

_N4H, _N1H, _N15, _N1M = 1000, 1000, 480, 600   # fetch depths: 166d 4H (EMA200),
# 41d 1H (FVG zone memory), 5d 15m (swing refs), 10h 1m -> 300 2m bars (ATR/fractals)
# — sized to match the lab backtest context (run B)
_l2_alerted = False


# ── indicators (identical math to the lab engine) ──────────────────────────
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
            out.append({"ts": candles[i]["ts"], "price": lo, "confirmed_ts": candles[i + k]["ts"]})
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
    """Bullish FVG at i: high[i-2]<low[i] -> (high[i-2], low[i]);
    bearish: low[i-2]>high[i] -> (high[i], low[i-2]). Zone dies on a CLOSED
    1H through its far edge."""
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


def find_zone(zones, ts, price):
    """The FVG zone a trigger fires in — captured for the chart overlay."""
    for z in zones:
        if (z["created_ts"] <= ts and (z["dead_after"] is None or z["dead_after"] > ts)
                and z["low"] <= price <= z["high"]):
            return z
    return None


def resample_2m(c1m_closed):
    """Closed 1m candles -> closed 2m candles (drops a trailing half-bucket)."""
    out = []
    for c in c1m_closed:
        b = c["ts"] // 120_000 * 120_000
        if not out or out[-1]["ts"] != b:
            out.append({"ts": b, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "vol": c["vol"]})
        else:
            o = out[-1]
            o["high"] = max(o["high"], c["high"])
            o["low"] = min(o["low"], c["low"])
            o["close"] = c["close"]
            o["vol"] += c["vol"]
    if out and (out[-1]["ts"] + 120_000) > (c1m_closed[-1]["ts"] + 60_000):
        out.pop()   # only 1 of 2 minutes present -> not a closed 2m candle
    return out


# ── L2 imbalance gate (spec; REST snapshot at trigger time) ───────────────
def fetch_depth_ratio(T, symbol):
    """sum(top-10 bid qty) / sum(top-10 ask qty). None = unavailable."""
    global _l2_alerted
    try:
        url = f"{T.BITGET}/merge-depth?symbol={symbol}&productType={T.PRODUCT}&type=step0&limit=10"
        data = T.http_get(url)
        if data.get("code") != "00000":
            raise RuntimeError(data.get("msg"))
        bids = (data.get("data") or {}).get("bids") or []
        asks = (data.get("data") or {}).get("asks") or []
        vb = sum(float(b[1]) for b in bids[:10])
        va = sum(float(a[1]) for a in asks[:10])
        if va <= 0:
            return None
        return vb / va
    except Exception:
        if not _l2_alerted:
            _l2_alerted = True
            try:
                T.send_telegram(f"[{symbol.replace('USDT','')}] sovereign L2 depth unavailable — gate fail-open this cycle")
            except Exception:
                pass
        return None


# ── the engine ──────────────────────────────────────────────────────────────
def process_pair(state):
    import tick as T

    symbol = state.get("_pair") or T.SYMBOL
    PTAG = symbol.replace("USDT", "")

    def _pt(text):
        try:
            T.send_telegram(f"[{PTAG}] {text}")
        except Exception:
            pass

    sv = state.setdefault("sovereign", {})   # setdefault: never detach from state
    balance = state["balance"]
    now = datetime.now(timezone.utc)

    # ── live mode: real account, real orders (owner 2026-09-15) ──
    # AUTO-FALLBACK (owner 2026-09-15): if the LIVE wallet can't fund a trade
    # (equity < MIN_LIVE_EQUITY) while the account is flat, the pair flips to
    # Bitget DEMO (same engine, pap:1 header, demo funds) and keeps trading.
    # While on demo the live wallet is re-probed every LIVE_RECHECK_MINUTES;
    # when it can fund trades again AND the demo side is flat, the pair flips
    # back to live. A position or resting order on either side blocks the
    # flip — it is never abandoned mid-trade.
    su = {}                                   # (was created too late — live block wrote to it)
    live_mode = (getattr(T, "LIVE_TRADING", False)
                 and symbol in getattr(T, "LIVE_PAIRS", set())
                 and LIVE_EXEC.keys_present()
                 # Owner spec 2026-09-17 Sec 5.3: the mode flag is an
                 # ADDITIONAL top-level gate on top of the existing
                 # LIVE_TRADING/LIVE_PAIRS/keys checks — real orders require
                 # ALL of them, not just this one.
                 and getattr(T, "current_mode", lambda: "PAPER")() == "LIVE")
    live_cli, equity = None, None
    on_demo = sv.get("trading_env") == "DEMO_FALLBACK"
    if live_mode:
        min_eq = getattr(LIVE_EXEC, "MIN_LIVE_EQUITY", 10.0)
        recheck_s = getattr(LIVE_EXEC, "LIVE_RECHECK_MINUTES", 60.0) * 60
        live_probe, live_eq = None, None

        # probe the LIVE wallet — every tick while live; on the demo-fallback
        # only hourly (owner: "demo one hour, another hour" — keep trading,
        # check the live wallet periodically until funds arrive).
        probe_due = (not on_demo) or (now - datetime.fromisoformat(sv.get("last_live_check", "1970-01-01T00:00:00+00:00"))).total_seconds() >= recheck_s
        if probe_due:
            try:
                live_probe = LIVE_EXEC.client(demo=False)
                live_eq = live_probe.account_equity()
                sv["last_live_check"] = now.isoformat()
            except Exception as e:
                if on_demo:
                    _pt(f"live wallet recheck failed ({e}) — staying on demo")
                else:
                    _pt(f"LIVE unreachable: {e} — trading paused this tick")
                    return "live-paused", f"live account unreachable: {e}"

        if not on_demo:
            if live_eq is not None:
                su["last_live_equity"] = round(live_eq, 4)
            if live_eq is not None and live_eq < min_eq and not sv.get("pos") \
                    and not (sv.get("pending") or {}).get("order_id"):
                sv["trading_env"] = "DEMO_FALLBACK"
                on_demo = True
                _pt(f"live wallet ${live_eq:.2f} below ${min_eq:.0f} min — "
                    f"AUTO-FALLBACK to Bitget demo; live rechecked every "
                    f"{getattr(LIVE_EXEC, 'LIVE_RECHECK_MINUTES', 60.0):.0f}min")
            elif live_eq is not None and live_eq < min_eq:
                _pt(f"live wallet ${live_eq:.2f} below ${min_eq:.0f} min but a "
                    f"position/order is open — managing it live, no new sizing")
        elif live_probe is not None:
            # on demo, hourly live probe returned — switch back when it can
            # fund trades AND the demo side is flat
            demo_flat = not sv.get("pos") and not (sv.get("pending") or {}).get("order_id")
            if live_eq is not None and live_eq >= min_eq and demo_flat:
                sv.pop("trading_env", None)
                on_demo = False
                _pt(f"live wallet funded (${live_eq:.2f} ≥ ${min_eq:.0f}) and demo flat — "
                    f"SWITCHED BACK to live trading")

        if on_demo:
            live_cli = LIVE_EXEC.client(demo=True)
            try:
                equity = live_cli.account_equity()   # demo funds size the trade
            except Exception as e:
                _pt(f"demo account unreachable: {e} — trading paused this tick")
                return "live-paused", f"demo account unreachable: {e}"
            state["balance"] = equity
            su["live_mode"] = True
            su["trading_env"] = "DEMO_FALLBACK"
            if sv.get("pending") and not sv["pending"].get("order_id"):
                sv["pending"] = None            # stale paper pending from before the flip
                _pt("paper pending cleared on demo activation")
        elif live_eq is not None or live_probe is not None:
            live_cli = live_probe or LIVE_EXEC.client(demo=False)
            if live_eq is not None:
                equity = live_eq
                state["balance"] = equity      # risk sizing on REAL equity
            su["live_mode"] = True
            if sv.get("pending") and not sv["pending"].get("order_id"):
                sv["pending"] = None            # stale paper pending from before the flip
                _pt("paper pending cleared on live activation")
        # (probe not due & on demo → handled above: live_cli stays None this
        # tick only if unreachable; demo client set below)

    # ── fetch all timeframes (drop forming candles) ──
    c4h = T.fetch_candles("4H", _N4H, symbol)[:-1]
    c1h = T.fetch_candles("1H", _N1H, symbol)[:-1]
    c15 = T.fetch_candles("15m", _N15, symbol)[:-1]
    c1m = T.fetch_candles("1m", _N1M, symbol)[:-1]
    c2m = resample_2m(c1m)
    ticker = T.fetch_ticker(symbol)
    price = ticker["last"]

    if len(c4h) < EMA_SLOW + 1 or len(c2m) < 20 or len(c15) < 2 * FRACTAL_K + 2:
        return "defer", "insufficient history for sovereign gates"

    su["last_price"] = price
    su["last_tick_at"] = now.isoformat()
    now_ms = c2m[-1]["ts"] + 120_000
    now_iso = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()

    # ── precompute indicator context (once per tick) ──
    closes4 = [c["close"] for c in c4h]
    ema50 = ema_series(closes4, EMA_FAST)
    ema200 = ema_series(closes4, EMA_SLOW)
    atr2m = atr_series(c2m, 14)
    atr_at = {c["ts"]: a for c, a in zip(c2m, atr2m) if a is not None}
    close_at = {c["ts"]: c["close"] for c in c2m}
    bull_zones, bear_zones = fvg_zones(c1h)
    sw_lows15 = fractal_lows(c15, FRACTAL_K)
    sw_highs15 = fractal_highs(c15, FRACTAL_K)
    fh_2m = fractal_highs(c2m, FRACTAL_K)
    fl_2m = fractal_lows(c2m, FRACTAL_K)
    c4h_close_t = [c["ts"] + 4 * 3600_000 for c in c4h]

    def bias_at(ms):
        i = bisect.bisect_right(c4h_close_t, ms) - 1
        if i < EMA_SLOW or ema200[i] is None or ema50[i] is None:
            return None
        cl = c4h[i]["close"]
        if cl > ema50[i] > ema200[i]:
            return "bull"
        if cl < ema50[i] < ema200[i]:
            return "bear"
        return None

    # ── candles to replay: everything closed since the last walk ──
    walked = sv.get("walk_ms") or (now_ms - 120_000)
    new_c = [c for c in c2m if c["ts"] + 120_000 > walked and c["ts"] + 120_000 <= now_ms]

    action, details = "observe", sv.get("phase", "FLAT")
    pending_filled = None

    for c in new_c:
        ts = c["ts"] + 120_000          # close time of this 2m candle
        px = c["close"]
        iso = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
        sv["walk_ms"] = ts             # cursor advances on EVERY consumed candle
                                       # (a close/expiry must not replay this candle)
        if live_mode and (sv.get("pos") or (sv.get("pending") or {}).get("order_id")):
            # live: the resting order / exchange TP+SL own execution; the
            # replay only tracks excursions (no simulated fills or closes)
            p = sv.get("pos")
            if p:
                side = p["side"]
                adv = (c["high"] - p["entry"]) / p["entry"] if side == "long" else (p["entry"] - c["low"]) / p["entry"]
                bad = (c["low"] - p["entry"]) / p["entry"] if side == "long" else (p["entry"] - c["high"]) / p["entry"]
                p["mfe_frac"] = round(max(p.get("mfe_frac") or 0.0, max(adv, 0)), 6)
                p["mae_frac"] = round(max(p.get("mae_frac") or 0.0, max(bad, 0)), 6)
            continue
        # lab-faithful sweep visibility: the lab's gate only ever saw the last
        # completed 15m candle WHILE ARMED. Candles closed while the machine
        # was busy (position riding, pending resting, bias off, outside zone)
        # are consumed UNSEEN — they must not resurrect later as stale sweeps.
        if sv.get("phase") not in ("ARMED", "READY"):
            sv["sweep_scan_ms"] = ts

        # ── 1. open position management ──
        pos = sv.get("pos")
        if pos:
            side = pos["side"]
            adv = (c["high"] - pos["entry"]) / pos["entry"] if side == "long" else (pos["entry"] - c["low"]) / pos["entry"]
            adv_bad = (c["low"] - pos["entry"]) / pos["entry"] if side == "long" else (pos["entry"] - c["high"]) / pos["entry"]
            pos["mfe_frac"] = round(max(pos.get("mfe_frac") or 0.0, max(adv, 0)), 6)
            pos["mae_frac"] = round(max(pos.get("mae_frac") or 0.0, max(adv_bad, 0)), 6)
            hit_sl = c["low"] <= pos["sl"] if side == "long" else c["high"] >= pos["sl"]
            hit_tp = c["high"] >= pos["tp"] if side == "long" else c["low"] <= pos["tp"]
            if hit_sl:                       # conservative: SL first in one candle
                _close(T, state, pos, pos["sl"], "SL", symbol, sv, su, iso, _pt)
                action, details = "closed", f"sovereign SL @ {pos['sl']}"
            elif hit_tp:
                _close(T, state, pos, pos["tp"], "TP", symbol, sv, su, iso, _pt)
                action, details = "closed", f"sovereign TP (maker) @ {pos['tp']}"
            else:
                sv["walk_ms"] = ts
            continue

        # ── 2. pending POST_ONLY retest limit ──
        pending = sv.get("pending")
        if pending:
            if ts > pending["expires_ms"]:
                sv["pending"] = None
                _pt(f"sovereign retest limit EXPIRED @ {pending['level']:.6g} ({pending['side']})")
                action, details = "expired", "pending limit expired"
                continue
            side, lvl = pending["side"], pending["level"]
            if (side == "long" and c["low"] <= lvl) or (side == "short" and c["high"] >= lvl):
                msg = _fill(T, state, sv, pending, lvl, iso, symbol, _pt)
                if msg == "opened":
                    action, details = "opened", f"sovereign {side} maker fill @ {lvl}"
                    sv["walk_ms"] = ts
                else:
                    action, details = "skip", msg
            else:
                sv["walk_ms"] = ts
            continue

        # ── 3. gates — evaluated as of THIS candle's close (lab walk) ──
        d = bias_at(ts)
        if d is None:
            if sv.get("phase") not in (None, "FLAT"):
                _pt("sovereign bias dissolved — state reset")
            sv = {"phase": "FLAT", "walk_ms": ts}
            su["last_bias"] = "none"
            continue
        su["last_bias"] = d

        in_zone = price_in_zone(bull_zones if d == "bull" else bear_zones, ts, px)

        if sv.get("phase", "FLAT") == "FLAT":
            if in_zone:
                # cursor opens at the just-completed 15m candle: the lab checked
                # the last completed 15m at the arming step itself (a sweep closed
                # up to 15 min before arming fires at the arming candle)
                sv = {"phase": "ARMED", "dir": d, "armed_ms": ts, "sweep_scan_ms": ts - 900_000, "walk_ms": ts}
                action, details = "armed", f"{d} FVG zone"
                _az = find_zone(bull_zones if d == "bull" else bear_zones, ts, px) or {}
                _zone_event(symbol, ts, "fvg_zone", d, fvg_top=_az.get("high"),
                            fvg_bottom=_az.get("low"), note="1H imbalance zone armed")
            else:
                sv["walk_ms"] = ts
            continue
        if sv.get("phase") == "ARMED" and d != sv.get("dir"):
            sv = {"phase": "FLAT", "walk_ms": ts}
            continue
        if sv.get("phase") == "ARMED" and not in_zone and ts - sv.get("armed_ms", ts) > READY_TIMEOUT_H * 3600_000:
            sv = {"phase": "FLAT", "walk_ms": ts}
            action, details = "expired", "armed timeout"
            _zone_event(symbol, ts, "expired", sv.get("dir") or d, note="armed timeout")
            continue

        # gate 3 — 15m sweep: scan each closed 15m candle EXACTLY once.
        # Runs while ARMED (arm the sweep) AND while READY (lab-faithful:
        # the lab re-checked the latest 15m every step — invalidation on a
        # 15m close below the swept level, or a fresh sweep extending READY).
        if sv.get("phase") in ("ARMED", "READY"):
            cursor = sv.get("sweep_scan_ms") or ts
            fresh15 = [x for x in c15 if cursor < x["ts"] + 900_000 <= ts]
            for c15c in fresh15:
                sv["sweep_scan_ms"] = c15c["ts"] + 900_000
                if d == "bull":
                    refs = [s for s in sw_lows15 if s["confirmed_ts"] <= c15c["ts"]]
                    if refs:
                        ref = refs[-1]["price"]
                        if c15c["low"] < ref and c15c["close"] > ref:
                            sv.update({"phase": "READY", "dir": d, "sweep_ms": ts,
                                       "swept_level": ref})
                            _pt(f"sovereign READY — 15m sweep @ {ref:.6g} reclosed ({d})")
                            action, details = "ready", "15m sweep"
                            _zone_event(symbol, ts, "sweep", d, level=ref,
                                        note="15m liquidity sweep reclosed")
                        elif sv.get("swept_level") is not None and c15c["close"] < sv["swept_level"]:
                            sv.update({"phase": "ARMED", "swept_level": None})
                else:
                    refs = [s for s in sw_highs15 if s["confirmed_ts"] <= c15c["ts"]]
                    if refs:
                        ref = refs[-1]["price"]
                        if c15c["high"] > ref and c15c["close"] < ref:
                            sv.update({"phase": "READY", "dir": d, "sweep_ms": ts,
                                       "swept_level": ref})
                            _pt(f"sovereign READY — 15m sweep @ {ref:.6g} reclosed ({d})")
                            action, details = "ready", "15m sweep"
                            _zone_event(symbol, ts, "sweep", d, level=ref,
                                        note="15m liquidity sweep reclosed")
                        elif sv.get("swept_level") is not None and c15c["close"] > sv["swept_level"]:
                            sv.update({"phase": "ARMED", "swept_level": None})
            sv["walk_ms"] = ts
            if sv.get("phase") != "READY":
                continue

        if sv.get("phase") != "READY":
            sv["walk_ms"] = ts
            continue
        if ts - sv.get("sweep_ms", ts) > SWEEP_VALID_H * 3600_000:
            sv.update({"phase": "ARMED", "swept_level": None, "walk_ms": ts})
            action, details = "expired", "sweep validity"
            _zone_event(symbol, ts, "expired", d, note="sweep validity window elapsed")
            continue

        # gate 4 — CHOCH beyond the post-sweep pullback fractal
        sweep_ms = sv.get("sweep_ms") or 0
        atr = atr_at.get(c["ts"])
        if atr is None:
            sv["walk_ms"] = ts
            continue
        trig = False
        level = None
        if d == "bull":
            refs = [f for f in fh_2m if f["confirmed_ts"] <= c["ts"] and f["ts"] > sweep_ms]
            if refs and c["close"] > refs[-1]["price"]:
                trig, level = True, refs[-1]["price"]
        else:
            refs = [f for f in fl_2m if f["confirmed_ts"] <= c["ts"] and f["ts"] > sweep_ms]
            if refs and c["close"] < refs[-1]["price"]:
                trig, level = True, refs[-1]["price"]
        if not trig:
            sv["walk_ms"] = ts
            continue

        atr_frac = atr / c["close"]
        if atr_frac < ATR_FLOOR:
            sv.update({"phase": "ARMED", "swept_level": None, "walk_ms": ts})
            _pt(f"sovereign trigger vetoed: ATR {atr_frac*100:.3f}% below friction floor {ATR_FLOOR*100:.1f}%")
            action, details = "veto", "ATR below friction floor"
            _zone_event(symbol, ts, "veto", d, level=level,
                        note=f"ATR {atr_frac*100:.3f}% below friction floor")
            continue

        # L2 imbalance gate (spec: approved ONLY if I_L2 >= 1.5)
        if L2_GATE:
            ratio = fetch_depth_ratio(T, symbol)
            if ratio is not None and ratio < L2_MIN:
                sv["walk_ms"] = ts
                action, details = "veto-l2", f"I_L2 {ratio:.2f} < {L2_MIN}"
                _zone_event(symbol, ts, "veto", d, level=level,
                            note=f"L2 imbalance {ratio:.2f} < {L2_MIN}")
                continue

        if live_mode:
            _z = find_zone(bull_zones if d == "bull" else bear_zones, ts, level) or {}
            sv["_trig_ctx"] = {"fvg_top": _z.get("high"), "fvg_bottom": _z.get("low"),
                               "sweep_price": sv.get("swept_level") or level}
            ok, msg = _live_place(T, live_cli, symbol, d, level, atr_frac, equity, sv, iso, _pt)
            if ok:
                action, details = "armed-limit-live", f"live retest limit @ {level}"
                _zone_event(symbol, ts, "choch_limit", d, level=level,
                            fvg_top=_z.get("high"), fvg_bottom=_z.get("low"),
                            sweep_price=sv.get("swept_level") or level,
                            note="POST_ONLY retest limit armed (live)")
            else:
                sv.update({"phase": "ARMED", "swept_level": None})
                action, details = "skip-live", msg
            continue
        zone = find_zone(bull_zones if d == "bull" else bear_zones, ts, level) or {}
        sv["pending"] = {
            "side": "long" if d == "bull" else "short",
            "level": level,
            "atr_frac": atr_frac,
            "placed_ms": ts,
            "expires_ms": ts + int(SWEEP_VALID_H * 3600_000),
            "fvg_top": zone.get("high"),
            "fvg_bottom": zone.get("low"),
            "sweep_price": sv.get("swept_level") or level,
        }
        sv["phase"] = "FLAT"
        sv["walk_ms"] = ts
        action, details = "armed-limit", f"retest limit @ {level}"
        _pt(f"sovereign TRIGGER {d}: POST_ONLY retest limit @ {level:.6g} (3h expiry, ATR {atr_frac*100:.3f}%)")
        _zone_event(symbol, ts, "choch_limit", d, level=level,
                    fvg_top=zone.get("high"), fvg_bottom=zone.get("low"),
                    sweep_price=sv.get("swept_level") or level,
                    note="POST_ONLY retest limit armed (3h expiry)")
        continue

    # ── live-price touch checks between candle closes (intra-tick safety;
    #    PAPER ONLY — in live mode the exchange's resting orders do this) ──
    pos = None if live_mode else sv.get("pos")
    if pos and action in ("observe", "hold"):
        side = pos["side"]
        if (side == "long" and price <= pos["sl"]) or (side == "short" and price >= pos["sl"]):
            _close(T, state, pos, pos["sl"], "SL", symbol, sv, su, now_iso, _pt)
            action, details = "closed", f"sovereign SL @ {pos['sl']} (tick price)"
            pos = None
        elif (side == "long" and price >= pos["tp"]) or (side == "short" and price <= pos["tp"]):
            _close(T, state, pos, pos["tp"], "TP", symbol, sv, su, now_iso, _pt)
            action, details = "closed", f"sovereign TP (maker) @ {pos['tp']} (tick price)"
            pos = None
    pending = None if live_mode else sv.get("pending")
    if not pos and pending and now_ms <= pending["expires_ms"]:
        side, lvl = pending["side"], pending["level"]
        if (side == "long" and price <= lvl) or (side == "short" and price >= lvl):
            msg = _fill(T, state, sv, pending, lvl, now_iso, symbol, _pt)
            if msg == "opened":
                action, details = "opened", f"sovereign {side} maker fill @ {lvl} (tick price)"
            else:
                action, details = "skip", msg

    # ── live reconcile: the exchange is the source of truth ──
    if live_mode:
        try:
            _live_reconcile(T, live_cli, state, symbol, sv, su, now_iso, _pt)
        except Exception as e:
            _pt(f"EXECUTION_ERROR reconcile {symbol}: {type(e).__name__}: {str(e)[:160]} — exchange stays source of truth")

    # ── persist: mirror position for budget/dashboard, save machine state ──
    pos = sv.get("pos")
    if pos:
        su.update(_mirror(pos, symbol))
    else:
        su["position_open"] = False
        su["side"] = "none"
        su["last_error"] = "[]"
    su["sovereign"] = sv
    T.sync(symbol, state_update=su)

    if pos and action == "observe":
        upnl = (price - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "long" else -1)
        return "hold", f"sovereign pos {pos['side']} uPnL {upnl:+.4f}"
    return action, details


# ── fill / close helpers ───────────────────────────────────────────────────
def _fill(T, state, sv, pending, lvl, iso, symbol, _pt):
    """Pending limit touched -> open the position (maker fill semantics)."""
    balance = state["balance"]
    side, atr_frac = pending["side"], pending["atr_frac"]
    dp = atr_frac * lvl
    qty = (RISK_FRAC * balance) / dp
    notional = qty * lvl
    if notional < MIN_NOTIONAL and balance >= MIN_NOTIONAL:
        # OWNER 09-17: a small wallet still trades — bump to the fixed
        # $3-cash x 10x clip instead of skipping below the exchange floor.
        clip = min(LIVE_EXEC.LIVE_MARGIN_USD * LEV_CAP, LEV_CAP * balance)
        if clip >= MIN_NOTIONAL:
            qty = clip / lvl
            notional = qty * lvl
    if notional < MIN_NOTIONAL:
        sv["pending"] = None
        _pt("sovereign fill skipped: below Bitget $5 minimum")
        return "min-notional"
    margin = notional / LEV_CAP
    if margin + float(state.get("_other_margin") or 0) > T.MARGIN_BUDGET * balance:
        sv["pending"] = None
        _pt("sovereign fill skipped: global margin budget")
        return "margin-budget"
    if notional > LEV_CAP * balance:
        notional = LEV_CAP * balance
        qty = notional / lvl
        margin = notional / LEV_CAP
    pos = {
        "side": side, "entry": lvl, "qty": qty, "notional": notional, "margin": margin,
        "sl": lvl - dp if side == "long" else lvl + dp,
        "tp": lvl + RR * dp if side == "long" else lvl - RR * dp,
        "atr_frac_at_entry": atr_frac, "entry_mode": "maker",
        "opened_at": iso, "mfe_frac": 0.0, "mae_frac": 0.0,
        "trade_id": None, "fvg_top": pending.get("fvg_top"),
        "fvg_bottom": pending.get("fvg_bottom"),
        "sweep_price": pending.get("sweep_price"),
    }
    try:
        from engine import ui_store
        pos["trade_id"] = ui_store.new_trade_id(symbol, iso)
        ui_store.record_open(
            pos["trade_id"], symbol, side, iso, lvl, pos["tp"], pos["sl"],
            f"1H_{'BULL' if side == 'long' else 'BEAR'}_FVG + 15m_SWEEP + 2m_CHOCH",
            fvg_top=pending.get("fvg_top"), fvg_bottom=pending.get("fvg_bottom"),
            sweep_price=pending.get("sweep_price"), engine="sovereign-v6")
    except Exception:
        pass
    sv["pending"] = None
    pos["exec_mode"] = "PAPER"   # 2026-09-18: trade mode tag = execution truth
    sv["pos"] = pos
    _pt(f"sovereign FILLED {side} @ {lvl:.6g} (maker retest) | SL {pos['sl']:.6g} TP {pos['tp']:.6g}")
    return "opened"


def _close(T, state, pos, exit_px, reason, symbol, sv, su, iso, _pt):
    """Exact sovereign friction: maker entry 0.02%, maker TP 0.02%,
    taker SL 0.06% + 0.03% slip."""
    side = pos["side"]
    gross = (exit_px - pos["entry"]) * pos["qty"] * (1 if side == "long" else -1)
    entry_fee = pos["entry"] * pos["qty"] * MAKER
    exit_fee = exit_px * pos["qty"] * (MAKER if reason == "TP" else TAKER + SLIP)
    fees = entry_fee + exit_fee
    net = gross - fees
    new_balance = round(state["balance"] + net, 6)

    tp_frac = abs(pos["tp"] - pos["entry"]) / pos["entry"]
    mfe = pos.get("mfe_frac") or 0.0
    t_state = T.classify_trade_state(reason, mfe, tp_frac)
    held_min = None
    try:
        held_min = int((datetime.fromisoformat(iso) - datetime.fromisoformat(pos["opened_at"])).total_seconds() // 60)
    except Exception:
        pass
    trade = {
        "pair": symbol, "engine": "sovereign-v6", "side": side,
        "trade_id": pos.get("trade_id"),
        "entry_mode": pos.get("entry_mode", "maker"),
        "trade_state": t_state, "mfe_frac": round(mfe, 6),
        "mae_frac": round(pos.get("mae_frac") or 0.0, 6),
        "minutes_held": held_min, "tp_frac": round(tp_frac, 6),
        "entry_price": pos["entry"], "exit_price": exit_px,
        "notional": pos["notional"], "margin": pos["margin"], "leverage": LEV_CAP,
        "hard_sl": pos["atr_frac_at_entry"],
        "gross_pnl": round(gross, 6), "fees": round(fees, 6), "net_pnl": round(net, 6),
        "reason": reason, "balance_after": new_balance,
        "atr_frac_at_entry": pos["atr_frac_at_entry"], "regime_at_entry": "sovereign-v6",
        "opened_at": pos["opened_at"], "closed_at": iso,
        "mode": pos.get("exec_mode", "PAPER"),   # execution truth, not account flag (2026-09-18)
    }
    wins = (state.get("wins") or 0) + (1 if net > 0 else 0)
    losses = (state.get("losses") or 0) + (0 if net > 0 else 1)
    sv.pop("pos", None)
    su.update({
        "position_open": False, "side": "none", "entry_price": 0,
        "tp_price": 0, "sl_price": 0, "notional": 0, "margin": 0,
        "opened_at": None, "last_error": "[]",
        "balance": new_balance, "wins": wins, "losses": losses,
        "total_trades": (state.get("total_trades") or 0) + 1,
        "sovereign": sv,
    })
    _pt(f"sovereign CLOSE {reason} {side} @ {exit_px:.6g} | net {net:+.4f} | balance ${new_balance:.2f}")
    T.sync(symbol, state_update=su, trade=trade)
    return trade


def _mirror(pos, symbol):
    """Mirror the sovereign position into standard pair fields + the legacy
    blob so v5's cross-pair budget, dashboard and state viewers see it."""
    import tick as T
    from engine.positions import serialize_positions
    blob_pos = {
        "pair": symbol, "side": pos["side"], "entry_price": pos["entry"],
        "tp_price": pos["tp"], "notional": pos["notional"], "margin": pos["margin"],
        "opened_at": pos["opened_at"], "aligned": True,
        "mfe_frac": pos.get("mfe_frac", 0.0), "mae_frac": pos.get("mae_frac", 0.0),
        "advisory_score": None, "wall_ratio": None, "regime_at_entry": "sovereign-v6",
        "atr_frac_at_entry": pos["atr_frac_at_entry"], "leverage": LEV_CAP,
        "hard_sl": pos["atr_frac_at_entry"],
    }
    su = serialize_positions([blob_pos], 1, symbol)
    su.update({
        "position_open": True, "side": pos["side"], "entry_price": pos["entry"],
        "tp_price": pos["tp"], "sl_price": pos["sl"], "notional": pos["notional"],
        "margin": pos["margin"], "opened_at": pos["opened_at"],
    })
    return su


# ── live execution helpers (owner 2026-09-15) ───────────────────────────────
def _live_place(T, live_cli, symbol, d, level, atr_frac, equity, sv, iso, _pt):
    """Place the REAL post-only retest limit with exchange-side TP/SL.

    Alert sequence (owner spec 2026-09-19): a strategy signal is NEVER
    reported as a trade. Real-exchange stages get distinct labels:
    LIVE_SIGNAL -> ORDER_SUBMITTED -> ORDER_ACCEPTED -> ORDER_FILLED ->
    PROTECTION_CONFIRMED -> POSITION_DETECTED -> POSITION_CLOSED.
    Any failed stage reports itself (ORDER_REJECTED / EXECUTION_ERROR)."""
    side = "long" if d == "bull" else "short"
    dp = atr_frac * level
    _pt(f"LIVE_SIGNAL {d} {symbol}: all gates passed — retest level {level:.6g} "
        f"(ATR {atr_frac*100:.3f}%); constructing live order")
    try:
        info = live_cli.contract_info(symbol)
        # OWNER 09-17: entries commit a FIXED $3 of cash at 10x — notional
        # is $3 x leverage (the $5 Bitget floor is cleared 6x over). If the
        # wallet is too small to carry $3 at 10x, clamp to what it can carry.
        cap = min(LIVE_EXEC.LIVE_MARGIN_USD * LEV_CAP, LEV_CAP * equity,
                  LIVE_EXEC.LIVE_MAX_NOTIONAL)
        qty = live_cli.round_qty(symbol, cap / level)
        notional = qty * level
        if qty < info["min_size"] or notional < max(MIN_NOTIONAL, info.get("min_usdt", LIVE_EXEC.LIVE_MIN_NOTIONAL)):
            _pt(f"LIVE_SIGNAL {d} {symbol}: order NOT submitted — below Bitget "
                f"minimum (qty {qty} @ {level:.6g}); re-armed, no trade")
            return False, f"below Bitget minimum (qty {qty} @ {level:.6g})"
        sl = level - dp if side == "long" else level + dp
        tp = level + RR * dp if side == "long" else level - RR * dp
        live_cli.set_leverage(symbol, LEV_CAP)
        _pt(f"ORDER_SUBMITTED {symbol} {side}: post-only limit @ {level:.6g} "
            f"qty {qty} — cash ${notional / LEV_CAP:.2f} @ {LEV_CAP}x = "
            f"${notional:.2f} notional (sl {sl:.6g} / tp {tp:.6g}), TP/SL preset with order")
        oid = live_cli.place_entry_limit(symbol, side, qty, level, sl, tp)
    except LIVE_EXEC.BitgetError as e:
        _pt(f"ORDER_REJECTED {symbol} {side}: Bitget code {getattr(e, 'code', '?')} — "
            f"{str(e)[:160]} (entry {level:.6g}) — NO trade opened, re-armed")
        return False, f"place failed: {e}"
    except Exception as e:
        _pt(f"EXECUTION_ERROR {symbol} {side} place: {type(e).__name__}: "
            f"{str(e)[:160]} — NO trade opened, re-armed")
        return False, f"place failed: {e}"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ctx = sv.pop("_trig_ctx", {}) or {}
    sv["pending"] = {
        "side": side, "level": level, "atr_frac": atr_frac,
        "placed_ms": now_ms, "expires_ms": now_ms + int(SWEEP_VALID_H * 3600_000),
        "order_id": oid, "qty": qty, "sl": sl, "tp": tp,
        "notional": notional, "margin": notional / LEV_CAP,
        "fvg_top": ctx.get("fvg_top"), "fvg_bottom": ctx.get("fvg_bottom"),
        "sweep_price": ctx.get("sweep_price"),
    }
    sv["phase"] = "FLAT"
    _pt(f"ORDER_ACCEPTED {symbol} {side}: Bitget accepted — order id {oid} "
        f"(post-only limit resting @ {level:.6g})")
    return True, oid


def _live_reconcile(T, live_cli, state, symbol, sv, su, now_iso, _pt):
    """Adopt exchange truth into the machine each tick: order fills, cancels,
    expiry, position closes (booked from real fills), orphan adoption."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # a) resting entry order
    pending = sv.get("pending")
    if pending and pending.get("order_id"):
        od = live_cli.order_detail(symbol, pending["order_id"])
        st = od["state"]
        if st in ("filled", "partially_filled"):
            entry = od["avg_price"] or pending["level"]
            sv["pos"] = {
                "side": pending["side"], "entry": entry,
                "qty": od["filled_size"] or pending["qty"],
                "notional": (od["filled_size"] or pending["qty"]) * entry,
                "margin": pending.get("margin", 0),
                "sl": od["sl"] or pending["sl"], "tp": od["tp"] or pending["tp"],
                "atr_frac_at_entry": pending["atr_frac"], "entry_mode": "maker-live",
                "exec_mode": ("DEMO" if sv.get("trading_env") == "DEMO_FALLBACK" else "LIVE"),
                "opened_at": now_iso, "mfe_frac": 0.0, "mae_frac": 0.0,
                "trade_id": None, "fvg_top": pending.get("fvg_top"),
                "fvg_bottom": pending.get("fvg_bottom"),
                "sweep_price": pending.get("sweep_price"),
            }
            try:
                from engine import ui_store
                sv["pos"]["trade_id"] = ui_store.new_trade_id(symbol, now_iso)
                ui_store.record_open(
                    sv["pos"]["trade_id"], symbol, pending["side"], now_iso,
                    entry, sv["pos"]["sl"], sv["pos"]["tp"],
                    f"1H_{'BULL' if pending['side'] == 'long' else 'BEAR'}_FVG + 15m_SWEEP + 2m_CHOCH",
                    fvg_top=pending.get("fvg_top"), fvg_bottom=pending.get("fvg_bottom"),
                    sweep_price=pending.get("sweep_price"), engine="sovereign-v6")
            except Exception:
                pass
            sv["pending"] = None
            _pt(f"ORDER_FILLED {pending['side']} {symbol} @ {entry:.6g} qty "
                f"{sv['pos']['qty']} — order {pending['order_id']} (avg price from Bitget)")
            if od["sl"] and od["tp"]:
                sv["pos"]["protection_sent"] = True
                _pt(f"PROTECTION_CONFIRMED {symbol}: exchange-side TP {od['tp']:.6g} / "
                    f"SL {od['sl']:.6g} read back from Bitget on the filled order")
        elif st == "canceled":
            sv["pending"] = None
            _pt(f"ORDER_REJECTED {symbol} {pending['side']}: Bitget canceled the "
                f"post-only entry (would have crossed book, order {pending['order_id']}) — NO trade, re-armed")
        elif now_ms > pending["expires_ms"]:
            try:
                live_cli.cancel_order(symbol, pending["order_id"])
            except Exception:
                pass
            sv["pending"] = None
            _pt(f"LIVE retest limit EXPIRED @ {pending['level']:.6g} — canceled, re-armed")

    # b) orphan resting order (crash between placement and state commit)
    if not sv.get("pending"):
        for od_row in live_cli.pending_orders(symbol):
            if (od_row.get("tradeSide") or "") != "open":
                continue
            oid = od_row.get("orderId")
            od = live_cli.order_detail(symbol, oid)
            if od["state"] not in ("new", "live", "live_part"):
                continue
            now_ms2 = int(datetime.now(timezone.utc).timestamp() * 1000)
            sv["pending"] = {
                "side": "long" if (od_row.get("side") or "buy") == "buy" else "short",
                "level": od["price"], "atr_frac": abs(od["sl"] - od["price"]) / od["price"] if od["sl"] else 0.0,
                "placed_ms": int(od_row.get("cTime") or now_ms2),
                "expires_ms": now_ms2 + int(SWEEP_VALID_H * 3600_000),
                "order_id": oid, "qty": float(od_row.get("size") or 0),
                "sl": od["sl"], "tp": od["tp"],
            }
            _pt(f"LIVE adopted orphan resting order @ {od['price']:.6g}")
            break

    # c) open position: real exits happen exchange-side; book when closed
    pos = sv.get("pos")
    ex = live_cli.position(symbol)
    if pos and ex is None:
        opened_ms = None
        try:
            opened_ms = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
        except Exception:
            opened_ms = None
        exit_px, reason = None, None
        if opened_ms:
            cf = live_cli.closing_fill(symbol, opened_ms)
            if cf and cf.get("price"):
                exit_px = cf["price"]
                reason = "TP" if abs(cf["price"] - pos["tp"]) <= abs(cf["price"] - pos["sl"]) else "SL"
        if exit_px is None:
            exit_px = pos["tp"] if abs((pos.get("tp") or 0)) else pos["sl"]
            reason = "TP" if exit_px == pos.get("tp") else "SL"
        trade = _close(T, state, pos, exit_px, reason, symbol, sv, su, now_iso, _pt)
        try:
            su["balance"] = round(live_cli.account_equity(), 6)
        except Exception:
            pass
        _pt(f"POSITION_CLOSED {symbol} {reason} @ {exit_px:.6g} — confirmed closed on Bitget"
            + (f", realized {trade['net_pnl']:+.4f} USDT (net of fees)" if trade else ""))
    elif pos and ex:
        if ex["entry"]:
            pos["entry"] = ex["entry"]
        pos["qty"] = ex["size"]
        if not pos.get("detected_sent"):
            pos["detected_sent"] = True
            _pt(f"POSITION_DETECTED {ex['side']} {symbol} @ {ex['entry']:.6g} "
                f"size {ex['size']} lev {ex['leverage']}x — reconcile confirms open position on Bitget")
        if not pos.get("protection_sent"):
            try:
                plan = live_cli.orders_plan_profit_loss(symbol)
                if plan:
                    trig = ", ".join(f"{(p.get('triggerType') or '?')}@{p.get('triggerPrice')}"
                                     for p in plan[:2])
                    pos["protection_sent"] = True
                    _pt(f"PROTECTION_CONFIRMED {symbol}: exchange-side TP/SL plan present ({trig})")
                else:
                    pos["protection_sent"] = True   # report once, not every tick
                    _pt(f"PROTECTION_UNCONFIRMED {symbol}: no exchange-side TP/SL plan found — verify in Bitget app")
            except Exception:
                pass    # transient read; retry on the next tick
    elif not pos and ex:
        # orphan position (crash between fill and state commit) — adopt
        sv["pos"] = {
            "side": ex["side"], "entry": ex["entry"], "qty": ex["size"],
            "notional": ex["size"] * ex["entry"], "margin": ex["size"] * ex["entry"] / max(ex.get("leverage") or LEV_CAP, 1),
            "sl": 0.0, "tp": 0.0,
            "atr_frac_at_entry": 0.0, "entry_mode": "maker-live",
            "exec_mode": ("DEMO" if sv.get("trading_env") == "DEMO_FALLBACK" else "LIVE"),
            "opened_at": now_iso, "mfe_frac": 0.0, "mae_frac": 0.0,
        }
        sv["pos"]["detected_sent"] = True
        _pt(f"POSITION_DETECTED {ex['side']} {symbol} @ {ex['entry']:.6g} size {ex['size']} "
            f"lev {ex.get('leverage') or LEV_CAP}x — orphan adopted (exchange-side TP/SL active, verify in Bitget app)")
