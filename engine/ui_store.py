"""UI trade store (owner spec 2026-09-15): SQLite trades.db + chart JSON export.

Immutable execution log for the Visual Chart & Historical Execution Overlay
System. Every open and close writes here; data/trades_ui.json (the REST
payload contract, section 5 of the spec) is exported each tick for the
GitHub-Pages chart frontend.

NEVER raises into the engines — every entry point is wrapped. The DB is a
UI/audit convenience; the canonical ledger remains data/trades.jsonl.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TRADES_DB", os.path.join(_REPO, "data", "trades.db"))
UI_JSON = os.path.join(_REPO, "data", "trades_ui.json")

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id    TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    direction   TEXT CHECK(direction IN ('LONG', 'SHORT')) NOT NULL,
    entry_time  INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    tp_price    REAL NOT NULL,
    sl_price    REAL NOT NULL,
    exit_time   INTEGER,
    exit_price  REAL,
    status      TEXT NOT NULL,
    reason_entry TEXT,
    reason_exit  TEXT,
    fvg_top     REAL,
    fvg_bottom  REAL,
    sweep_price REAL,
    net_pnl     REAL,
    engine      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades(symbol, entry_time);
"""


def _iso_to_epoch_s(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return None


def _con():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.executescript(_SCHEMA)
    return con


def _pair_fmt(symbol):
    """NEARUSDT -> NEAR/USDT (UI convention). Best-effort USDT/USDC split."""
    for q in ("USDT", "USDC"):
        if symbol.endswith(q):
            return symbol[:-len(q)] + "/" + q
    return symbol


def new_trade_id(symbol, opened_at_iso):
    """NEAR/USDT-20260915-001 per the spec. Day-scoped sequence per symbol."""
    try:
        day = datetime.fromisoformat(opened_at_iso).strftime("%Y%m%d")
    except (ValueError, TypeError):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"{_pair_fmt(symbol).replace('/', '-')}-{day}"
    with _LOCK, _con() as con:
        n = con.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_id LIKE ?", (base + "-%",)
        ).fetchone()[0]
        return f"{base}-{n + 1:03d}"


def record_open(trade_id, symbol, side, opened_at_iso, entry, tp, sl,
                reason_entry, fvg_top=None, fvg_bottom=None, sweep_price=None,
                engine="sovereign-v6"):
    try:
        with _LOCK, _con() as con:
            con.execute(
                """INSERT OR REPLACE INTO trades
                   (trade_id, symbol, direction, entry_time, entry_price,
                    tp_price, sl_price, status, reason_entry, fvg_top,
                    fvg_bottom, sweep_price, engine)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_id, symbol, "LONG" if side == "long" else "SHORT",
                 _iso_to_epoch_s(opened_at_iso), entry, tp, sl, "OPEN",
                 reason_entry, fvg_top, fvg_bottom, sweep_price, engine))
    except Exception:
        pass


def record_close(trade_id, closed_at_iso, exit_px, status, reason_exit, net_pnl=None):
    try:
        with _LOCK, _con() as con:
            con.execute(
                """UPDATE trades SET exit_time=?, exit_price=?, status=?,
                   reason_exit=?, net_pnl=? WHERE trade_id=?""",
                (_iso_to_epoch_s(closed_at_iso), exit_px, status, reason_exit,
                 net_pnl, trade_id))
    except Exception:
        pass


def _reason_exit_of(reason):
    return "MAKER_TP_FILLED" if reason == "TP" else "SL_TAKER_STOP"


def record_trade(tr):
    """Hook for the sync() choke point: every CLOSED trade from every engine
    (v5 + sovereign) lands here. Sovereign rows were opened via record_open —
    close them; v5 rows get inserted closed (structural fields NULL)."""
    try:
        tid = tr.get("trade_id")
        reason = tr.get("reason")
        side = (tr.get("side") or "").upper()
        opened = _iso_to_epoch_s(tr.get("opened_at"))
        closed = _iso_to_epoch_s(tr.get("closed_at"))
        if reason in ("TP", "SL"):
            status = "WIN_TP_HIT" if reason == "TP" else "LOSS_SL_HIT"
        else:
            status = "WIN_TP_HIT" if (tr.get("net_pnl") or 0) > 0 else "LOSS_SL_HIT"
        if tid:
            cur = record_close(tid, tr.get("closed_at"), tr.get("exit_price"),
                               status, _reason_exit_of(reason), tr.get("net_pnl"))
        # Owner 2026-09-16: v5 trades record TP distance (tp_frac) and the
        # locked kill switch (hard_sl) as fractions, not prices — derive the
        # prices so the chart can draw the TP zone and kill-switch line.
        _entry = tr.get("entry_price") or 0
        _long = side == "LONG"
        _tp = tr.get("tp_price") or 0
        if (not _tp) and _entry and tr.get("tp_frac"):
            _tp = round(_entry * (1 + (tr["tp_frac"] if _long else -tr["tp_frac"])), 6)
        _sl = tr.get("sl_price") or 0
        if (not _sl) and _entry and tr.get("hard_sl"):
            _sl = round(_entry * (1 - (tr["hard_sl"] if _long else -tr["hard_sl"])), 6)
        tr = {**tr, "tp_price": _tp, "sl_price": _sl}
        if not tid:
            with _LOCK, _con() as con:
                exists = con.execute(
                    "SELECT 1 FROM trades WHERE trade_id=?",
                    (tid,)).fetchone()
                if exists:
                    return
                con.execute(
                    """INSERT OR IGNORE INTO trades
                       (trade_id, symbol, direction, entry_time, entry_price,
                        tp_price, sl_price, exit_time, exit_price, status,
                        reason_entry, reason_exit, net_pnl, engine)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tr.get("trade_id") or _fallback_id(tr),
                     tr.get("pair"), side if side in ("LONG", "SHORT") else "LONG",
                     opened, tr.get("entry_price"), tr.get("tp_price") or 0,
                     tr.get("sl_price") or 0, closed, tr.get("exit_price"),
                     status, tr.get("reason_entry") or f"{tr.get('engine') or 'v5'} entry",
                     _reason_exit_of(reason) if reason else "TIME_STOP",
                     tr.get("net_pnl"), tr.get("engine") or "v5"))
    except Exception:
        pass


