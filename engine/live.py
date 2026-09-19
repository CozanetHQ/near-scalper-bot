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
import urllib.error
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
    """Exchange-returned failure. `.code` = Bitget error code (e.g. '400014')
    so rejection alerts can name the exact code; None for transport errors."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


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
        """GET is retried on transient network faults (2026-09-19 fix: the
        live account probe / reconcile reads were single-attempt, so one
        "Connection reset by peer" aborted the whole tick for that pair —
        that was the actual live-trading blocker, not a Bitget rejection).
        POST (place/cancel/leverage) stays single-attempt, unchanged: an
        order-placement call must NEVER be blindly retried — a reset can
        happen after Bitget already accepted the order, and retrying could
        submit a duplicate. GETs are read-only and safe to retry."""
        if not keys_present():
            raise BitgetError("live trading keys missing")
        is_get = method.upper() == "GET"
        attempts = 3 if is_get else 1
        last_exc = None
        for attempt in range(attempts):
            try:
                return self._do_req(method, path, params, body)
            except BitgetError:
                raise   # exchange-returned error code — not transient, don't retry
            except Exception as e:
                last_exc = e
                if attempt < attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))   # 0.5s, 1s
                    continue
                raise
        raise last_exc

    def _do_req(self, method, path, params=None, body=None):
        params = params or {}
        req_path = path + ("?" + urllib.parse.urlencode(params) if params else "")
        body_str = json.dumps(body) if body else ""
        ts = str(int(time.time() * 1000))
        headers = {
            "ACCESS-KEY": _KEYS["key"],
            "ACCESS-SIGN": self._sign(ts, method.upper(), req_path, body_str),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": _KEYS["passphrase"],
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        if self.demo:
            headers["pap"] = "1"   # Bitget demo trading
        url = API + req_path
        req = urllib.request.Request(url, data=(body_str.encode() if body_str else None),
                                     headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            # 2026-09-19 fix: a non-2xx HTTP status (Bitget sometimes rejects
            # bad params this way, not just via a 200+error-code body) used
            # to propagate as urllib's bare "HTTP Error 400: Bad Request" —
            # the ACTUAL rejection reason in the response body was silently
            # discarded, so a real order rejection looked identical to a
            # transport failure. Read the body and surface the real code/msg.
            raw = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            try:
                body_data = json.loads(raw) if raw else {}
            except Exception:
                body_data = {}
            if 400 <= e.code < 500:
                # client error: retrying the identical request won't change
                # the outcome — surface as BitgetError (non-retryable), with
                # the real code/msg when Bitget's body provided one.
                if body_data.get("code"):
                    raise BitgetError(f"bitget {path}: {body_data.get('code')} "
                                      f"{body_data.get('msg')} (http {e.code})",
                                      code=body_data.get("code"))
                raise BitgetError(f"bitget {path}: http {e.code} {e.reason} — {raw[:300]}",
                                  code=str(e.code))
            # 5xx: infra-side, may well be transient — let the GET retry loop
            # in _req handle it (raising plain, not BitgetError, keeps it
            # retryable); re-raise as-is so the retry wrapper's generic
            # except-Exception branch catches and retries it.
            raise RuntimeError(f"bitget {path}: http {e.code} {e.reason} — {raw[:300]}")
        if data.get("code") != "00000":
            raise BitgetError(f"bitget {path}: {data.get('code')} {data.get('msg')}",
                              code=data.get("code"))
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
        # sizeMultiplier (base qty step) + minTradeNum + minTradeUSDT.
        # 2026-09-19 fix (owner-authorized): price precision is PER SYMBOL.
        # Bitget "pricePlace" = max price decimals (BTC=1, ETH=2, SOL=3,
        # NEAR=4, XRP=4), "priceEndStep" = tick mantissa (tick = endstep *
        # 10^-pricePlace). The old hardcoded 4dp produced off-tick TP/SL for
        # BTC/ETH/SOL, which Bitget rejects — entry/TP/SL must snap to the
        # symbol's actual tick.
        dp = int(float(r.get("pricePlace") or 4))
        info = {
            "size_step": float(r.get("sizeMultiplier") or 1),
            "min_size": float(r.get("minTradeNum") or 1),
            "min_usdt": float(r.get("minTradeUSDT") or 5),
            "price_dp": dp,
            "price_tick": float(r.get("priceEndStep") or 1) * (10 ** -dp),
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
                # 2026-09-19 fix: Bitget's single-position response field is
                # "averageOpenPrice" (or "openPriceAvg" on some API
                # versions) -- "avgPrice" isn't a field this endpoint
                # returns, so entry was always defaulting to 0 for any
                # orphan-adopted position (a manually-opened position the
                # bot detects but didn't place itself), which fed a 0
                # entry/margin/notional straight into sv["pos"].
                return {
                    "side": side, "size": total,
                    "entry": float(r.get("averageOpenPrice") or r.get("openPriceAvg")
                                   or r.get("avgPrice") or 0),
                    "leverage": int(float(r.get("leverage") or 10)),
                    "unrealized_pl": float(r.get("unrealizedPL") or 0),
                }
        return None

    def closing_fill(self, symbol, opened_after_ms):
        """Most recent filled CLOSE order after a timestamp -> exit price/reason."""
        rows = self._req("GET", "/api/v2/mix/order/orders-history", {
            "symbol": symbol, "productType": PRODUCT,
        })
        # 2026-09-18 fix: this endpoint's data is {"entrustedList": [...], "endId": ...}
        # (verified against Bitget docs), NOT a bare list — same shape as the
        # orders-plan fallback below, which already unwraps it correctly.
        # Iterating the raw dict was yielding its string keys ("entrustedList",
        # "endId") instead of order rows -> "'str' object has no attribute 'get'"
        # on every LIVE reconcile tick, spamming Telegram and skipping close
        # reconciliation for the whole tick (exception raised before block c).
        rows = (rows or {}).get("entrustedList", []) if isinstance(rows, dict) else (rows or [])
        best = None
        for r in rows:
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
        # 2026-09-18 fix: same wrapped-dict shape as orders-history above —
        # data is {"entrustedList": [...], "endId": ...}, not a bare list.
        if isinstance(rows, dict):
            return rows.get("entrustedList", []) or []
        return rows or []

    # ── manual-position manager endpoints (owner spec 2026-09-18; additive,
    # used ONLY by engine/manual_manager.py — no existing call path changes) ──
    def all_positions(self):
        """Every open USDT-FUTURES position on this account, one-way or hedge.
        Normalized rows: symbol/side/size/entry/leverage/unrealized_pl/
        margin_mode/created_ms."""
        rows = self._req("GET", "/api/v2/mix/position/all-position", {
            "productType": PRODUCT, "marginCoin": MARGIN_COIN,
        })
        out = []
        for r in rows or []:
            total = float(r.get("total") or 0)
            if total == 0:
                continue
            side = (r.get("holdSide") or "").lower()
            if side not in ("long", "short"):
                side = "long" if total > 0 else "short"
            out.append({
                "symbol": r.get("symbol"), "side": side, "size": abs(total),
                # 2026-09-19 fix (mirror of the single-position fix): the
                # all-position rows expose "averageOpenPrice"/"openPriceAvg",
                # not "avgPrice" -- manual positions were adopted with entry
                # 0.0, which broke the manual manager's TP compute
                # (non-positive TP) and any downstream per-unit math.
                "entry": float(r.get("averageOpenPrice") or r.get("openPriceAvg")
                               or r.get("avgPrice") or 0),
                "leverage": int(float(r.get("leverage") or 10)),
                "unrealized_pl": float(r.get("unrealizedPL") or 0),
                "margin_mode": (r.get("marginMode") or "isolated"),
                "created_ms": int(r.get("cTime") or 0),
            })
        return out

    def orders_plan_profit_loss(self, symbol):
        """Resting profit/loss plan (TP/SL trigger) orders for a symbol."""
        rows = self._req("GET", "/api/v2/mix/order/orders-plan", {
            "symbol": symbol, "productType": PRODUCT, "planType": "profit_loss",
        })
        if isinstance(rows, dict):
            return rows.get("entrustedList", []) or []
        return rows or []

    def place_reduce_limit(self, symbol, side, qty, price, margin_mode="isolated"):
        """Reduce-only GTC limit (a standing TP for an open position).
        side = 'sell' closes a long, 'buy' closes a short."""
        body = {
            "symbol": symbol, "productType": PRODUCT,
            "marginMode": "isolated" if (margin_mode or "isolated") == "isolated" else "crossed",
            "marginCoin": MARGIN_COIN,
            "size": self._fmt_size(symbol, qty),
            "side": side,
            "tradeSide": "close",
            "orderType": "limit",
            "force": "gtc",
            "reduceOnly": "YES",
            "price": self._fmt_price(symbol, price),
        }
        try:
            data = self._req("POST", "/api/v2/mix/order/place-order", body=body)
        except BitgetError:
            # some Bitget revision may reject reduceOnly alongside tradeSide
            body.pop("reduceOnly", None)
            data = self._req("POST", "/api/v2/mix/order/place-order", body=body)
        return (data or {}).get("orderId")

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
        """Snap entry/TP/SL to the symbol's actual Bitget tick
        (pricePlace decimals; tick = priceEndStep x 10^-pricePlace), so the
        exchange always accepts the price. No universal 4dp."""
        info = self.contract_info(symbol)
        dp = info["price_dp"]
        tick = info.get("price_tick") or (10 ** -dp)
        snapped = round(round(price / tick) * tick, dp)
        return f"{snapped:.{dp}f}"


_CLIENTS = {}

def client(demo=None):
    """Cached client per environment (None=default, True=demo, False=live)."""
    key = None if demo is None else bool(demo)
    if key not in _CLIENTS:
        _CLIENTS[key] = BitgetPrivate(demo=demo)
    return _CLIENTS[key]
