#!/usr/bin/env python3
"""Live execution verification (owner-authorized 2026-09-19, spec sections 2+3).

Proves, WITHOUT placing any order and WITHOUT any API key:
  1. _fmt_price snaps entry/TP/SL to each symbol's REAL Bitget contract
     precision (pricePlace/priceEndStep fetched live from the public
     endpoint) for all five pairs, with realistic arbitrary prices.
  2. The complete order construction (strategy params -> Bitget payload)
     is correct: product, margin mode, leverage call, side, tradeSide,
     order type, force, qty precision/minimums, price precision.

No network writes: the BitgetPrivate._req transport is monkeypatched to
capture payloads locally. The ONLY network traffic is the public
(unauthenticated) contract-spec GET.
"""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from engine.live import BitgetPrivate, PRODUCT, MARGIN_COIN  # noqa: E402

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"]
# realistic mid-September prices with deliberately ugly ATR-derived offsets
SCENARIOS = {
    #            entry-ish price   atr_frac (fraction of price)
    "BTCUSDT": (81083.3555,        0.0028),
    "ETHUSDT":  (2618.3742,        0.0031),
    "SOLUSDT":  (112.92347,        0.0034),
    "NEARUSDT": (3.7413555,         0.0042),
    "XRPUSDT":  (1.3969123,         0.0038),
}
RR = 2.0
LEV = 10
CASH = 3.0
passed = failed = 0


def check(ok, label):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def public_specs():
    url = ("https://api.bitget.com/api/v2/mix/market/contracts"
           "?productType=USDT-FUTURES")
    with urllib.request.urlopen(url, timeout=15) as r:
        rows = json.loads(r.read().decode())["data"]
    return {r["symbol"]: r for r in rows if r["symbol"] in PAIRS}


def make_client(specs):
    """BitgetPrivate with contract specs pre-cached from the LIVE public
    endpoint, so contract_info() never needs keys."""
    cli = BitgetPrivate(demo=False)
    for sym, r in specs.items():
        dp = int(float(r.get("pricePlace") or 4))
        cli._contracts[sym] = {
            "size_step": float(r.get("sizeMultiplier") or 1),
            "min_size": float(r.get("minTradeNum") or 1),
            "min_usdt": float(r.get("minTradeUSDT") or 5),
            "price_dp": dp,
            "price_tick": float(r.get("priceEndStep") or 1) * (10 ** -dp),
        }
    return cli


def on_tick(value, tick, tol=1e-9):
    return abs(value / tick - round(value / tick)) < tol


def decimals_of(s):
    return len(s.split(".")[1]) if "." in s else 0


