"""Unlock Trading (owner spec 2026-09-17, Master Architecture Sec 5.2).

Manually clears the losing-streak circuit breaker. Invoked ONLY by the
"Unlock Trading" GitHub Actions workflow — the bot never clears this flag
on its own (no timer, no session-block rollover, no balance recovery). Logs
every unlock to data/circuit_breaker_log.jsonl for the audit trail.
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
    reason = os.environ.get("UNLOCK_REASON") or ""
    actor = os.environ.get("UNLOCK_ACTOR") or "unknown"
    if not reason.strip():
        print("UNLOCK_REASON is required (audit trail)", file=sys.stderr)
        sys.exit(1)

    t = _load_tick()
    master = t.load_master()
    acct = master.setdefault("account", {})
    if not acct.get("circuit_breaker_active"):
        print("circuit breaker is not tripped — nothing to unlock")
        return

    tripped_at = acct.get("circuit_breaker_tripped_at")
    streak = acct.get("circuit_breaker_streak")
    acct["circuit_breaker_active"] = False
    acct["circuit_breaker_unlocked_at"] = datetime.now(timezone.utc).isoformat()
    acct["circuit_breaker_unlocked_by"] = actor
    acct["circuit_breaker_unlock_reason"] = reason
    os.makedirs(os.path.dirname(t.STATE_FILE), exist_ok=True)
    with open(t.STATE_FILE, "w") as f:
        json.dump(master, f, separators=(",", ":"))

    log_entry = {
        "unlocked_at": datetime.now(timezone.utc).isoformat(),
        "tripped_at": tripped_at, "streak": streak,
        "reason": reason, "actor": actor,
    }
    log_path = os.path.join(_REPO, "data", "circuit_breaker_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    print(f"CIRCUIT BREAKER UNLOCKED by {actor}: {reason} (was tripped at {tripped_at}, streak {streak})")


if __name__ == "__main__":
    main()
