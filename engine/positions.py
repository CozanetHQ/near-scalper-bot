"""Position Manager — serialization + trade-state analytics.

The compact position format survives the sync endpoint's schema whitelist
(the last_error smuggling hack); the same keys must never change without a
round-trip probe. trade-state classification implements owner principle 2:
trade failure != target failure.
"""

import json

def serialize_positions(positions, max_positions, default_symbol):
    """Store the positions list as compact JSON inside the legacy `last_error`
    string field — the ONLY writable field proven to round-trip the sync
    endpoint's schema whitelist at full length. If you change this format,
    probe the round-trip first."""
    compact = [
        {"s": p["side"], "e": p["entry_price"], "t": p["tp_price"],
         "n": p["notional"], "m": p["margin"], "o": p["opened_at"],
         "a": bool(p.get("aligned")), "pr": p.get("pair") or default_symbol,
         "mf": round(p.get("mfe_frac") or 0.0, 6),
         "me": round(p.get("mae_frac") or 0.0, 6),
         "sc": p.get("advisory_score"), "wr": p.get("wall_ratio")}
        for p in positions[:max_positions]
    ]
    return {"last_error": json.dumps(compact, separators=(",", ":"))}


def parse_positions(state, default_symbol=None):
    """Rebuild the positions list from the JSON in `last_error`."""
    raw = state.get("last_error") or ""
    if not isinstance(raw, str) or not raw.startswith("["):
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for p in data:
        try:
            if p.get("s") in ("long", "short"):
                out.append({
                    "pair": p.get("pr") or default_symbol or "UNKNOWN",
                    "side": p["s"],
                    "entry_price": float(p["e"]),
                    "tp_price": float(p["t"]),
                    "notional": float(p["n"]),
                    "margin": float(p["m"]),
                    "opened_at": p["o"],
                    "aligned": bool(p.get("a", False)),
                    "mfe_frac": float(p.get("mf") or 0.0),
                    "mae_frac": float(p.get("me") or 0.0),
                    "advisory_score": p.get("sc"), "wall_ratio": p.get("wr"),
                })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def classify_trade_state(reason, mfe_frac, tp_frac):
    """Owner spec principle 2: trade failure (immediately wrong) and target
    failure (moved substantially toward TP, then failed) are DIFFERENT
    situations and get different management downstream."""
    if reason == "TP":
        return "target_success"
    if tp_frac > 0 and mfe_frac >= 0.5 * tp_frac:
        return "target_failure"
    if tp_frac > 0 and mfe_frac < 0.25 * tp_frac:
        return "trade_failure"
    return "mixed"
