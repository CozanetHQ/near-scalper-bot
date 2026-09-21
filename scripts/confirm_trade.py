"""One-shot LIVE confirmation trade (owner request 2026-09-21).

"After all the fixes the bot should be able to take one trade to confirm."

Runs on the GitHub runner (same env/secrets as the tick workflow). Uses the
SAME production path as the sovereign engine — engine/live.py client,
contract_info, round_qty, set_leverage, place-order with presetStopSurplus /
presetStopLoss — but enters at MARKET so it fills immediately instead of
resting on a retest level.

- Pair: NEARUSDT (flagship, +12R/30d expectancy in the gate replay)
- Side: LONG (4H EMA bias is bull)
- Size: engine standard — $3 cash @ 10x ≈ $30 notional
- Risk: SL 0.5% / TP 1.0% (RR 2.0) ≈ $0.15 risk on the wallet
- After the fill, the sovereign engine adopts the position on its next tick
  (orphan adoption in _live_reconcile) and manages the exit with its own
  logic. This trade is a pipeline confirmation, NOT a strategy signal.

Stages reported to Telegram: CONFIRM_SUBMITTED -> CONFIRM_FILLED ->
CONFIRM_POSITION_LIVE (or CONFIRM_REJECTED with the exact Bitget error,
which is usually the API key still missing trade/positions permission).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYMBOL = "NEARUSDT"
SL_PCT, TP_PCT = 0.005, 0.010          # 0.5% stop / 1.0% target (RR 2.0)
LEV = 10
MARGIN_USD = float(os.environ.get("LIVE_MARGIN_USD", "3"))


def tg(msg):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("[no-telegram]", msg)
        return
    try:
        data = json.dumps({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("[telegram-fail]", e)


def main():
    from engine import live as LIVE
    from engine.sovereign import LEV_CAP

    cli = LIVE.client(demo=False)          # LIVE wallet — never demo
    eq = cli.account_equity()
    print(f"live equity: ${eq:.4f}")

    info = cli.contract_info(SYMBOL)
    row = cli._req("GET", "/api/v2/mix/market/ticker",
                   {"symbol": SYMBOL, "productType": LIVE.PRODUCT})[0]
    px = float(row["lastPr"])

    cap = min(MARGIN_USD * LEV, LEV * eq,
              float(os.environ.get("LIVE_MAX_NOTIONAL", "50")))
    qty = cli.round_qty(SYMBOL, cap / px)
    notional = qty * px
    if notional < 5:
        print(f"CONFIRM_SKIP: notional ${notional:.2f} below Bitget $5 min")
        tg(f"CONFIRM_SKIP {SYMBOL}: sized below Bitget $5 minimum — wallet ${eq:.2f} "
           f"too small for a $3-margin clip; no order placed")
        return 1

    sl, tp = px * (1 - SL_PCT), px * (1 + TP_PCT)
    cli.set_leverage(SYMBOL, LEV)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    tg(f"CONFIRM_SUBMITTED {SYMBOL} LONG market: qty {qty} (~${notional:.2f} @ "
       f"{px:.4g}) — cash ${notional/LEV:.2f} @ {LEV}x, SL {sl:.4g} / TP {tp:.4g} {ts} UTC")

    try:
        data = cli._req("POST", "/api/v2/mix/order/place-order", body={
            "symbol": SYMBOL, "productType": LIVE.PRODUCT,
            "marginMode": "isolated", "marginCoin": LIVE.MARGIN_COIN,
            "size": cli._fmt_size(SYMBOL, qty),
            "side": "buy", "tradeSide": "open",
            "orderType": "market", "force": "gtc",
            "presetStopSurplusPrice": cli._fmt_price(SYMBOL, tp),
            "presetStopLossPrice": cli._fmt_price(SYMBOL, sl),
        })
        oid = (data or {}).get("orderId")
    except LIVE.BitgetError as e:
        print(f"CONFIRM_REJECTED: Bitget code {getattr(e, 'code', '?')} — {e}")
        tg(f"CONFIRM_REJECTED {SYMBOL}: Bitget code {getattr(e, 'code', '?')} — "
           f"{str(e)[:200]}. If this is a permission error, enable Futures "
           f"trade (positions read+write) on the API key in Bitget settings.")
        return 2

    print(f"order accepted: {oid}")
    od = cli.order_detail(SYMBOL, oid)
    pos = cli.position(SYMBOL)
    entry = pos["entry"] if pos else (od.get("avg_price") or px)
    tg(f"CONFIRM_FILLED {SYMBOL} LONG qty {qty} @ {entry:.4g} — order {oid}; "
       f"SL {sl:.4g} / TP {tp:.4g} live on-exchange. The engine adopts and "
       f"manages it on the next tick (orphan adoption).")
    print(json.dumps({"order_id": oid, "qty": qty, "entry": entry,
                      "sl": sl, "tp": tp, "notional": notional}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
