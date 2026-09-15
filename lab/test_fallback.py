"""Unit test — live→demo AUTO-FALLBACK decision logic (owner 2026-09-15).

Mocks LIVE_EXEC with a fake Bitget client so all four scenarios run without
touching the network:
  1. live funded            → stays LIVE, sizes on real equity
  2. live broke + flat     → falls back to DEMO, sizes on demo equity
  3. demo + hourly due + live funded + demo flat → switches back to LIVE
  4. demo + live funded but demo position open → stays on demo
  5. live broke but position open → stays LIVE managing it
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from engine import sovereign
from engine import live as LIVE_EXEC


class FakeClient:
    """Stands in for BitgetPrivate — per-environment account state."""
    live_equity = 100.0
    demo_equity = 5000.0
    live_position = None
    demo_position = None

    def __init__(self, demo=None):
        self.demo = LIVE_EXEC.DEMO if demo is None else demo

    def account_equity(self):
        return self.demo_equity if self.demo else self.live_equity

    def position(self, symbol):
        return self.demo_position if self.demo else self.live_position


def patch_env():
    sovereign.LIVE_EXEC.keys_present = lambda: True
    sovereign.LIVE_EXEC.client = lambda demo=None: FakeClient(demo=demo)
    sovereign.LIVE_EXEC.MIN_LIVE_EQUITY = 10.0
    sovereign.LIVE_EXEC.LIVE_RECHECK_MINUTES = 60.0
    f = FakeClient
    f.live_equity, f.demo_equity, f.live_position, f.demo_position = 100.0, 5000.0, None, None


def fresh_state(now):
    # non-empty sovereign dict — setdefault keeps sv attached to state
    return {"_pair": "NEARUSDT", "balance": 10.0,
            "sovereign": {"walk_ms": 0, "phase": "FLAT"}}


def run_one(state, live_flag=True):
    """Drive process_pair far enough to reach the live-decision block only:
    we can't run the whole engine offline, so we re-execute its exact
    decision block via exec of the source slice? No — simpler: the decision
    is observable through sv/su, so call the real code path with fetches
    monkeypatched to raise immediately after the decision (the decision
    happens BEFORE any fetch)."""
    import tick as T
    T.LIVE_TRADING = live_flag
    T.LIVE_PAIRS = {"NEARUSDT"}
    # any fetch after the decision raises — we only care that the decision
    # block ran and mutated state before hitting the network
    def boom(*a, **k):
        raise RuntimeError("STOP_AFTER_DECISION")
    T.fetch_candles = boom
    T.fetch_ticker = boom
    try:
        sovereign.process_pair(state)
    except RuntimeError as e:
        assert "STOP_AFTER_DECISION" in str(e), e
    return state


def main():
    now = datetime.now(timezone.utc)
    ok = 0

    # ── 1. live funded → LIVE, real equity sizing ──
    patch_env()
    st = fresh_state(now)
    run_one(st)
    sv, su = st["sovereign"], st
    assert sv.get("trading_env") is None, sv.get("trading_env")
    assert st["balance"] == 100.0, st["balance"]
    print("1. live funded            → stays LIVE, balance=real equity      OK")
    ok += 1

    # ── 2. live broke + flat → DEMO fallback ──
    patch_env()
    FakeClient.live_equity = 3.0
    st = fresh_state(now)
    run_one(st)
    sv = st["sovereign"]
    assert sv.get("trading_env") == "DEMO_FALLBACK", sv.get("trading_env")
    assert st["balance"] == 5000.0, st["balance"]          # demo funds size it
    assert sv.get("last_live_check"), "recheck timestamp set"
    print("2. live broke + flat      → DEMO_FALLBACK, balance=demo equity  OK")
    ok += 1

    # ── 3. demo + hourly due + live funded + flat → back to LIVE ──
    patch_env()
    FakeClient.live_equity = 150.0
    st = fresh_state(now)
    sv = st["sovereign"]
    sv["trading_env"] = "DEMO_FALLBACK"
    sv["last_live_check"] = (now - timedelta(hours=2)).isoformat()   # recheck due
    run_one(st)
    sv = st["sovereign"]
    assert sv.get("trading_env") is None, sv.get("trading_env")
    assert st["balance"] == 150.0, st["balance"]
    print("3. demo + live funded     → SWITCHED BACK to LIVE              OK")
    ok += 1

    # ── 4. demo + live funded but demo position open → stays demo ──
    patch_env()
    FakeClient.live_equity = 150.0
    FakeClient.demo_position = {"qty": 5}
    st = fresh_state(now)
    sv = st["sovereign"]
    sv["trading_env"] = "DEMO_FALLBACK"
    sv["pos"] = {"side": "long", "entry": 4.0}                          # demo pos tracked
    sv["last_live_check"] = (now - timedelta(hours=2)).isoformat()
    run_one(st)
    sv = st["sovereign"]
    assert sv.get("trading_env") == "DEMO_FALLBACK", sv.get("trading_env")
    assert st["balance"] == 5000.0
    print("4. demo pos open          → stays on demo until flat           OK")
    ok += 1

    # ── 5. live broke but live position open → stays LIVE managing it ──
    patch_env()
    FakeClient.live_equity = 3.0
    st = fresh_state(now)
    st["sovereign"]["pos"] = {"side": "long", "entry": 4.0}
    run_one(st)
    sv = st["sovereign"]
    assert sv.get("trading_env") is None, sv.get("trading_env")
    print("5. live pos open + broke  → stays LIVE, no new sizing          OK")
    ok += 1

    print(f"\nAll {ok}/5 fallback scenarios passed.")


if __name__ == "__main__":
    main()
