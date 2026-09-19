"""Manual Position Auto-Management layer (owner spec 2026-09-18).

When the owner opens a futures position on Bitget from his phone, this module
detects it, classifies it as a MANUAL position, computes a TP with the EXISTING
production TP engine (sovereign: TP = entry +/- RR * ATR14(2m)), places a
reduce-only TP limit on Bitget, and monitors until the position disappears.

HARD RULES (owner spec):
  - This module NEVER touches the strategy: no entries, no exits of bot
    positions, no balance/ledger writes, no stats contamination. Manual trades
    are booked to data/manual_trades.jsonl ONLY.
  - It never infers WHY the owner entered. It only manages the exit.
  - It never closes a manual position because of a strategy signal, never
    opens positions, never changes leverage, never increases size.
  - Ownership states: BOT_MANAGED (engine's own), MANUAL_MANAGED (adopted,
    TP placed), MANUAL_IGNORED (existing TP found / ambiguous — read-only).
  - Idempotent: one TP order per adopted position, tracked by order id and
    re-verified against the exchange every cycle.

Config (data/manual_manager.json "config", env as fallback default):
  enabled  — AUTO_MANAGE_MANUAL_POSITIONS. Default OFF: detect + alert only.
  kill     — MANUAL_MANAGER_KILL_SWITCH. When ON: read-only monitoring +
             alerts only (no new TP orders, no modification of any position).

Store lives in data/manual_manager.json so the engine's state.json is NEVER
written by this layer (zero interference with the trading machine's state).
Both files ride the existing "Commit engine state" step (`git add state data`).
"""
import json
import os
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_FILE = os.path.join(REPO, "data", "manual_manager.json")
TRADES_FILE = os.path.join(REPO, "data", "manual_trades.jsonl")

_ERROR_ALERT_COOLDOWN_S = 3600  # one API-error alert per hour (no spam)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── config + store ───────────────────────────────────────────────────────────
def _default_store():
    return {"config": {}, "positions": {}, "closed": []}


def load_store():
    try:
        with open(STORE_FILE) as f:
            s = json.load(f)
        s.setdefault("config", {})
        s.setdefault("positions", {})
        s.setdefault("closed", [])
        return s
    except (OSError, ValueError):
        return _default_store()


def save_store(store):
    os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, STORE_FILE)


def resolve_config(store):
    """state-file config wins; env vars are the fallback default. Default OFF."""
    cfg = store.get("config") or {}
    enabled = cfg.get("enabled")
    if enabled is None:
        enabled = os.environ.get("AUTO_MANAGE_MANUAL_POSITIONS", "0") == "1"
    kill = cfg.get("kill")
    if kill is None:
        kill = os.environ.get("MANUAL_MANAGER_KILL_SWITCH", "0") == "1"
    return bool(enabled), bool(kill)


# ── ownership classification ─────────────────────────────────────────────────
def _bot_footprint(T):
    """What the strategy currently owns on the REAL exchange, per (symbol, side):
    list of dicts {symbol, side, qty, pending_order_id?} from state.json."""
    master = T.load_master()
    out = []
    for symbol, ps in (master.get("pairs") or {}).items():
        if not (ps.get("position_open") or ps.get("sovereign")):
            continue
        sv = ps.get("sovereign") or {}
        pos = sv.get("pos")
        if pos and ps.get("position_open"):
            out.append({"symbol": symbol, "side": pos["side"],
                        "qty": float(pos.get("qty") or 0), "kind": "pos"})
        pend = sv.get("pending")
        if pend and pend.get("order_id"):
            out.append({"symbol": symbol, "side": pend["side"],
                        "qty": float(pend.get("qty") or 0),
                        "kind": "pending", "order_id": pend["order_id"],
                        "tp": pend.get("tp")})
    return out


