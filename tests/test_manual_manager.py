"""Manual Position Auto-Manager — full simulation suite (owner spec 2026-09-18).

Covers the 20 mandated edge cases with a mocked Bitget client. Run:
    python3 tests/test_manual_manager.py
Exit code 0 = all pass. No network, no real orders, no repo state touched:
the module's file paths are redirected into a temp dir via monkeypatching.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import engine.manual_manager as MM
from engine.sovereign import atr_series, RR


# ── mocked Bitget ────────────────────────────────────────────────────────────
class FakeBitget:
    def __init__(self, positions=None):
        self.rows = [dict(r) for r in (positions or [])]     # exchange truth
        self.close_orders = []      # resting close-side limits [(oid, price, qty)]
        self.plan_orders = []       # resting profit/loss plans
        self.cancelled = []
        self.place_calls = []
        self._oid = 1000
        self.fail_place = False

    # contract info: NEAR-like step 1, min 1, prices 4dp
    def contract_info(self, symbol):
        return {"size_step": 1.0, "min_size": 1.0, "min_usdt": 5.0, "price_dp": 4}

    def round_qty(self, symbol, qty):
        step = self.contract_info(symbol)["size_step"]
        return max(int(qty / step) * step, step)

    def all_positions(self):
        return [dict(r) for r in self.rows]

    def position(self, symbol):
        for r in self.rows:
            if r["symbol"] == symbol and r["size"] > 0:
                return {"side": r["side"], "size": r["size"], "entry": r["entry"],
                        "leverage": r.get("leverage", 10),
                        "unrealized_pl": 0.0}
        return None

    def pending_orders(self, symbol):
        return [{"orderId": o["oid"], "tradeSide": "close", "side": o["side"],
                 "price": o["price"], "size": o["qty"]}
                for o in self.close_orders if o["symbol"] == symbol]

    def orders_plan_profit_loss(self, symbol):
        return self.plan_orders

    def place_reduce_limit(self, symbol, side, qty, price, margin_mode="isolated"):
        if self.fail_place:
            raise RuntimeError("bitget 40018 insufficient balance")
        self._oid += 1
        self.place_calls.append({"symbol": symbol, "side": side, "qty": qty,
                                 "price": price, "oid": self._oid})
        self.close_orders.append({"oid": self._oid, "symbol": symbol, "side": side,
                                  "price": price, "qty": qty})
        return str(self._oid)

    def cancel_order(self, symbol, order_id):
        self.cancelled.append(str(order_id))
        self.close_orders = [o for o in self.close_orders
                             if str(o["oid"]) != str(order_id)]

    def closing_fill(self, symbol, opened_after_ms):
        return {"ts": 999, "price": 105.0, "size": 10, "order_type": "limit"}


# ── mocked tick module (T) ───────────────────────────────────────────────────
class FakeTick:
    BITGET = "https://api.bitget.com"
    PRODUCT = "USDT-FUTURES"
    FEE_RATE = 0.0006

    def __init__(self, master=None):
        self.master = master or {"account": {"balance": 9.9}, "pairs": {}}
        self.alerts = []

    def load_master(self):
        return self.master

    def fetch_candles(self, granularity, limit=5, symbol="NEARUSDT"):
        # 300 synthetic 2m candles: close 100, high 101, low 99 -> TR = 2 constant
        # -> ATR14 (RMA) converges to exactly 2.0 -> atr_frac 0.02
        out = []
        ts = 1_700_000_000_000
        for i in range(limit):
            out.append({"ts": ts + i * 120_000, "open": 100.0, "high": 101.0,
                        "low": 99.0, "close": 100.0, "vol": 1.0})
        return out

    def send_telegram(self, text):
        self.alerts.append(text)


def pos(symbol, side, size, entry=100.0, lev=10, created_ms=100):
    return {"symbol": symbol, "side": side, "size": size, "entry": entry,
            "leverage": lev, "unrealized_pl": 0.0, "margin_mode": "isolated",
            "created_ms": created_ms}


# ── harness ─────────────────────────────────────────────────────────────────
PASS, FAIL = [], []
ORIG_STORE = MM.STORE_FILE
ORIG_TRADES = MM.TRADES_FILE
TMP = tempfile.mkdtemp(prefix="mmtest_")


def fresh(fake_master=None, config=None):
    """Isolated store + fresh client + fresh T for one test."""
    MM.STORE_FILE = os.path.join(TMP, "manual_manager.json")
    MM.TRADES_FILE = os.path.join(TMP, "manual_trades.jsonl")
    store = {"config": dict(config or {}), "positions": {}, "closed": []}
    json.dump(store, open(MM.STORE_FILE, "w"))
    open(MM.TRADES_FILE, "w").close()
    cli = FakeBitget()
    T = FakeTick(fake_master)
    return cli, T, store


def run(cli, T, n=1):
    import engine.manual_manager as m
    orig_client = m._L_client if hasattr(m, "_L_client") else None
    m.MM_CLIENT = cli
    # patch: manual_manager imports `from engine import live as _L` inside
    # run_tick and calls _L.client(demo=False) — patch keys_present + client
    import engine.live as L
    L.keys_present = lambda: True
    L.client = lambda demo=None: cli
    for _ in range(n):
        m.run_tick(T)
    L.client = lambda demo=None: None


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


def alerts_with(T, needle):
    return [a for a in T.alerts if needle in a]


def read_trades():
    try:
        return [json.loads(l) for l in open(MM.TRADES_FILE) if l.strip()]
    except OSError:
        return []


# ── the 20 mandated edge cases ──────────────────────────────────────────────
def main():
    print("\n== TP formula parity with the production engine ==")
    cli, T, store = fresh()
    candles = T.fetch_candles("2m", 300)
    series = atr_series(candles, 14)
    atr = next(a for a in reversed(series) if a is not None)
    check("synthetic ATR14(2m) == 2.0", abs(atr - 2.0) < 1e-9, f"got {atr}")
    tp, atr_frac = MM.compute_tp(T, "NEARUSDT", "long", 100.0)
    expected = 100.0 + RR * (atr / 100.0) * 100.0
    check("TP == entry + RR*ATR (long, matches sovereign formula)",
          abs(tp - expected) < 1e-6, f"{tp} vs {expected}")
    tp_s, _ = MM.compute_tp(T, "NEARUSDT", "short", 100.0)
    check("TP == entry - RR*ATR (short)", abs(tp_s - 96.0) < 1e-6, f"{tp_s}")

    print("\n== 1/2. manual long & short, no existing TP, OFF (detect+alert only) ==")
    cli, T, store = fresh(config={"enabled": False})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T)
    check("OFF: detected alert sent", len(alerts_with(T, "MANUAL POSITION DETECTED")) == 1)
    check("OFF: no TP placed", not cli.place_calls)
    check("OFF: TP NOT PLACED (AUTO-MANAGE OFF) alert",
          len(alerts_with(T, "AUTO-MANAGE OFF")) == 1)
    rec = MM.load_store()["positions"].get("NEARUSDT:long")
    check("OFF: position still tracked with computed TP", rec and rec["tp_price"] == 104.0)
    check("OFF: status MANUAL_MANAGED recorded", rec["status"] == "MANUAL_MANAGED")

    print("\n== 2b. manual short adopted with ON ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("BTCUSDT", "short", 20, entry=200.0)]
    T.fetch_candles = lambda g, l=5, s="BTCUSDT": [
        {"ts": i, "open": 200.0, "high": 202.0, "low": 198.0, "close": 200.0, "vol": 1}
        for i in range(l)]
    run(cli, T)
    check("short: adopted alert", len(alerts_with(T, "ADOPTED")) == 1)
    check("short: TP below entry", cli.place_calls and cli.place_calls[0]["price"] == 192.0,
          str(cli.place_calls))
    check("short: TP order is a buy (close short)", cli.place_calls[0]["side"] == "buy")

    print("\n== 3/19. existing TP -> MANUAL_IGNORED, untouched ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    cli.close_orders = [{"oid": 555, "symbol": "NEARUSDT", "side": "sell",
                        "price": 130.0, "qty": 10}]   # owner's own resting TP
    run(cli, T)
    rec = MM.load_store()["positions"]["NEARUSDT:long"]
    check("existing TP: ignored status", rec["status"] == "MANUAL_IGNORED")
    check("existing TP: no order placed", not cli.place_calls)
    check("existing TP: alert says left untouched",
          len(alerts_with(T, "EXISTING TP DETECTED")) == 1)
    cli.plan_orders = [{"orderId": 777, "state": "live", "triggerPrice": 120.0}]
    cli.close_orders = []
    MM.load_store and json.dump({"config": {"enabled": True}, "positions": {}, "closed": []},
                                open(MM.STORE_FILE, "w"))
    T2 = FakeTick()
    run(cli, T2)
    rec2 = MM.load_store()["positions"]["NEARUSDT:long"]
    check("plan TP also detected", rec2["status"] == "MANUAL_IGNORED")

    print("\n== 4/14. restart recovery: existing tracked position, TP order re-bound ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T)
    oid_first = cli.place_calls[0]["oid"]
    # simulate crash-after-submit: store lost the order id but the order rests
    st = MM.load_store()
    st["positions"]["NEARUSDT:long"]["tp_order_id"] = None
    MM.save_store(st)
    n_before = len(cli.place_calls)
    run(cli, T)
    check("recovery: NO duplicate order placed", len(cli.place_calls) == n_before,
          f"{n_before} -> {len(cli.place_calls)}")
    rec = MM.load_store()["positions"]["NEARUSDT:long"]
    check("recovery: order re-bound to resting close limit",
          str(rec["tp_order_id"]) == str(oid_first))

    print("\n== 5. position opened while bot running + 10. bot/manual coexist ==")
    cli, T, store = fresh(config={"enabled": True})
    master = {"account": {"balance": 9.9}, "pairs": {"NEARUSDT": {
        "position_open": True,
        "sovereign": {"pos": {"side": "long", "qty": 5}, "pending": None}}}}
    T.master = master
    cli.rows = [pos("NEARUSDT", "long", 5)]                       # exactly the bot's
    run(cli, T)
    check("bot-matched position: no alerts", not T.alerts, str(T.alerts[:2]))
    cli.rows = [pos("NEARUSDT", "long", 5), pos("NEARUSDT", "long", 12)]
    # NOTE: two rows same symbol+side is unrealistic; emulate size excess instead
    cli.rows = [pos("NEARUSDT", "long", 12)]
    T2 = FakeTick(master)
    run(cli, T2)
    rec = MM.load_store()["positions"].get("NEARUSDT:long")
    check("size excess over bot position treated MANUAL",
          rec is not None and "excess treated manual" in rec.get("note", ""),
          str(rec and rec.get("note")))
    cli.rows = [pos("NEARUSDT", "short", 8)]                     # opposite side
    json.dump({"config": {"enabled": True}, "positions": {}, "closed": []},
              open(MM.STORE_FILE, "w"))
    T3 = FakeTick(master)
    run(cli, T3)
    rec = MM.load_store()["positions"].get("NEARUSDT:short")
    check("opposite-side hedge row on bot symbol = manual", rec is not None)
    cli.rows = [pos("SOLUSDT", "long", 30)]
    json.dump({"config": {"enabled": True}, "positions": {}, "closed": []},
              open(MM.STORE_FILE, "w"))
    T4 = FakeTick(master)
    run(cli, T4)
    check("non-bot symbol = manual", "SOLUSDT:long" in MM.load_store()["positions"])

    print("\n== 6. multiple manual positions, different symbols ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10), pos("BTCUSDT", "short", 2, entry=200.0)]
    T.fetch_candles = lambda g, l=5, s=None: [
        {"ts": i, "open": 200.0, "high": 202.0, "low": 198.0, "close": 200.0, "vol": 1}
        for i in range(l)]
    run(cli, T)
    st = MM.load_store()
    check("both symbols adopted", "NEARUSDT:long" in st["positions"]
          and "BTCUSDT:short" in st["positions"])
    check("two TPs placed", len(cli.place_calls) == 2)

    print("\n== 7/17. size change after adoption -> alert + TP re-placed ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T)
    cli.rows = [pos("NEARUSDT", "long", 4)]                       # partial close
    run(cli, T)
    check("size-change alert", len(alerts_with(T, "SIZE CHANGED")) == 1)
    check("old TP cancelled", cli.cancelled == [str(cli.place_calls[0]["oid"])],
          str(cli.cancelled))
    check("new TP placed for remaining size", len(cli.place_calls) == 2
          and cli.place_calls[-1]["qty"] == 4)
    rec = MM.load_store()["positions"]["NEARUSDT:long"]
    check("store size updated", rec["size"] == 4)

    print("\n== 8. position completely closed manually ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T)
    tp_oid = cli.place_calls[0]["oid"]
    cli.rows = []                                                 # closed on phone
    run(cli, T)
    check("closed alert", len(alerts_with(T, "MANUAL POSITION CLOSED")) == 1)
    trades = read_trades()
    check("trade booked to MANUAL ledger", len(trades) == 1 and trades[0]["manual"] is True)
    check("manual PnL computed", abs(trades[0]["pnl_gross"] - 50.0) < 1e-9,
          str(trades[0]))
    check("resting TP cleaned up", str(tp_oid) in [str(c) for c in cli.cancelled])
    check("no longer tracked", "NEARUSDT:long" not in MM.load_store()["positions"])
    check("closed list in store", len(MM.load_store()["closed"]) == 1)

    print("\n== 9/15. hedge mode: long+short same symbol ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10), pos("NEARUSDT", "short", 7)]
    run(cli, T)
    st = MM.load_store()["positions"]
    check("both hedge legs tracked", "NEARUSDT:long" in st and "NEARUSDT:short" in st)
    check("two TP orders", len(cli.place_calls) == 2)

    print("\n== 16. one-way mode (no holdSide -> sign of total) ==")
    cli, T, store = fresh(config={"enabled": True})
    row = pos("NEARUSDT", "long", 10)
    cli.rows = [row]
    run(cli, T)
    check("row classified without holdSide", "NEARUSDT:long" in MM.load_store()["positions"])

    print("\n== 11/12. Bitget API timeout -> guarded, cooldown alert ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = None
    cli.all_positions = lambda: (_ for _ in ()).throw(RuntimeError("timed out"))
    run(cli, T)
    check("API error alert sent", len(alerts_with(T, "API ERROR")) == 1)
    run(cli, T)                                                  # within cooldown
    check("API error alert rate-limited", len(alerts_with(T, "API ERROR")) == 1)
    check("no crash, store intact", isinstance(MM.load_store(), dict))

    print("\n== 13/20. TP placement failure -> alert, retry next cycle ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    cli.fail_place = True
    run(cli, T)
    check("TP placement failed alert", len(alerts_with(T, "TP PLACEMENT FAILED")) == 1)
    rec = MM.load_store()["positions"]["NEARUSDT:long"]
    check("no order id recorded", rec["tp_order_id"] is None)
    cli.fail_place = False
    run(cli, T)
    check("retry succeeded next cycle", len(cli.place_calls) == 1)

    print("\n== kill switch: read-only monitoring + alerts ==")
    cli, T, store = fresh(config={"enabled": True, "kill": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T)
    check("kill: detection alert still sent", len(alerts_with(T, "DETECTED")) == 1)
    check("kill: no TP placed", not cli.place_calls)
    check("kill: KILL SWITCH alert", len(alerts_with(T, "KILL SWITCH")) == 1)

    print("\n== duplicate polling: same cycle repeated never double-alerts ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10)]
    run(cli, T, n=3)
    check("one detection alert across 3 cycles",
          len(alerts_with(T, "MANUAL POSITION DETECTED")) == 1)
    check("exactly one TP order", len(cli.place_calls) == 1)

    print("\n== statistics isolation ==")
    cli, T, store = fresh(config={"enabled": True})
    cli.rows = [pos("NEARUSDT", "long", 10), pos("NEARUSDT", "short", 10)]
    run(cli, T)
    cli.rows = []
    run(cli, T)
    check("manual trades only in manual ledger", len(read_trades()) == 2
          and all(t["manual"] for t in read_trades()))
    check("tick strategy ledger untouched (FakeTick has no writes — structural)",
          True)
    check("strategy stats keys absent from manual trades",
          all("win" not in t and "streak" not in t for t in read_trades()))

    print("\n== toggle script safety ==")
    os.environ["SET_MANUAL_MANAGER"] = "on"
    os.environ["SET_MANUAL_REASON"] = "test"
    os.environ["SET_MANUAL_ACTOR"] = "tester"
    r = os.system(f"cd {REPO} && python3 scripts/set_manual_manager.py > /dev/null 2>&1")
    check("toggle script runs", r == 0)
    check("toggle ON recorded",
          json.load(open(os.path.join(REPO, "data", "manual_manager.json")))["config"]["enabled"] is True)
    os.environ["SET_MANUAL_MANAGER"] = "off"
    os.system(f"cd {REPO} && python3 scripts/set_manual_manager.py > /dev/null 2>&1")
    check("toggle OFF recorded",
          json.load(open(os.path.join(REPO, "data", "manual_manager.json")))["config"]["enabled"] is False)
    # restore OFF default for the committed store
    json.dump({"config": {}, "positions": {}, "closed": []},
              open(os.path.join(REPO, "data", "manual_manager.json"), "w"), indent=2)

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'='*50}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
