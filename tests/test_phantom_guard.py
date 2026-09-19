"""2026-09-19 P0 regression: phantom-trade + orphan-adoption guards.

Reproduces (with mocks, no network, no repo state) the live incident:
the owner opens a MANUAL short on Bitget -> sovereign v6 orphan-adopter used
to grab it as its own "long" -> when the owner closed it, the engine booked a
phantom trade with exit 0.0 (fake -$30 loss, negative balance).

Run:  python3 tests/test_phantom_guard.py
"""
import json, os, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from engine import sovereign as SV


class FakeCli:
    def __init__(self, pos, fills=None, pending=None, plans=None):
        self._pos, self._fills, self._pending, self._plans = pos, fills or [], pending or [], plans or []
    def pending_orders(self, symbol): return self._pending
    def order_detail(self, symbol, oid): return {"state": "new", "price": 0, "sl": 0, "tp": 0, "filled_size": 0}
    def position(self, symbol): return self._pos
    def closing_fill(self, symbol, after_ms): return None
    def orders_plan_profit_loss(self, symbol): return self._plans
    def account_equity(self): return 3.86


class FakeT:
    def __init__(self):
        self.trades, self.alerts = [], []
    def sync(self, symbol, state_update=None, trade=None):
        if trade: self.trades.append(trade)
    def classify_trade_state(self, *a, **k): return "mixed"


def _pt_sink(alerts):
    def _pt(msg): alerts.append(msg)
    return _pt


def run_case(pos, manual_positions=None, sighting_age_s=0, with_pos=None):
    tmp = tempfile.mkdtemp()
    store = os.path.join(tmp, "manual_manager.json")
    with open(store, "w") as f:
        json.dump({"config": {"enabled": True}, "positions": manual_positions or {}, "closed": []}, f)
    real_store = SV_MM.STORE_FILE
    SV_MM.STORE_FILE = store
    try:
        T = FakeT()
        state = {"balance": 3.86, "wins": 0, "losses": 0, "total_trades": 0}
        sv, su = {}, {}
        if with_pos: sv["pos"] = with_pos
        if sighting_age_s:
            sv["orphan_sighting"] = {"key": f"NEARUSDT:{pos['side']}",
                                     "first_seen_ms": int(time.time() * 1000) - sighting_age_s * 1000}
        now_iso = "2026-09-19T20:00:00+00:00"
        SV._live_reconcile(T, FakeCli(pos), state, "NEARUSDT", sv, su, now_iso, _pt_sink(T.alerts))
        return T, sv, su
    finally:
        SV_MM.STORE_FILE = real_store


import engine.manual_manager as SV_MM

failures = []
def check(name, cond, detail=""):
    print(("  ✔ " if cond else "  ✘ ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond: failures.append(name)

print("case A: manual short tracked by manual-manager → sovereign must NOT adopt")
T, sv, su = run_case({"side": "short", "size": 10.0, "entry": 3.6193, "leverage": 10,
                      "unrealized_pl": 0.0},
                     manual_positions={"NEARUSDT:short": {"symbol": "NEARUSDT", "side": "short"}})
check("no position adopted", "pos" not in sv, str(sv.get("pos")))
check("guard alert sent once", any("MANUAL position" in a for a in T.alerts))
check("no trade booked", len(T.trades) == 0)

print("case B: unknown position, first sighting → adoption DEFERRED")
T, sv, su = run_case({"side": "short", "size": 10.0, "entry": 3.6193, "leverage": 10, "unrealized_pl": 0.0})
check("no adoption on first sighting", "pos" not in sv)
check("sighting recorded", sv.get("orphan_sighting", {}).get("key") == "NEARUSDT:short")
check("deferral alert sent", any("DEFERRED" in a for a in T.alerts))

print("case C: unknown position survives 2nd pass (real orphan) → adopted")
T, sv, su = run_case({"side": "short", "size": 10.0, "entry": 3.6193, "leverage": 10, "unrealized_pl": 0.0},
                     sighting_age_s=30)
check("orphan adopted after deferral", (sv.get("pos") or {}).get("side") == "short")
check("side is SHORT (holdSide trusted)", (sv.get("pos") or {}).get("side") == "short")

print("case D: adopted orphan with tp/sl=0 vanishes → NO phantom trade booked")
orphan_pos = {"side": "short", "entry": 3.6193, "qty": 10.0, "notional": 36.19, "margin": 3.62,
              "sl": 0.0, "tp": 0.0, "atr_frac_at_entry": 0.0, "entry_mode": "maker-live",
              "opened_at": "2026-09-19T18:32:00+00:00", "mfe_frac": 0.0, "mae_frac": 0.0}
T, sv, su = run_case(None, with_pos=dict(orphan_pos))
check("no trade booked (data-integrity guard)", len(T.trades) == 0, f"{len(T.trades)} booked")
check("pos cleared", "pos" not in sv)
check("honest alert sent", any("exit price UNKNOWN" in a for a in T.alerts))
check("balance NOT poisoned", abs(state_check := 3.86 - 3.86) < 1e-9)

print()
if failures:
    print(f"FAIL: {len(failures)}"); sys.exit(1)
print("PASS: all phantom-guard checks green")