def classify(row, bot_fp):
    """Decide who owns an exchange position row.
    Returns (status, note). Conservative: ambiguity -> MANUAL (never steal an
    owner position into the bot, never silently manage an ambiguous one)."""
    sym, side = row["symbol"], row["side"]
    same_side = [b for b in bot_fp if b["symbol"] == sym and b["side"] == side]
    for b in same_side:
        if b["kind"] == "pending":
            # a filled pending entry not yet reconciled: within 15% of qty it is
            # the bot's; leave it alone for the sovereign reconcile to adopt
            if b["qty"] and abs(row["size"] - b["qty"]) <= max(b["qty"] * 0.15, 1e-12):
                return "BOT_MANAGED", "matches bot pending entry (pre-reconcile)"
            continue
        if b["kind"] == "pos":
            # bot position exists: the exchange size up to bot qty is the bot's.
            if b["qty"] and row["size"] <= b["qty"] * 1.05:
                return "BOT_MANAGED", "matches bot open position"
            return "MANUAL_MANAGED", (
                f"bot pos qty {b['qty']} but exchange {row['size']} — excess treated manual")
    # opposite-side rows on a bot symbol, and any position on non-bot symbols
    return "MANUAL_MANAGED", "no matching bot footprint"


# ── TP engine (EXACT reuse of the production sovereign formula) ─────────────
def compute_tp(T, symbol, side, entry):
    """Production TP path (engine/sovereign.py _live_place): 
    dp = ATR14(2m)/close * entry ; tp = entry +/- RR * dp. Single source of
    truth: imports atr_series and RR from sovereign. Raises on data problems."""
    # 2026-09-19 fix: Bitget has NO native 2m granularity — fetch_candles("2m")
    # returned HTTP 400 EVERY time (the exact "TP compute failed: HTTP Error
    # 400" on the owner's tracked SOL short, so no manual TP could ever be
    # computed). Mirror the production walk exactly: fetch 1m, drop the
    # in-progress candle, resample to 2m locally, ATR14 on the resampled series.
    from engine.sovereign import atr_series, RR, resample_2m
    candles = resample_2m(T.fetch_candles("1m", 600, symbol)[:-1])
    series = atr_series(candles, 14)
    atr = next((a for a in reversed(series) if a is not None), None)
    if not atr or not candles or not candles[-1]["close"]:
        raise RuntimeError("ATR14(2m) unavailable")
    atr_frac = atr / candles[-1]["close"]
    dp = atr_frac * entry
    tp = entry + RR * dp if side == "long" else entry - RR * dp
    if tp <= 0:
        raise RuntimeError(f"computed TP {tp:.6g} non-positive")
    return round(tp, 4), atr_frac


# ── existing-TP detection (never overwrite the owner's own exit) ─────────────
def _existing_tp(cli, symbol, pos_side, entry=None, tp_hint=None):
    """A resting TP for THIS position (side-aware, owner spec):
      - a close-side limit on the correct side (sell closes long / buy closes
        short) — the bot never places standalone close orders (its TP rides
        with the entry as a preset), so any such order belongs to the owner;
      - a profit-side profit/loss plan (trigger on the PROFIT side of entry;
        a stop-loss plan does NOT block adoption — it is not a TP).
    Returns a descriptor or None."""
    close_side = "sell" if pos_side == "long" else "buy"
    found = None
    for o in cli.pending_orders(symbol) or []:
        if (o.get("tradeSide") or "") != "close":
            continue
        o_side = (o.get("side") or "").lower()
        if o_side and o_side != close_side:
            continue    # belongs to the OTHER hedge leg, not this one
        found = {"type": "close-limit", "order_id": o.get("orderId"),
                 "price": float(o.get("price") or 0)}
        if tp_hint and found["price"] and abs(found["price"] - tp_hint) / tp_hint < 0.002:
            found["ours_probably"] = True   # crashed-after-submit recovery
        break
    if not found:
        try:
            rows = cli.orders_plan_profit_loss(symbol)
            for o in rows or []:
                if (o.get("state") or "") in ("filled", "canceled"):
                    continue
                trig = float(o.get("triggerPrice") or 0)
                if not trig or not entry:
                    continue
                profit_side = trig > entry if pos_side == "long" else trig < entry
                if not profit_side:
                    continue    # an SL plan, not a TP
                found = {"type": "plan", "order_id": o.get("orderId"), "price": trig}
                break
        except Exception:
            pass
    return found


