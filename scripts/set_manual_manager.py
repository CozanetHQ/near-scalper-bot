"""Toggle the Manual Position Auto-Management layer (owner spec 2026-09-18).

The owner's authenticated toggle, mirroring scripts/set_mode.py:
invoked by the "Manual Manager Toggle" GitHub Actions workflow — running it
requires being logged into the CozanetHQ GitHub org. Also safe to run from
the maintainer sandbox (data-file edit only — NEVER touches engine code).

Contract:
  - Writes the config block of data/manual_manager.json:
      enabled  — AUTO_MANAGE_MANUAL_POSITIONS (OFF default: detect+alert only)
      kill     — MANUAL_MANAGER_KILL_SWITCH (read-only monitoring + alerts)
  - Every toggle is appended to data/manual_manager_log.jsonl (audit trail).

Never touches state/state.json, tick.py, or any strategy code.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_store():
    p = os.path.join(_REPO, "data", "manual_manager.json")
    try:
        with open(p) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("config", {})
    s.setdefault("positions", {})
    s.setdefault("closed", [])
    return s, p


def main():
    mode = (os.environ.get("SET_MANUAL_MANAGER") or "").strip().lower()
    if mode not in ("on", "off"):
        print("SET_MANUAL_MANAGER must be 'on' or 'off'", file=sys.stderr)
        sys.exit(1)
    kill = (os.environ.get("SET_MANUAL_KILL") or "").strip().lower()
    if kill not in ("", "on", "off"):
        print("SET_MANUAL_KILL must be 'on' or 'off'", file=sys.stderr)
        sys.exit(1)
    actor = os.environ.get("SET_MANUAL_ACTOR") or "unknown"
    reason = os.environ.get("SET_MANUAL_REASON") or ""

    store, path = _load_store()
    if mode == "off":
        store["config"]["enabled"] = False
    else:
        # flipping ON requires the kill switch to be explicitly considered:
        # refuse if the kill switch is active — safety conditions first.
        if store["config"].get("kill"):
            print("REFUSING: MANUAL_MANAGER_KILL_SWITCH is active — clear it "
                  "first (SET_MANUAL_KILL=off)", file=sys.stderr)
            sys.exit(2)
        store["config"]["enabled"] = True
    if kill:
        store["config"]["kill"] = (kill == "on")
        if kill == "on":
            store["config"]["enabled"] = store["config"].get("enabled", False)  # kill ≠ off switch

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(store, f, indent=2)

    entry = {
        "toggled_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor, "reason": reason,
        "enabled": store["config"].get("enabled"),
        "kill": store["config"].get("kill"),
    }
    logp = os.path.join(_REPO, "data", "manual_manager_log.jsonl")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    with open(logp, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    print(f"manual manager: enabled={entry['enabled']} kill={entry['kill']}")


if __name__ == "__main__":
    main()
