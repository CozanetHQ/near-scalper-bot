"""Bitget LIVE futures executor for the Sovereign v6 engine (owner 2026-09-15).

Gates: LIVE_TRADING=1 AND the pair is in LIVE_PAIRS AND Bitget API keys are
present as env vars. Anything missing -> the bot stays paper. v5 pairs are
NEVER live through this module.

Safety model:
  - Entry: POST_ONLY limit at the CHOCH retest level (no market chasing).
  - TP/SL ride WITH the order (presetStopSurplusPrice / presetStopLossPrice):
    the exchange closes the position even if GitHub Actions dies mid-trade.
    TP is a maker reduce-only limit at the 1:2 level; SL is a stop market.
  - Expiry: the tick cancels the resting order after 3h (no orphan orders).
  - Sizing: 1.5% of REAL account equity, notional capped by LEV_CAP and
    LIVE_MAX_NOTIONAL (hard ceiling, default $50), rounded to the
    contract's volume precision, rejected below Bitget's minimum.
  - Reconciliation: every tick the exchange is the source of truth —
    orphan orders/positions from a crashed run are adopted, closed
    positions are booked from real fills.

Demo staging: BITGET_DEMO=1 adds the demo header so the exact same code
runs against Bitget demo funds first (owner verifies in the Bitget app
before LIVE_TRADING flips to real money).
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

API = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
MARGIN_COIN = "USDT"
LEV_CAP = 10
LIVE_MAX_NOTIONAL = float(os.environ.get("LIVE_MAX_NOTIONAL", "50"))   # hard $ ceiling
LIVE_MIN_NOTIONAL = float(os.environ.get("LIVE_MIN_NOTIONAL", "5"))    # Bitget minimum
# OWNER 2026-09-17 (voice, verbatim intent): the CASH committed to an entry
# is $3 at 10x leverage. Not the trade size — the cash. If leverage changes
# the notional (say $20), he doesn't care; the constant is the $3 margin.
LIVE_MARGIN_USD = float(os.environ.get("LIVE_MARGIN_USD", "3.0"))

_KEYS = {
    "key": os.environ.get("BITGET_API_KEY", ""),
    "secret": os.environ.get("BITGET_SECRET", ""),
    "passphrase": os.environ.get("BITGET_PASSPHRASE", ""),
}
DEMO = os.environ.get("BITGET_DEMO", "1") == "1"
MIN_LIVE_EQUITY = float(os.environ.get("MIN_LIVE_EQUITY", "10"))
LIVE_RECHECK_MINUTES = float(os.environ.get("LIVE_RECHECK_MINUTES", "60"))


def keys_present():
    return all(_KEYS.values())


class BitgetError(RuntimeError):
    pass


class BitgetPrivate:
    """Signed Bitget v2 REST client (HMAC-SHA256 / base64).

    demo=None -> module default (BITGET_DEMO env). demo=True/False pins the
    environment per instance so the auto-fallback can hold a live client and
    a demo client at the same time.
    """

    def __init__(self, demo=None):
        self._contracts = {}
        self.demo = DEMO if demo is None else demo

    def _sign(self, ts, method, path, body):
        pre = f"{ts}{method}{path}{body}"
        mac = hmac.new(_KEYS["secret"].encode(), pre.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _req(self, method, path, params=None, body=None):
        if not keys_present():
            raise BitgetError("live trading keys missing")
        params = params or {}
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        body_str = json.dumps(body) if body else ""
        ts = str(int(time.time() * 1000))
        headers = {
            "ACCESS-KEY": _KEYS["key"],
            "ACCESS-SIGN": self._sign(ts, method.upper(), path, body_str),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": _KEYS["passphrase"],
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        if self.demo:
            headers["pap"] = "1"   # Bitget demo trading
        url = API + path
        req = urllib.request.Request(url, data=(body_str.encode() if body_str else None),
                                     headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode())
        if data.get("code") != "00000":
            raise BitgetError(f"bitget {path}: {data.get('code')} {data.get('msg')}")
        return data.get("data")

    # ── account / instruments ──
    def account_equity(self):
        """Total USDT equity of the futures account (sizing base)."""
        rows = self._req("GET", "/api/v2/mix/account/accounts", {"productType": PRODUCT})
        for r in rows or []:
            if r.get("marginCoin") == MARGIN_COIN:
                return float(r.get("accountEquity") or r.get("usdtEquity") or 0)
        raise BitgetError("no USDT futures account found")

    def contract_info(self, symbol):
        if symbol in self._contracts:
            return self._contracts[symbol]
        rows = self._req("GET", "/api/v2/mix/market/contracts",
                        {"productType": PRODUCT, "symbol": symbol})
        r = (rows or [{}])[0]
        # verified against the live endpoint (2026-09-15): USDT-FUTURES uses
        # sizeMultiplier (base qty step) + minTradeNum + minTradeUSDT;
        # prices accept 4 decimals (confirmed via live order book depth)
        info = {
            "size_step": float(r.get("sizeMultiplier") or 1),
            "min_size": float(r.get("minTradeNum") or 1),
            "min_usdt": float(r.get("minTradeUSDT") or 5),
            "price_dp": 4,
        }
        self._contracts[symbol] = info
        return info

    def set_leverage(self, symbol, leverage):
        self._req("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": symbol, "productType": PRODUCT,
            "marginMode": "isolated", "leverage": str(leverage),
        })

    # ── orders ──
    def place_entry_limit(self, symbol, side, qty, price, sl, tp):
        """POST_ONLY retest limit, open side, exchange-side TP/SL preset."""
        data = self._req("POST", "/api/v2/mix/order/place-order", body={
            "symbol": symbol, "productType": PRODUCT,
            "marginMode": "isolated", "marginCoin": MARGIN_COIN,
            "size": self._fmt_size(symbol, qty),
            "side": "buy" if side == "long" else "sell",
            "tradeSide": "open",
            "orderType": "limit",
            "force": "post_only",
            "price": self._fmt_price(symbol, price),
            "presetStopSurplusPrice": self._fmt_price(symbol, tp),
            "presetStopLossPrice": self._fmt_price(symbol, sl),
        })
        return (data or {}).get("orderId")

    def cancel_order(self, symbol, order_id):
        self._req("POST", "/api/v2/mix/order/cancel-order", body={
            "symbol": symbol, "productType": PRODUCT, "marginCoin": MARGIN_COIN,
            "orderId": order_id,
        })

    def order_detail(self, symbol, order_id):
        rows = self._req("GET", "/api/v2/mix/order/detail",
                         {"symbol": symbol, "productType": PRODUCT, "orderId": order_id})
        r = (rows or [{}])
        r = r[0] if isinstance(r, list) else r
        return {
            "state": (r.get("state") or "").lower(),          # new/live -> live, filled, canceled
            "filled_size": float(r.get("accBaseAmount") or 0),
            "avg_price": float(r.get("priceAvg") or 0),
            "price": float(r.get("price") or 0),
            "sl": float(r.get("presetStopLossPrice") or 0),
            "tp": float(r.get("presetStopSurplusPrice") or 0),
        }

    # ── positions ──
    def position(self, symbol):
        rows = self._req("GET", "/api/v2/mix/position/single-position", {
            "productType": PRODUCT, "marginCoin": MARGIN_COIN, "symbol": symbol,
        })
        for r in rows or []:
            total = abs(float(r.get("total") or 0))
            if total > 0:
                side = "long" if float(r.get("total")) > 0 else "short"
                return {
                    "side": side, "size": total,
                    "entry": float(r.get("avgPrice") or 0),
                    "leverage": int(float(r.get("leverage") or 10)),
                    "unrealized_pl": float(r.get("unrealizedPL") or 0),
                }
        return None

    def closing_fill(self, symbol, opened_after_ms):
        """Most recent filled CLOSE order after a timestamp -> exit price/reason."""
        rows = self._req("GET", "/api/v2/mix/order/orders-history", {
            "symbol": symbol, "productType": PRODUCT,
        })
        best = None
        for r in rows or []:
            if (r.get("tradeSide") or "") != "close":
                continue
            ts = int(r.get("cts") or r.get("uTime") or 0)
            if ts < opened_after_ms:
                continue
            if (r.get("state") or "") != "filled":
                continue
            if best is None or ts > best["ts"]:
                best = {"ts": ts, "price": float(r.get("priceAvg") or 0),
                        "size": float(r.get("accBaseAmount") or 0),
                        "order_type": (r.get("orderType") or "").lower()}
        # plan/stop orders (the SL) live in the plan endpoint
        if best is None or best["price"] == 0:
            try:
                prows = self._req("GET", "/api/v2/mix/order/orders-plan", {
                    "symbol": symbol, "productType": PRODUCT,
                    "planType": "profit_loss",
                })
                for r in (prows or {}).get("entrustedList", []) or []:
                    ts = int(r.get("uTime") or 0)
                    if ts < opened_after_ms or (r.get("state") or "") != "filled":
                        continue
                    if best is None or ts > best["ts"]:
                        best = {"ts": ts, "price": float(r.get("triggerPrice") or r.get("price") or 0),
                                "size": float(r.get("size") or 0),
                                "order_type": (r.get("triggerType") or "").lower()}
            except Exception:
                pass
        return best

    def pending_orders(self, symbol):
        rows = self._req("GET", "/api/v2/mix/order/orders-pending", {
            "symbol": symbol, "productType": PRODUCT,
        })
        return rows or []

    # ── formatting ──
    def round_qty(self, symbol, qty):
        step = self.contract_info(symbol)["size_step"]
        return max(int(qty / step) * step, step)

    def _fmt_size(self, symbol, qty):
        step = self.contract_info(symbol)["size_step"]
        if step == int(step):
            return str(int(qty))
        return f"{qty:g}"

    def _fmt_price(self, symbol, price):
        return f"{round(price, 4):.4f}"


_CLIENTS = {}

def client(demo=None):
    """Cached client per environment (None=default, True=demo, False=live)."""
    key = None if demo is None else bool(demo)
    if key not in _CLIENTS:
        _CLIENTS[key] = BitgetPrivate(demo=demo)
    return _CLIENTS[key]
