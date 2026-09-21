"""Sovereign v6 gate replay — 30d per pair, faithful to engine/sovereign.py.

Purpose (owner 2026-09-20): measure how many qualifying setups the CURRENT
gate set actually produces, then test loosened variants until the machine
behaves like a scalper (owner target: ~100 trades on a good day across pairs)
without abandoning structure (bias + zone + sweep + CHOCH).

Faithfulness notes:
- 4H/1H/15m/2m data identical to the engine's fetches (same endpoints,
  same [:-1] forming-drop, same resample_2m).
- Gate math copied verbatim from engine/sovereign.py (ema_series, atr_series,
  fractal_lows/highs, fvg_zones, price_in_zone, bias_at).
- The walk replays closed 2m candles only; pending-limit fills walk forward
  on 2m candles (conservative SL-first in a both-hit candle), matching the
  engine's maker retest + RR=2 semantics.
- L2 depth gate: REST snapshots are not historically available, so the
  replay treats it as OFF for every variant (the live engine can keep it on).

Variants (argv[1], default = current):
  current   — engine's exact gates: 1H FVG zones, 15m sweep, 2m CHOCH,
              ATR_FLOOR 0.10%, sweep validity 3h, armed timeout 3h.
  loose15   — adds 15m FVG zones as an arming alternative (1H OR 15m),
              adds 2m fractal sweeps as an alternative to 15m sweeps,
              ATR_FLOOR 0.05%.
  scalp     — loose15 + arming also accepts price within 0.75xATR of a
              live zone edge (entry still needs the retest CHOCH), sweep
              validity 2h, armed timeout 2h.

Outputs per pair + total: arms, sweeps(READY), triggers(setups), fills,
wins/losses, gross expectancy in R (1R = the SL distance), setups/day.
Data caches under research/replay_cache/ (gitignored).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "replay_cache")
os.makedirs(CACHE, exist_ok=True)

PAIRS = ["NEARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
BASE = "https://api.bitget.com/api/v2/mix/market/candles"
DAYS = 30

# ── engine-locked params (engine/sovereign.py) ─────────────────────────────
EMA_FAST, EMA_SLOW = 50, 200
FRACTAL_K = 2
RR = 2.0
SWEEP_VALID_H, READY_TIMEOUT_H = 3.0, 3.0

VARIANTS = {
    "current": dict(zone15=False, sweep2m=False, near_zone_atr=0.0,
                    atr_floor=0.0010, sweep_h=3.0, ready_h=3.0),
    "loose15": dict(zone15=True, sweep2m=True, near_zone_atr=0.0,
                    atr_floor=0.0005, sweep_h=3.0, ready_h=3.0),
    "scalp":   dict(zone15=True, sweep2m=True, near_zone_atr=0.75,
                    atr_floor=0.0005, sweep_h=2.0, ready_h=2.0),
    "zone15only": dict(zone15=True, sweep2m=False, near_zone_atr=0.0,
                    atr_floor=0.0005, sweep_h=3.0, ready_h=3.0),
    "near15":  dict(zone15=True, sweep2m=False, near_zone_atr=0.75,
                    atr_floor=0.0005, sweep_h=3.0, ready_h=3.0),
    "near15f1": dict(zone15=True, sweep2m=False, near_zone_atr=0.75,
                    atr_floor=0.0010, sweep_h=3.0, ready_h=3.0),
}

# ── data (identical shape to the engine's fetch_candles) ────────────────────
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "replay"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())

def fetch(sym, gran, limit, end=None):
    q = f"symbol={sym}&productType=USDT-FUTURES&granularity={gran}&limit={limit}"
    if end:
        q += f"&endTime={end}"
    d = _get(f"{BASE}?{q}")
    if d.get("code") != "00000":
        raise RuntimeError(f"bitget candles: {d.get('msg')}")
    return [{"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
             "low": float(x[3]), "close": float(x[4]), "vol": float(x[5])}
            for x in d["data"]]

def fetch_range(sym, gran, days):
    """Paginate back to `days` ago; merge and de-dupe."""
    end = int(time.time() * 1000)
    need = end - days * 86_400_000
    rows, calls, guard = [], 0, 0
    while calls < 300 and guard < 400:
        d = fetch(sym, gran, 1000, end)
        calls += 1
        if not d:
            break
        rows = d + rows
        oldest = d[0]["ts"]
        if oldest <= need:
            break
        end = oldest - 1
        time.sleep(0.05)
    seen, out = set(), []
    for c in rows:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            out.append(c)
    out.sort(key=lambda c: c["ts"])
    return out

def load_pair(sym):
    path = os.path.join(CACHE, f"{sym}_{DAYS}d.json")
    if os.path.exists(path):
        return json.load(open(path))
    data = {
        "1m": fetch_range(sym, "1m", DAYS),
        "15m": fetch_range(sym, "15m", DAYS),
        "1H": fetch_range(sym, "1H", DAYS + 15),   # zone memory ~ engine's 41d
        "4H": fetch_range(sym, "4H", DAYS + 90),   # EMA200 warmup + walk window
    }
    json.dump(data, open(path, "w"))
    return data

# ── engine math (verbatim) ──────────────────────────────────────────────────
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
    a = sum(trs[1: period + 1]) / period
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
            out.append({"ts": candles[i]["ts"], "price": lo,
                        "confirmed_ts": candles[i + k]["ts"]})
    return out

def fractal_highs(candles, k=2):
    out = []
    n = len(candles)
    for i in range(k, n - k):
        hi = candles[i]["high"]
        if all(candles[j]["high"] <= hi for j in range(i - k, i + k + 1) if j != i):
            out.append({"idx": i, "ts": candles[i]["ts"], "price": hi,
                        "confirmed_ts": candles[i + k]["ts"]})
    return out

def fvg_zones(c1h):
    bull, bear = [], []
    for i in range(2, len(c1h)):
        a, c = c1h[i - 2], c1h[i]
        if a["high"] < c["low"]:
            bull.append({"created_ts": c["ts"], "low": a["high"], "high": c["low"],
                         "dead_after": None})
        if a["low"] > c["high"]:
            bear.append({"created_ts": c["ts"], "low": c["high"], "high": a["low"],
                         "dead_after": None})
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

def resample_2m(c1m_closed):
    out = []
    for c in c1m_closed:
        b = c["ts"] // 120_000 * 120_000
        if not out or out[-1]["ts"] != b:
            out.append({"ts": b, "open": c["open"], "high": c["high"], "low": c["low"],
                        "close": c["close"], "vol": c["vol"]})
        else:
            o = out[-1]
            o["high"] = max(o["high"], c["high"])
            o["low"] = min(o["low"], c["low"])
            o["close"] = c["close"]
            o["vol"] += c["vol"]
    if out and (out[-1]["ts"] + 120_000) > (c1m_closed[-1]["ts"] + 60_000):
        out.pop()
    return out

# ── the walk ───────────────────────────────────────────────────────────────
def replay_pair(sym, cfg, quiet=False):
    data = load_pair(sym)
    c4h = data["4H"][:-1]
    c1h = data["1H"][:-1]
    c15 = data["15m"][:-1]
    all2m = resample_2m(data["1m"])
    walk_start_ms = all2m[-1]["ts"] - (DAYS - 1) * 86_400_000
    c2m = [c for c in all2m if c["ts"] >= walk_start_ms]
    if len(c15) < 2 * FRACTAL_K + 2 or len(c2m) < 20 or len(c4h) < EMA_SLOW + 1:
        print(f"{sym}: insufficient history — 4H={len(c4h)} 15m={len(c15)} 2m={len(c2m)}")
        return None

    closes4 = [c["close"] for c in c4h]
    ema50 = ema_series(closes4, EMA_FAST)
    ema200 = ema_series(closes4, EMA_SLOW)
    c4h_close_t = [c["ts"] + 4 * 3600_000 for c in c4h]
    import bisect

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

    bull1h, bear1h = fvg_zones(c1h)
    if cfg["zone15"]:
        bull15, bear15 = fvg_zones(c15)
        bull_zones = bull1h + bull15
        bear_zones = bear1h + bear15
    else:
        bull_zones, bear_zones = bull1h, bear1h

    sw_lows15 = fractal_lows(c15, FRACTAL_K)
    sw_highs15 = fractal_highs(c15, FRACTAL_K)
    fl_2m = fractal_lows(all2m, FRACTAL_K)
    fh_2m = fractal_highs(all2m, FRACTAL_K)
    atr2m = atr_series(all2m, 14)
    atr_at = {c["ts"]: a for c, a in zip(all2m, atr2m) if a is not None}

    # per-15m-candle sweep scan helper
    def sweep_scan(d, sweep_ms, ts, refs15_l, refs15_h):
        """Return (swept_level, invalidated) for the 15m candles between
        sweep_ms and ts — engine logic; extended by 2m fractal sweeps."""
        found, invalid = None, False
        if d == "bull":
            refs = [s for s in refs15_l if s["confirmed_ts"] <= ts]
            if refs:
                ref = refs[-1]["price"]
                if sweep_ms is None or sweep_ms < ts:
                    # engine checks each fresh 15m; approximate with the
                    # window's extremes over fresh 15m candles
                    pass
            return found, invalid
        return found, invalid

    phase, dirn = "FLAT", None
    armed_ms = sweep_ms = swept_level = 0
    sweep_scan_ms = 0
    stats = dict(arm=0, ready=0, trigger=0, fill=0, win=0, loss=0, expired=0,
                 veto_atr=0)
    pnl_r = 0.0
    pos = None
    pending = None
    daily = {}

    near_mult = cfg["near_zone_atr"]
    atr_floor = cfg["atr_floor"]
    sweep_valid_ms = cfg["sweep_h"] * 3600_000
    ready_timeout_ms = cfg["ready_h"] * 3600_000

    def in_zone_any(zones, ts, price):
        return any(z["created_ts"] <= ts and
                   (z["dead_after"] is None or z["dead_after"] > ts) and
                   z["low"] <= price <= z["high"] for z in zones)

    def near_zone_edge(zones, ts, price, atr):
        if not zones or not atr:
            return False
        for z in zones:
            if z["created_ts"] <= ts and (z["dead_after"] is None or z["dead_after"] > ts):
                if z["low"] <= price <= z["high"]:
                    return True
                if dirn == "bull" and 0 <= z["low"] - price <= near_mult * atr:
                    return True
                if dirn == "bear" and 0 <= price - z["high"] <= near_mult * atr:
                    return True
        return False

    fresh15_seen = 0
    for c in c2m:
        ts = c["ts"] + 120_000
        px = c["close"]
        day = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%m-%d")

        # ── open position management (RR walk, SL-first conservative) ──
        if pos:
            side, lvl, dp = pos["side"], pos["lvl"], pos["dp"]
            hit_sl = c["low"] <= lvl - dp if side == "long" else c["high"] >= lvl + dp
            hit_tp = c["high"] >= lvl + RR * dp if side == "long" else c["low"] <= lvl - RR * dp
            if hit_sl:                       # conservative: SL first
                pnl_r -= 1.0
                stats["loss"] += 1
                pos = None
                daily[day] = daily.get(day, 0) + 1
            elif hit_tp:
                pnl_r += RR
                stats["win"] += 1
                pos = None
                daily[day] = daily.get(day, 0) + 1
            else:
                continue
            continue

        # ── pending limit fill ──
        if pending:
            if ts > pending["expires_ms"]:
                pending = None
            else:
                lvl, side, dp = pending["level"], pending["side"], pending["dp"]
                touched = c["low"] <= lvl if side == "long" else c["high"] >= lvl
                if touched:
                    stats["fill"] += 1
                    pos = {"side": side, "lvl": lvl, "dp": dp}
                    pending = None
                continue

        d = bias_at(ts)
        if d is None:
            if phase != "FLAT":
                stats["expired"] += 1
            phase, dirn = "FLAT", None
            continue

        if phase == "FLAT":
            zones = bull_zones if d == "bull" else bear_zones
            atr = atr_at.get(c["ts"]) or 0
            hit = in_zone_any(zones, ts, px)
            if not hit and near_mult:
                hit = near_zone_edge(zones, ts, px, atr)
            if hit:
                phase, dirn, armed_ms = "ARMED", d, ts
                sweep_scan_ms = ts - 900_000
                stats["arm"] += 1
            continue

        if phase == "ARMED" and d != dirn:
            phase = "FLAT"
            continue
        if phase == "ARMED" and ts - armed_ms > ready_timeout_ms:
            phase = "FLAT"
            stats["expired"] += 1
            continue

        # ── sweep gate: fresh closed 15m candles ──
        if phase in ("ARMED", "READY"):
            fresh = [x for x in c15 if sweep_scan_ms < x["ts"] + 900_000 <= ts]
            for fc in fresh:
                sweep_scan_ms = fc["ts"] + 900_000
                if dirn == "bull":
                    refs = [s for s in sw_lows15 if s["confirmed_ts"] <= fc["ts"]]
                    if refs:
                        ref = refs[-1]["price"]
                        if fc["low"] < ref and fc["close"] > ref:
                            phase, sweep_ms, swept_level = "READY", ts, ref
                            stats["ready"] += 1
                        elif swept_level and fc["close"] < swept_level:
                            phase, swept_level = "ARMED", None
                else:
                    refs = [s for s in sw_highs15 if s["confirmed_ts"] <= fc["ts"]]
                    if refs:
                        ref = refs[-1]["price"]
                        if fc["high"] > ref and fc["close"] < ref:
                            phase, sweep_ms, swept_level = "READY", ts, ref
                            stats["ready"] += 1
                        elif swept_level and fc["close"] > swept_level:
                            phase, swept_level = "ARMED", None
            if phase != "READY":
                # 2m fractal sweep alternative (loosened variants)
                if cfg["sweep2m"] and phase == "ARMED":
                    if dirn == "bull":
                        refs = [f for f in fl_2m
                                if sweep_scan_ms < f["confirmed_ts"] <= ts]
                        if refs:
                            ref = refs[-1]["price"]
                            if c["low"] < ref and px > ref:
                                phase, sweep_ms, swept_level = "READY", ts, ref
                                stats["ready"] += 1
                    else:
                        refs = [f for f in fh_2m
                                if sweep_scan_ms < f["confirmed_ts"] <= ts]
                        if refs:
                            ref = refs[-1]["price"]
                            if c["high"] > ref and px < ref:
                                phase, sweep_ms, swept_level = "READY", ts, ref
                                stats["ready"] += 1
                if phase != "READY":
                    continue

        if ts - sweep_ms > sweep_valid_ms:
            phase, swept_level = "ARMED", None
            continue

        # ── CHOCH beyond post-sweep fractal ──
        atr = atr_at.get(c["ts"])
        if atr is None:
            continue
        trig, level = False, None
        if dirn == "bull":
            refs = [f for f in fh_2m if f["confirmed_ts"] <= c["ts"] and f["ts"] > sweep_ms]
            if refs and px > refs[-1]["price"]:
                trig, level = True, refs[-1]["price"]
        else:
            refs = [f for f in fl_2m if f["confirmed_ts"] <= c["ts"] and f["ts"] > sweep_ms]
            if refs and px < refs[-1]["price"]:
                trig, level = True, refs[-1]["price"]
        if not trig:
            continue

        atr_frac = atr / px
        if atr_frac < atr_floor:
            stats["veto_atr"] += 1
            phase, swept_level = "ARMED", None
            continue

        stats["trigger"] += 1
        dp = atr_frac * level
        pending = {
            "side": "long" if dirn == "bull" else "short",
            "level": level,
            "dp": dp,
            "expires_ms": ts + sweep_valid_ms,
        }
        phase, swept_level = "FLAT", None

    out = dict(stats)
    out["pnl_r"] = round(pnl_r, 2)
    out["days"] = DAYS
    out["setups_per_day"] = round(stats["trigger"] / DAYS, 2)
    out["trades_per_day"] = round((stats["win"] + stats["loss"]) / DAYS, 2)
    best_day = max(daily.values()) if daily else 0
    out["best_day_trades"] = best_day
    if not quiet:
        print(f"{sym:9s} arm={stats['arm']:4d} ready={stats['ready']:4d} "
              f"trigger={stats['trigger']:4d} fill={stats['fill']:4d} "
              f"W/L={stats['win']}/{stats['loss']} pnl={pnl_r:+6.1f}R "
              f"setups/day={out['setups_per_day']:5.2f} best_day={best_day}")
    return out


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "current"
    cfg = VARIANTS[variant]
    only = sys.argv[2].split(",") if len(sys.argv) > 2 else PAIRS
    print(f"== variant: {variant} ==")
    agg = dict(arm=0, ready=0, trigger=0, fill=0, win=0, loss=0, pnl=0.0,
               best=0)
    for sym in only:
        r = replay_pair(sym, cfg)
        if r:
            agg["arm"] += r["arm"]; agg["ready"] += r["ready"]
            agg["trigger"] += r["trigger"]; agg["fill"] += r["fill"]
            agg["win"] += r["win"]; agg["loss"] += r["loss"]
            agg["pnl"] += r["pnl_r"]; agg["best"] += r["best_day_trades"]
    n = len(only)
    print(f"TOTAL     arm={agg['arm']} ready={agg['ready']} "
          f"trigger={agg['trigger']} fill={agg['fill']} "
          f"W/L={agg['win']}/{agg['loss']} pnl={agg['pnl']:+.1f}R | "
          f"setups/day={agg['trigger']/DAYS:.1f} trades/day={(agg['win']+agg['loss'])/DAYS:.1f} "
          f"best-day≈{agg['best']}")


if __name__ == "__main__":
    main()