# ── alerts ──────────────────────────────────────────────────────────────────
def _fmt_side(side):
    return "LONG" if side == "long" else "SHORT"


def _alert(T, text):
    try:
        T.send_telegram(f"🤖 [manual-manager] {text}")
    except Exception:
        pass


def _alert_detected(T, row):
    _alert(T, f"🔵 MANUAL POSITION DETECTED\n{row['symbol']} {_fmt_side(row['side'])}\n"
              f"Entry: ${row['entry']:.6g}\nSize: {row['size']:g}\n"
              f"Leverage: {row.get('leverage', '?')}x")


def _alert_adopted(T, rec):
    _alert(T, f"🟢 MANUAL POSITION ADOPTED\n{rec['symbol']} {_fmt_side(rec['side'])}\n"
              f"Entry: ${rec['entry']:.6g}\nSize: {rec['size']:g}\n"
              f"Leverage: {rec.get('leverage', '?')}x\nTP: ${rec['tp_price']:.6g}\n"
              f"Expected TP: {rec.get('expected_tp_pct', 0):.2f}%")


# ── adoption ─────────────────────────────────────────────────────────────────
def _adopt(T, cli, row, note):
    """Safety gate + TP adoption for one fresh manual position. Returns the
    position record (status MANUAL_MANAGED or MANUAL_IGNORED)."""
    from engine import live as _L
    rec = {
        "symbol": row["symbol"], "side": row["side"], "entry": row["entry"],
        "size": row["size"], "leverage": row.get("leverage"),
        "margin_mode": row.get("margin_mode") or "isolated",
        "created_ms": row.get("created_ms"),
        "detected_at": _now_iso(), "status": None, "note": note,
        "tp_price": None, "tp_order_id": None, "atr_frac": None,
        "expected_tp_pct": None, "last_seen_size": row["size"],
    }

    # safety re-verification against the exchange (owner spec):
    fresh = cli.position(rec["symbol"])
    if fresh and fresh["side"] == rec["side"]:
        rec["size"] = fresh["size"]
        rec["entry"] = fresh["entry"] or rec["entry"]
        rec["leverage"] = fresh.get("leverage") or rec["leverage"]
    info = cli.contract_info(rec["symbol"])           # symbol exists + min size
    if rec["size"] < info["min_size"]:
        rec["status"] = "MANUAL_IGNORED"
        rec["note"] += "; below exchange minimum size"
        _alert(T, f"🟡 MANUAL POSITION IGNORED (below min size)\n"
                  f"{rec['symbol']} {_fmt_side(rec['side'])} size {rec['size']:g}")
        return rec

    # existing TP? -> read-only, never touch
    ex = _existing_tp(cli, rec["symbol"], rec["side"], entry=rec["entry"])
    if ex:
        rec["status"] = "MANUAL_IGNORED"
        rec["existing_tp"] = ex
        _alert(T, f"🟡 EXISTING TP DETECTED — left untouched\n{rec['symbol']} "
                  f"{_fmt_side(rec['side'])}\nTP order {ex.get('type')} @ "
                  f"${ex.get('price') or 0:.6g}")
        return rec

    # compute TP with the production engine
    try:
        tp, atr_frac = compute_tp(T, rec["symbol"], rec["side"], rec["entry"])
    except Exception as e:
        rec["status"] = "MANUAL_IGNORED"
        rec["note"] += f"; TP compute failed: {e}"
        _alert(T, f"🔴 MANUAL TP CALCULATION FAILED\n{rec['symbol']} — {e}")
        return rec
    rec["tp_price"] = tp
    rec["atr_frac"] = atr_frac
    rec["status"] = "MANUAL_MANAGED"
    dp = atr_frac * rec["entry"]
    rec["expected_tp_pct"] = (2.0 * dp / rec["entry"]) * 100   # RR * dp / entry
    return rec


