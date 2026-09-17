"""Set Trading Mode (owner spec 2026-09-17, Master Architecture Sec 5.3).

Invoked ONLY by the "Set Trading Mode" GitHub Actions workflow
(.github/workflows/set_mode.yml), which is the closest thing this
GitHub-Actions-native bot has to an authenticated founder-only UI toggle —
running it requires being logged into the CozanetHQ GitHub org.

Contract:
  1. Force-close every OPEN v5 position (ETH/SOL/XRP-style pairs) at the
     current market price, regardless of P&L, tagged MODE_SWITCH_FORCE_CLOSE.
  2. If a SOVEREIGN pair (BTC/NEAR) has an open position, REFUSE — the
     sovereign engine's close path hasn't been exercised by this script
     against a real position yet. Let it close naturally (TP/SL/kill) or
     close it manually, then re-run the mode switch.
  3. Only once every position is confirmed flat does it flip the mode flag
     (state/account/mode) via tick.write_mode().
  4. Logs every switch to data/mode_log.jsonl (owner audit trail, mirrors
     data/reset_log.jsonl).

Never called from the engine's own tick loop.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tick():
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    spec = importlib.util.spec_from_file_location("tick", os.path.join(_REPO, "tick.py"))
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    return t


def main():
    new_mode = (os.environ.get("SET_MODE") or "").strip().upper()
    reason = os.environ.get("SET_MODE_REASON") or ""
    actor = os.environ.get("SET_MODE_ACTOR") or "unknown"
    if new_mode not in ("LIVE", "PAPER"):
        print(f"SET_MODE must be LIVE or PAPER, got {new_mode!r}", file=sys.stderr)
        sys.exit(1)
    if not reason.strip():
        print("SET_MODE_REASON is required (audit trail)", file=sys.stderr)
        sys.exit(1)

    t = _load_tick()
    from engine.positions import parse_positions

    master = t.load_master()
    current = t.current_mode(master)
    if current == new_mode:
        print(f"already in {new_mode} mode — nothing to do")
        return

    closed = []
    for pair, ps in (master.get("pairs") or {}).items():
        if not ps.get("position_open"):
            continue
        sv = ps.get("sovereign")
        if sv and sv.get("pos"):
            print(f"REFUSING: {pair} has an OPEN SOVEREIGN position — this script's "
                  "sovereign close path is unverified against real data. Wait for it "
                  "to close naturally or close it manually, then retry.", file=sys.stderr)
            sys.exit(2)
        positions = parse_positions(ps, pair)
        if not positions:
            continue
        try:
            candles = t.fetch_candles("1m", 1, pair)
            price = float(candles[-1]["close"])
        except Exception as e:
            print(f"REFUSING: could not fetch a live price for {pair} to force-close "
                  f"at ({e}) — no position may close at a stale/guessed price.", file=sys.stderr)
            sys.exit(3)
        now = datetime.now(timezone.utc).isoformat()
        balance = float((master.get("account") or {}).get("balance") or t.START_BALANCE)
        remaining = []
        for pos in positions:
            trade, balance = t.close_position(pos, price, "MODE_SWITCH_FORCE_CLOSE", now, balance)
            t.sync(pair, state_update={"balance": balance}, trade=trade)
            closed.append({"pair": pair, "side": pos["side"], "exit_price": price,
                            "net_pnl": trade["net_pnl"]})
        # sync() re-reads master itself; refresh our local snapshot with the
        # position now flat before the next pair's loop iteration.
        master = t.load_master()
        master["pairs"][pair]["position_open"] = False
        master["pairs"][pair]["last_error"] = "[]"
        os.makedirs(os.path.dirname(t.STATE_FILE), exist_ok=True)
        with open(t.STATE_FILE, "w") as f:
            json.dump(master, f, separators=(",", ":"))

    t.write_mode(new_mode, reason, actor)

    log_entry = {
        "switched_at": datetime.now(timezone.utc).isoformat(),
        "from_mode": current, "to_mode": new_mode,
        "reason": reason, "actor": actor,
        "force_closed": closed,
    }
    log_path = os.path.join(_REPO, "data", "mode_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    print(f"MODE SWITCHED {current} -> {new_mode} by {actor}: {reason}")
    print(f"force-closed {len(closed)} position(s): {closed}")


if __name__ == "__main__":
    main()
