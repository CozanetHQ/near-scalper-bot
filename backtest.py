#!/usr/bin/env python3
"""
NEAR Scalper Backtester — replays REAL historical 1m candles through the
ACTUAL tick.py strategy code (no reimplementation). The bot's own
process_tick/finalize_close/sync/get_state logic runs unchanged; only the
data sources and the clock are simulated.

Usage:
  python3 backtest.py     # run with the live config over history
  python3 sweep.py        # parameter grid search
"""
import json
import importlib.util
from datetime import datetime, timezone


def load_module():
    spec = importlib.util.spec_from_file_location("tick", "tick.py")
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)
    return tick


def load_candles():
    return json.load(open("data_1m.json"))


def aggregate(candles, minutes):
    """Aggregate 1m candles into aligned higher-TF candles (UTC-aligned buckets)."""
    ms = minutes * 60_000
    out = {}
    for c in candles:
        b = c["ts"] // ms * ms
        if b not in out:
            out[b] = {"ts": b, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "vol": 0}
        else:
            o = out[b]
            o["high"] = max(o["high"], c["high"])
            o["low"] = min(o["low"], c["low"])
            o["close"] = c["close"]
        out[b]["vol"] += c["vol"]
    return sorted(out.values(), key=lambda x: x["ts"])


class SimClock:
    """Replaces tick.datetime so the strategy's clock runs on candle time."""
    def __init__(self):
        self.now_ms = 0

    def datetime_cls(self):
        sim = self
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(sim.now_ms / 1000, tz=tz or timezone.utc)
            fromisoformat = staticmethod(datetime.fromisoformat)
        return _DT


class Backtest:
    def __init__(self, tick, candles_1m, start_balance=2.653):
        self.tick = tick
        self.c1m = candles_1m
        self.c15m = aggregate(candles_1m, 15)
        self.c4h = aggregate(candles_1m, 240)
        self.sim_state = {"status": "running", "balance": start_balance, "position_open": False,
                          "side": "none", "streak_count": 0, "streak_side": "none",
                          "entry_price": 0, "tp_price": 0, "sl_price": 0, "notional": 0, "margin": 0,
                          "opened_at": None, "total_trades": 0, "wins": 0, "losses": 0}
        self.trades = []
        self.clock = SimClock()
        t = self.tick
        t.datetime = self.clock.datetime_cls()
        t.send_telegram = lambda text: None
        t.http_get = lambda url, timeout=8: {"state": dict(self.sim_state), "trades": list(self.trades)}
        def http_post(url, payload, headers=None, timeout=8):
            su = payload.get("state") or {}
            self.sim_state.update(su)
            tr = payload.get("trade")
            if tr:
                self.trades.insert(0, tr)
            return {"ok": True, "state": dict(self.sim_state)}
        t.http_post = http_post
        t.fetch_candles = self._fetch_candles
        t.fetch_ticker = self._fetch_ticker

    def _fetch_candles(self, gran, limit=5):
        i = self.cur_i
        if gran == "1m":
            lo = max(0, i + 1 - limit)
            return self.c1m[lo:i + 1]
        if gran == "15m":
            ts = self.c1m[i]["ts"]
            bucket = ts // 900_000 * 900_000
            closed = [c for c in self.c15m if c["ts"] < bucket]
            forming = [c for c in self.c15m if c["ts"] == bucket]
            return (closed + forming)[-limit:]
        if gran == "4H":
            ts = self.c1m[i]["ts"]
            bucket = ts // 14_400_000 * 14_400_000
            prev = [c for c in self.c4h if c["ts"] < bucket]
            forming = [c for c in self.c4h if c["ts"] == bucket]
            return (prev[-1:] + forming)
        raise ValueError(gran)

    def _fetch_ticker(self):
        c = self.c1m[self.cur_i]
        return {"last": c["close"], "mark": c["close"]}

    def run(self, start_idx=30, end_idx=None):
        t = self.tick
        end = len(self.c1m) if end_idx is None else end_idx
        self.min_eq = None
        self.cap_hours = 0.0
        for i in range(start_idx, end):
            self.cur_i = i
            self.clock.now_ms = self.c1m[i]["ts"]
            try:
                state = t.get_state()
                t.process_tick(state)
                # owner 09-08 lab metrics: max drawdown + capital-time locked
                close = self.c1m[i]["close"]
                pos = t.parse_positions(state)
                unreal = 0.0
                for p in pos:
                    amt = p["notional"] / p["entry_price"]
                    unreal += ((close - p["entry_price"]) * amt if p["side"] == "long"
                               else (p["entry_price"] - close) * amt)
                    self.cap_hours += p["margin"] / 60.0
                eq = (state.get("balance") or 0) + unreal
                if self.min_eq is None or eq < self.min_eq: self.min_eq = eq
            except Exception as e:
                return f"ERROR at candle {i}: {type(e).__name__} {e}"
        return None

    def report(self):
        s = self.sim_state
        tr = self.trades
        wins = [x for x in tr if x["net_pnl"] > 0]
        losses = [x for x in tr if x["net_pnl"] <= 0]
        gross_w = sum(x["net_pnl"] for x in wins)
        gross_l = abs(sum(x["net_pnl"] for x in losses))
        curve = [s["balance"]]
        for x in reversed(tr):
            curve.append(x["balance_after"])
        curve = list(reversed(curve))
        peak = curve[0]
        mdd = 0.0
        for v in curve:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        return {
            "trades": len(tr),
            "win_rate": round(100 * len(wins) / len(tr), 1) if tr else 0,
            "net": round(s["balance"] - 2.653, 4),
            "final_balance": round(s["balance"], 4),
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "avg_win": round(gross_w / len(wins), 4) if wins else 0,
            "avg_loss": round(-gross_l / len(losses), 4) if losses else 0,
            "max_drawdown": round(mdd, 4),
        }


if __name__ == "__main__":
    import time
    t0 = time.time()
    tick = load_module()
    bt = Backtest(tick, load_candles())
    err = bt.run()
    if err:
        print(err)
    else:
        print(json.dumps(bt.report(), indent=2))
        print(f"runtime: {time.time()-t0:.1f}s")
        for x in bt.trades[:5]:
            print(f"  {x['side']:5} {x['reason']:4} net {x['net_pnl']:+.4f} bal {x['balance_after']:.4f}")