def _place_tp(T, cli, rec):
    """Place (or re-place) the reduce-only TP limit. Idempotent via rec['tp_order_id']."""
    if rec.get("tp_order_id"):
        return  # already placed — duplicate protection
    qty = cli.round_qty(rec["symbol"], rec["size"])
    side = "sell" if rec["side"] == "long" else "buy"
    try:
        oid = cli.place_reduce_limit(rec["symbol"], side, qty, rec["tp_price"],
                                     rec.get("margin_mode"))
    except Exception as e:
        _alert(T, f"🔴 TP PLACEMENT FAILED\n{rec['symbol']} {_fmt_side(rec['side'])} "
                  f"@ ${rec['tp_price']:.6g} — {e}")
        return
    rec["tp_order_id"] = oid
    rec["tp_placed_at"] = _now_iso()
    _alert(T, f"✅ TP PLACED\n{rec['symbol']} {_fmt_side(rec['side'])} — reduce-only "
              f"limit {qty:g} @ ${rec['tp_price']:.6g} (order {oid})")


# ── close handling ───────────────────────────────────────────────────────────
def _close_position(T, cli, store, key, rec, reason):
    """Position gone from the exchange: book the manual trade (separate ledger),
    clean up our resting TP order if any, alert."""
    exit_price, order_type = None, None
    try:
        opened_after = int(rec.get("created_ms") or 0)
        fill = cli.closing_fill(rec["symbol"], opened_after)
        if fill:
            exit_price, order_type = fill["price"], fill.get("order_type")
    except Exception:
        pass
    if exit_price is None:
        exit_price = rec.get("last_mark") or rec["entry"]
    diff = (exit_price - rec["entry"]) if rec["side"] == "long" else (rec["entry"] - exit_price)
    gross = diff * rec["size"]
    fees = rec["entry"] * rec["size"] * getattr(T, "FEE_RATE", 0.0) * 2
    net = gross - fees
    trade = {
        "symbol": rec["symbol"], "side": rec["side"], "entry_price": rec["entry"],
        "exit_price": exit_price, "size": rec["size"],
        "leverage": rec.get("leverage"), "margin_mode": rec.get("margin_mode"),
        "tp_price": rec.get("tp_price"), "tp_order_id": rec.get("tp_order_id"),
        "pnl_gross": round(gross, 6), "pnl_net_est": round(net, 6),
        "exit_reason": reason, "exit_order_type": order_type,
        "opened_at": rec.get("created_ms"), "closed_at": _now_iso(),
        "status_at_close": rec.get("status"),
        "manual": True,   # NEVER merged into strategy statistics
    }
    store["closed"].append(trade)
    try:
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(trade, separators=(",", ":")) + "\n")
    except OSError:
        pass
    # clean up our resting TP if it survived the close (reduce-only, but tidy)
    if rec.get("tp_order_id") and reason != "tp":
        try:
            cli.cancel_order(rec["symbol"], rec["tp_order_id"])
        except Exception:
            pass
    del store["positions"][key]
    emoji = "🟢" if net >= 0 else "🔴"
    _alert(T, f"{emoji} MANUAL POSITION CLOSED ({reason})\n{rec['symbol']} "
              f"{_fmt_side(rec['side'])}\nEntry: ${rec['entry']:.6g} → Exit: "
              f"${exit_price:.6g}\nSize: {rec['size']:g}\nPnL (gross): {gross:+.4f} USDT")