def main():
    print("== fetching LIVE Bitget contract specs (public, unauthenticated) ==")
    specs = public_specs()
    cli = make_client(specs)
    print()

    # -- Section 2: price formatting per contract spec -------------------
    print("== 1. PRICE PRECISION (entry / TP / SL) - five pairs ==")
    for sym in PAIRS:
        r = specs[sym]
        dp = int(float(r["pricePlace"]))
        tick = float(r["priceEndStep"]) * (10 ** -dp)
        level, atr_frac = SCENARIOS[sym]
        d = atr_frac * level
        for side, entry, sl, tp in (
            ("LONG",  level,               level - d,       level + RR * d),
            ("SHORT", level + 0.0012345,   level + d + 0.0012345, level - RR * d),
        ):
            for name, p in (("entry", entry), ("SL", sl), ("TP", tp)):
                s = cli._fmt_price(sym, p)
                v = float(s)
                check(decimals_of(s) == dp,
                      f"{sym} {side} {name}: '{s}' has exactly {dp} dp (pricePlace={dp})")
                check(on_tick(v, tick),
                      f"{sym} {side} {name}: {v} on-tick (tick={tick})")
            e_s, tp_s, sl_s = (cli._fmt_price(sym, x) for x in (entry, tp, sl))
            if side == "LONG":
                check(float(sl_s) < float(e_s) < float(tp_s),
                      f"{sym} LONG ordering preserved: SL {sl_s} < entry {e_s} < TP {tp_s}")
            else:
                check(float(tp_s) < float(e_s) < float(sl_s),
                      f"{sym} SHORT ordering preserved: TP {tp_s} < entry {e_s} < SL {sl_s}")
    print()

    # -- Section 3: full order construction, NO order placed --------------
    print("== 2. ORDER CONSTRUCTION TRACE (transport captured, nothing sent) ==")
    captured = []

    def fake_req(method, path, params=None, body=None):
        captured.append({"method": method, "path": path,
                        "params": params or {}, "body": body})
        if path.endswith("place-order"):
            return {"orderId": "FAKE-NOT-REAL"}
        return None

    cli._req = fake_req
    for sym in PAIRS:
        r = specs[sym]
        dp = int(float(r["pricePlace"]))
        tick = float(r["priceEndStep"]) * (10 ** -dp)
        size_step = float(r["sizeMultiplier"])
        level, atr_frac = SCENARIOS[sym]
        d = atr_frac * level
        qty = cli.round_qty(sym, CASH * LEV / level)

        for dside, side_want, order_side in (("bull", "long", "buy"),
                                             ("bear", "short", "sell")):
            sl = level - d if side_want == "long" else level + d
            tp = level + RR * d if side_want == "long" else level - RR * d
            captured.clear()
            cli.set_leverage(sym, LEV)          # production sequence: leverage first
            oid = cli.place_entry_limit(sym, side_want, qty, level, sl, tp)

            calls = [c for c in captured if c["path"].endswith("place-order")]
            check(len(calls) == 1, f"{sym} {dside}: exactly one place-order call")
            b = calls[0]["body"]
            check(b["symbol"] == sym, f"{sym} {dside}: symbol {b['symbol']}")
            check(b["productType"] == PRODUCT, f"{sym} {dside}: productType USDT-FUTURES")
            check(b["marginMode"] == "isolated", f"{sym} {dside}: marginMode isolated")
            check(b["marginCoin"] == MARGIN_COIN, f"{sym} {dside}: marginCoin USDT")
            check(b["side"] == order_side, f"{sym} {dside}: side {b['side']} ({side_want})")
            check(b["tradeSide"] == "open", f"{sym} {dside}: tradeSide open (hedge-mode account)")
            check(b["orderType"] == "limit", f"{sym} {dside}: orderType limit")
            check(b["force"] == "post_only", f"{sym} {dside}: force post_only")
            check(b["size"] and float(b["size"]) >= float(r["minTradeNum"]),
                  f"{sym} {dside}: size {b['size']} >= minTradeNum {r['minTradeNum']}")
            sv = float(b["size"])
            check(abs(sv / size_step - round(sv / size_step)) < 1e-9,
                  f"{sym} {dside}: size on volume step {size_step}")
            for f in ("price", "presetStopSurplusPrice", "presetStopLossPrice"):
                check(decimals_of(b[f]) == dp and on_tick(float(b[f]), tick),
                      f"{sym} {dside}: {f} '{b[f]}' at {dp}dp on-tick")
            notional = sv * float(b["price"])
            check(notional >= float(r["minTradeUSDT"]),
                  f"{sym} {dside}: notional ${notional:.2f} >= minTradeUSDT ${r['minTradeUSDT']}")
            check(oid == "FAKE-NOT-REAL", f"{sym} {dside}: orderId surfaced from response")
            lev = [c for c in captured if c["path"].endswith("set-leverage")]
            check(len(lev) == 1 and lev[0]["body"]["marginMode"] == "isolated"
                  and lev[0]["body"]["leverage"] == str(LEV)
                  and captured.index(lev[0]) < captured.index(calls[0]),
                  f"{sym} {dside}: set-leverage isolated {LEV}x issued BEFORE place-order")
    print()

    print(f"RESULT: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
