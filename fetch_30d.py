import json, time, urllib.request

BASE = "https://api.bitget.com/api/v2/mix/market/candles"
PAIRS = ["NEARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
NOW_MS = int(time.time() * 1000)
START_MS = NOW_MS - 30 * 86400 * 1000

for pair in PAIRS:
    out, end_ms, pages = [], NOW_MS, 0
    while end_ms > START_MS and pages < 60:
        url = f"{BASE}?symbol={pair}&productType=USDT-FUTURES&granularity=1m&limit=1000&endTime={end_ms}"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.load(r).get("data") or []
        except Exception as e:
            print(f"  {pair} page {pages}: {e} — retry once")
            time.sleep(2)
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.load(r).get("data") or []
            except Exception:
                break
        if not data:
            break
        for row in data:
            out.append({"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "vol": float(row[5])})
        oldest = min(int(r[0]) for r in data)
        if oldest >= end_ms:
            break
        end_ms = oldest - 1
        pages += 1
        time.sleep(0.15)
    # dedupe, sort ascending, drop the still-forming last candle
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] in seen:
            continue
        seen.add(c["ts"])
        rows.append(c)
    if rows:
        rows = rows[:-1]
        gaps = sum(1 for a, b in zip(rows, rows[1:]) if b["ts"] - a["ts"] != 60000)
        with open(f"data_{pair}_30d.json", "w") as f:
            json.dump(rows, f)
        span = (rows[-1]["ts"] - rows[0]["ts"]) / 86400000
        print(f"{pair}: {len(rows)} candles, {span:.1f} days, {gaps} gap(s)")
