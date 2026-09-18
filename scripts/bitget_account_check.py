#!/usr/bin/env python3
"""Owner 2026-09-17: verify the Bitget API key + see the funded account.

Prints only balances and API status codes — never key material.
Probe order:
  1. Futures account  -> proves Futures permission (this is the 40014 test)
  2. Spot assets       -> the $4 the owner just funded (BNB)
  3. Futures asset list in case the transfer already happened
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bitget.com"
KEY = os.environ.get("BITGET_API_KEY", "")
SECRET = os.environ.get("BITGET_SECRET", "")
PASS = os.environ.get("BITGET_PASSPHRASE", "")


def signed(method, path, params=None, body=None, demo=False):
    params = params or {}
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    body_str = json.dumps(body) if body else ""
    ts = str(int(time.time() * 1000))
    pre = f"{ts}{method.upper()}{path}{body_str}"
    sign = base64.b64encode(hmac.new(SECRET.encode(), pre.encode(),
                                     hashlib.sha256).digest()).decode()
    headers = {
        "ACCESS-KEY": KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASS,
        "Content-Type": "application/json",
        "locale": "en-US",
    }
    if demo:
        headers["pap"] = "1"
    req = urllib.request.Request(API + path, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            raise
    return data


def show(label, data):
    print(f"\n=== {label} ===")
    print(f"code={data.get('code')} msg={str(data.get('msg'))[:180]}")
    return data.get("data")


def main():
    if not (KEY and SECRET and PASS):
        print("MISSING: BITGET_* secrets are not all set in the repo")
        sys.exit(1)
    print(f"key present, length {len(KEY)}")

    # 1) FUTURES permission probe — 40014 means no futures permission
    try:
        d = signed("GET", "/api/v2/mix/account/accounts",
                   {"productType": "USDT-FUTURES"})
        rows = show("FUTURES /mix/account/accounts (REAL account)", d)
        if rows:
            for r in rows:
                print(f"  {r.get('marginCoin')}: equity={r.get('accountEquity')} "
                      f"available={r.get('available')} locked={r.get('locked')}")
    except urllib.error.HTTPError as e:
        print(f"\n=== FUTURES probe === HTTP {e.code}")
        try:
            print(json.dumps(json.loads(e.read().decode()))[:300])
        except Exception:
            print("(no body)")
    except Exception as e:
        print(f"\n=== FUTURES probe === ERROR {type(e).__name__}: {str(e)[:200]}")

    # 2) SPOT assets — the $4 the owner funded (in BNB)
    try:
        d = signed("GET", "/api/v2/spot/account/assets")
        rows = show("SPOT assets (REAL account)", d)
        if rows:
            for a in rows:
                try:
                    bal = float(a.get("balance") or 0)
                    avail = float(a.get("available") or 0)
                    if bal > 0 or avail > 0:
                        print(f"  {a.get('coin')}: balance={bal} available={avail} "
                              f"frozen={a.get('frozen')}")
                except Exception:
                    pass
    except urllib.error.HTTPError as e:
        print(f"\n=== SPOT assets === HTTP {e.code}")
        try:
            print(json.dumps(json.loads(e.read().decode()))[:300])
        except Exception:
            print("(no body)")
    except Exception as e:
        print(f"\n=== SPOT assets === ERROR {type(e).__name__}: {str(e)[:200]}")

    # 3) FUTURES asset balances (in case the transfer to futures already ran)
    try:
        d = signed("GET", "/api/v2/mix/account/assets", {"productType": "USDT-FUTURES"})
        rows = show("FUTURES assets (REAL account)", d)
        if rows:
            for a in rows:
                if float(a.get("available") or 0) > 0 or float(a.get("frozen") or 0) > 0:
                    print(f"  {a.get('coin')}: available={a.get('available')} "
                          f"frozen={a.get('frozen')}")
    except urllib.error.HTTPError as e:
        print(f"\n=== FUTURES assets === HTTP {e.code}")
    except Exception as e:
        print(f"\n=== FUTURES assets === ERROR {type(e).__name__}: {str(e)[:200]}")

    print("\nDONE")


    # 4) POSITION MODE (owner pre-order verification 2026-09-19) — read-only.
    # posMode: "one_way_mode" | "hedge_mode". The production order path
    # (engine/live.py place_entry_limit) sends tradeSide:"open", which is
    # ONLY valid in hedge mode.
    try:
        d = signed("GET", "/api/v2/mix/account/account",
                   {"productType": "USDT-FUTURES", "marginCoin": "USDT"})
        rows = show("FUTURES position mode /mix/account/account (REAL account)", d)
        if isinstance(rows, dict):
            print(f"  posMode = {rows.get('posMode')}   "
                  f"(one_way_mode | hedge_mode)")
        elif isinstance(rows, list):
            for r in rows:
                print(f"  posMode = {r.get('posMode')}")
    except urllib.error.HTTPError as e:
        print(f"\n=== POSITION MODE === HTTP {e.code}")
        try:
            print(json.dumps(json.loads(e.read().decode()))[:300])
        except Exception:
            print("(no body)")
    except Exception as e:
        print(f"\n=== POSITION MODE === ERROR {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
