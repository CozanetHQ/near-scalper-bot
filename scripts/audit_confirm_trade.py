"""One-shot diagnostic: what ACTUALLY happened to the NEARUSDT confirmation
trade (owner report 2026-09-21 02:39 Lagos: manual-manager alerted a CLOSE
at entry==exit / PnL 0.0000, which is the code's FALLBACK value used when it
can't find the true closing fill -- not proof the real exit was breakeven).

Pulls every endpoint that could hold the real exit: order-history (manual
close), orders-plan profit_loss (standalone plan orders), AND the
position's own bill/fee history (account bills), which records the
realized PnL Bitget itself booked regardless of how the close order shows
up elsewhere. Reports the true entry/exit/PnL/reason to Telegram and to
stdout for the run log.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYMBOL = "NEARUSDT"
ENTRY = 4.1991
QTY = 7


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
    cli = LIVE.client(demo=False)

    print("=== order-history (all, tradeSide=close) ===")
    rows = cli._req("GET", "/api/v2/mix/order/orders-history",
                    {"symbol": SYMBOL, "productType": LIVE.PRODUCT})
    rows = (rows or {}).get("entrustedList", []) if isinstance(rows, dict) else (rows or [])
    close_rows = [r for r in rows if (r.get("tradeSide") or "") == "close"]
    for r in close_rows:
        print(json.dumps(r, indent=1))

    print("=== orders-plan (profit_loss) ===")
    try:
        prows = cli._req("GET", "/api/v2/mix/order/orders-plan",
                         {"symbol": SYMBOL, "productType": LIVE.PRODUCT,
                          "planType": "profit_loss"})
        plist = (prows or {}).get("entrustedList", []) if isinstance(prows, dict) else (prows or [])
        for r in plist:
            print(json.dumps(r, indent=1))
    except Exception as e:
        print("orders-plan error:", e)
        plist = []

    print("=== orders-plan (pos_profit / pos_loss, tpsl variants) ===")
    for pt in ("pos_profit", "pos_loss", "normal_plan", "track_plan"):
        try:
            r2 = cli._req("GET", "/api/v2/mix/order/orders-plan",
                          {"symbol": SYMBOL, "productType": LIVE.PRODUCT, "planType": pt})
            r2list = (r2 or {}).get("entrustedList", []) if isinstance(r2, dict) else (r2 or [])
            if r2list:
                print(f"-- planType={pt} --")
                for r in r2list:
                    print(json.dumps(r, indent=1))
        except Exception as e:
            print(f"planType={pt} error:", e)

    print("=== account bills (realized PnL / fees ledger) ===")
    bill_rows = []
    try:
        bills = cli._req("GET", "/api/v2/mix/account/bill",
                         {"symbol": SYMBOL, "productType": LIVE.PRODUCT,
                          "marginCoin": LIVE.MARGIN_COIN})
        bill_rows = (bills or {}).get("bills", []) if isinstance(bills, dict) else (bills or [])
        for r in bill_rows:
            print(json.dumps(r, indent=1))
    except Exception as e:
        print("bill error:", e)

    print("=== current position (should be flat) ===")
    pos = cli.position(SYMBOL)
    print(pos)

    # ── determine the real story ──
    real_exit, real_reason, real_pnl = None, None, None
    if close_rows:
        best = max(close_rows, key=lambda r: int(r.get("cts") or r.get("uTime") or 0))
        real_exit = float(best.get("priceAvg") or 0)
        real_reason = f"order-history close ({best.get('orderType')})"
    if not real_exit and plist:
        best = max(plist, key=lambda r: int(r.get("uTime") or 0))
        real_exit = float(best.get("triggerPrice") or best.get("price") or 0)
        real_reason = f"plan trigger ({best.get('planType') or best.get('triggerType')})"
    realized_pnl_bills = 0.0
    fee_bills = 0.0
    for r in bill_rows:
        biz = (r.get("businessType") or r.get("business") or "").lower()
        amt = float(r.get("amount") or r.get("fee") or 0)
        if "close" in biz or "profit" in biz or "pnl" in biz:
            realized_pnl_bills += amt
        if "fee" in biz:
            fee_bills += amt

    summary = (
        f"CONFIRM_TRADE_AUDIT {SYMBOL}: entry ${ENTRY} qty {QTY}\n"
        f"real exit price: {real_exit if real_exit else 'not found in order/plan history'}\n"
        f"real close reason: {real_reason or 'unknown — see bills'}\n"
        f"account bills realized pnl: {realized_pnl_bills:+.6f} | fees: {fee_bills:+.6f}\n"
        f"(manual-manager's 'PnL 0.0000' alert was its FALLBACK value — it "
        f"could not find the exit fill and defaulted exit=entry, not a real "
        f"breakeven measurement)"
    )
    print(summary)
    tg(summary)


if __name__ == "__main__":
    main()