# ── main cycle ───────────────────────────────────────────────────────────────
def run_tick(T):
    """One manual-manager cycle. Called ONCE per tick loop from tick.py, fully
    guarded — a failure here must never interrupt the trading loop."""
    from engine import live as _L
    if not _L.keys_present():
        return
    store = load_store()
    enabled, kill = resolve_config(store)
    cli = _L.client(demo=False)   # manual positions are REAL, never demo

    try:
        rows = cli.all_positions()
    except Exception as e:
        ts = time.time()
        last = store.get("config").get("_api_err_alerted", 0) or 0
        if ts - last > _ERROR_ALERT_COOLDOWN_S:
            _alert(T, f"🔴 MANUAL MANAGER API ERROR — read-only this cycle: {str(e)[:120]}")
            store["config"]["_api_err_alerted"] = ts
        save_store(store)
        return

    bot_fp = _bot_footprint(T)
    seen_keys = set()

    for row in rows:
        key = f"{row['symbol']}:{row['side']}"
        if key in seen_keys:   # same symbol+side twice (crossed+isolated) — ambiguous
            continue
        seen_keys.add(key)
        try:
            status, note = classify(row, bot_fp)
        except Exception:
            continue
        if status == "BOT_MANAGED":
            continue   # engine owns it — zero interference

        rec = store["positions"].get(key)
        if rec is None:
            _alert_detected(T, row)
            rec = _adopt(T, cli, row, note)
            rec["key"] = key
            store["positions"][key] = rec
            if rec.get("status") == "MANUAL_MANAGED":
                _alert_adopted(T, rec)      # spec alert #2: adopted + TP calculated
            if rec.get("tp_price") and not rec.get("tp_order_id"):
                if enabled and not kill:
                    _place_tp(T, cli, rec)
                else:
                    why = "KILL SWITCH" if kill else "AUTO-MANAGE OFF"
                    _alert(T, f"⚪ TP NOT PLACED ({why})\n{rec['symbol']} "
                              f"{_fmt_side(rec['side'])} — would be "
                              f"${rec['tp_price']:.6g}")
        else:
            # already tracked: monitor for size changes and TP order survival
            if rec.get("status") != "MANUAL_IGNORED":
                if abs(row["size"] - rec.get("last_seen_size", row["size"])) > max(rec["last_seen_size"] * 0.01, 1e-12):
                    old = rec["last_seen_size"]
                    rec["last_seen_size"] = row["size"]
                    rec["size"] = row["size"]
                    _alert(T, f"🟠 MANUAL POSITION SIZE CHANGED\n{rec['symbol']} "
                              f"{_fmt_side(rec['side'])} — {old:g} → {row['size']:g}")
                    # re-place the TP for the new size
                    if rec.get("tp_order_id"):
                        try:
                            cli.cancel_order(rec["symbol"], rec["tp_order_id"])
                        except Exception:
                            pass
                        rec["tp_order_id"] = None
                    if rec.get("tp_price") and enabled and not kill:
                        _place_tp(T, cli, rec)
                elif rec.get("status") == "MANUAL_MANAGED" and not rec.get("tp_order_id") \
                        and enabled and not kill:
                    # crashed-after-submit recovery: bind a resting close order at
                    # our TP price instead of risking a duplicate
                    ex = _existing_tp(cli, rec["symbol"], rec["side"],
                                      entry=rec["entry"], tp_hint=rec.get("tp_price"))
                    if ex and ex.get("ours_probably"):
                        rec["tp_order_id"] = ex["order_id"]
                        _alert(T, f"✅ TP ORDER RE-BOUND after restart\n{rec['symbol']} "
                                  f"order {ex['order_id']}")
                    elif not ex:
                        _place_tp(T, cli, rec)
            rec["last_checked"] = _now_iso()

    # tracked positions that vanished -> closed
    for key, rec in list(store["positions"].items()):
        if key in seen_keys:
            continue
        try:
            reason = "manual/external close"
            if rec.get("tp_order_id"):
                still_resting = any(
                    str(o.get("orderId")) == str(rec["tp_order_id"])
                    for o in (cli.pending_orders(rec["symbol"]) or []))
                reason = "manual/external close" if still_resting else "tp"
            _close_position(T, cli, store, key, rec, reason)
        except Exception as e:
            _alert(T, f"🔴 MANUAL CLOSE RECONCILE FAILED {rec.get('symbol')}: {str(e)[:100]}")

    save_store(store)
