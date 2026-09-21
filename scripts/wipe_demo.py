"""Wipe all paper/demo history and re-base the account on LIVE balance.

Owner directive 2026-09-21: the ledger was polluted by paper-era trades and
five phantom closes (exit price 0.0, ~-150 fake USDT) from the 2026-09-19
entry-0 bug. Everything demo/paper must go; the live Bitget equity (read
by the sovereign engine every tick) is the only balance truth.

Idempotent: safe to run repeatedly. Deletes:
  data/trades.jsonl, data/trades.db, data/trades_ui.json      (paper ledgers)
  data/manual_trades.jsonl, data/manual_manager.json          (stale adoptions)
  data/manual_manager_log.jsonl, data/circuit_breaker_log.jsonl
Resets state/state.json:
  trades -> []
  account -> balance/peak = current live equity snapshot, session rebased now,
            mode preserved (LIVE), risk flags cleared
  per-pair paper junk (last_reversal_at eq/u/n/hb/pk snapshots) -> fresh
  sovereign sv: trading_env (DEMO_FALLBACK) removed, machine state preserved

The engine overwrites account.balance with fresh live equity on the next
tick (live_mode probe), so balance truth is the wallet, not this file.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(_REPO, "state", "state.json")
DATA = os.path.join(_REPO, "data")

WIPE_FILES = [
    "trades.jsonl",
    "trades.db",
    "trades_ui.json",
    "manual_trades.jsonl",
    "manual_manager.json",
    "manual_manager_log.jsonl",
    "circuit_breaker_log.jsonl",
]

def main(actor="wipe_demo.py"):
    now = datetime.now(timezone.utc).isoformat()
    s = json.load(open(STATE))
    acct = s.setdefault("account", {})
    # fresh live equity: per-pair sovereign probe (written every tick) beats
    # the account block, which froze on 2026-09-19 before the 09-21 fix.
    eq = 0.0
    for ps in (s.get("pairs") or {}).values():
        v = (ps.get("sovereign") or {}).get("last_live_equity")
        try:
            eq = max(eq, float(v or 0))
        except (TypeError, ValueError):
            pass
    if eq <= 0:
        try:
            eq = float(acct.get("live_equity") or 0)
        except (TypeError, ValueError):
            eq = 0.0
    if eq <= 0:
        eq = float(acct.get("balance") or 0)
    if eq <= 0:
        print("refusing to wipe: no live equity snapshot found")
        sys.exit(1)

    wiped = []
    for f in WIPE_FILES:
        p = os.path.join(DATA, f)
        if os.path.exists(p):
            if f.endswith(".db"):
                os.remove(p)                      # immutable log: full delete
            else:
                open(p, "w").close()              # empty file, keep REST contract
            wiped.append(f)

    # fresh trades_ui.json contract (chart frontend expects the shape)
    ui = {"trades": [], "zones": [], "exported_at": now}
    json.dump(ui, open(os.path.join(DATA, "trades_ui.json"), "w"), indent=2)

    n_trades = len(s.get("trades") or [])
    s["trades"] = []
    for k in ("circuit_breaker_active", "circuit_breaker_tripped_at",
              "circuit_breaker_streak", "circuit_breaker_session"):
        acct.pop(k, None)      # tripped by the phantom 09-19 losses — wiped
    acct.update({
        "balance": eq,
        "peak_balance": eq,
        "session_start_balance": eq,
        "session_start_at": now,
        "session_note": f"Owner wipe 2026-09-21: all paper/demo history purged; "
                        f"live Bitget equity is the only balance truth",
        "status": "running",
        "last_tick_at": now,
        "wipe_at": now,
        "wipe_by": actor,
    })
    acct.pop("mode_set_at", None)  # set fresh below if mode survives
    mode = (acct.get("mode") or "PAPER").upper()
    acct["mode"] = mode
    acct["mode_set_at"] = now
    acct["mode_set_reason"] = "preserved across owner wipe"
    acct["mode_set_by"] = actor

    for pair, ps in (s.get("pairs") or {}).items():
        ps.pop("_riskoff_day", None)
        ps.pop("_day_start_balance", None)
        ps["last_reversal_at"] = json.dumps({"eq": eq, "u": 0.0, "n": 0,
                                             "hb": now, "pk": eq})
        sv = ps.get("sovereign") or {}
        sv.pop("trading_env", None)          # never demo again
        ps["position_open"] = False
        ps["side"] = "none"

    json.dump(s, open(STATE, "w"), indent=2)
    print(f"wiped {len(wiped)} data files; purged {n_trades} ledger trades; "
          f"account rebased at ${eq:.2f} live equity; mode={mode}")

if __name__ == "__main__":
    main(actor=sys.argv[1] if len(sys.argv) > 1 else "wipe_demo.py")