def _fallback_id(tr):
    return f"{tr.get('pair')}-{tr.get('opened_at')}-{tr.get('side')}"


def export_ui_json():
    """Section-5 REST payload, keyed by pair, written for the chart frontend."""
    try:
        with _LOCK, _con() as con:
            rows = con.execute(
                """SELECT trade_id, symbol, direction, entry_time, entry_price,
                          tp_price, sl_price, exit_time, exit_price, status,
                          reason_entry, reason_exit, fvg_top, fvg_bottom,
                          sweep_price, net_pnl, engine
                   FROM trades ORDER BY entry_time""").fetchall()
        out = {}
        for r in rows:
            (tid, symbol, direction, entry_time, entry_price, tp, sl,
             exit_time, exit_price, status, reason_entry, reason_exit,
             fvg_top, fvg_bottom, sweep_price, net_pnl, engine) = r
            out.setdefault(_pair_fmt(symbol), []).append({
                "trade_id": tid, "direction": direction,
                "entry_time": entry_time, "entry_price": entry_price,
                "tp_price": tp, "sl_price": sl,
                "exit_time": exit_time, "exit_price": exit_price,
                "status": status,
                "reason_entry": reason_entry, "reason_exit": reason_exit,
                "fvg_top": fvg_top, "fvg_bottom": fvg_bottom,
                "sweep_price": sweep_price, "net_pnl": net_pnl,
                "engine": engine,
            })
        payload = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "symbols": out,
        }
        os.makedirs(os.path.dirname(UI_JSON), exist_ok=True)
        with open(UI_JSON, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        return payload
    except Exception:
        return None


def api_trades(symbol):
    """GET /api/trades?symbol=NEAR/USDT — exact spec section-5 response."""
    try:
        sym = symbol.upper().replace("/", "")
        with _LOCK, _con() as con:
            rows = con.execute(
                """SELECT trade_id, symbol, direction, entry_time, entry_price,
                          tp_price, sl_price, exit_time, exit_price, status,
                          reason_entry, reason_exit, fvg_top, fvg_bottom,
                          sweep_price
                   FROM trades WHERE symbol=? ORDER BY entry_time""",
                (sym,)).fetchall()
        return {
            "symbol": _pair_fmt(sym),
            "trades": [{
                "trade_id": r[0], "direction": r[2], "entry_time": r[3],
                "entry_price": r[4], "tp_price": r[5], "sl_price": r[6],
                "exit_time": r[7], "exit_price": r[8], "status": r[9],
                "reason_entry": r[10], "reason_exit": r[11],
                "fvg_top": r[12], "fvg_bottom": r[13], "sweep_price": r[14],
            } for r in rows],
        }
    except Exception:
        return {"symbol": _pair_fmt(symbol), "trades": []}
