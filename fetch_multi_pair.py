import json, time, urllib.request
from datetime import datetime, timezone, timedelta

PAIRS = ["NEARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
DAYS = 30

def fetch_page(symbol, end_ms):
    url = (f"https://api.bitget.com/api/v2/mix/market/candles?symbol={symbol}"
           f"&granularity=1m&limit=1000&productType=usdt-futures&endTime={end_ms}")
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(url, timeout=10)
            d = json.loads(r.read())
            if d.get("code") == "00000":
                return d["data"]
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return []

def fetch_symbol(symbol, days):
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=days)).timestamp() * 1000)
    out = []
    cursor = end_ms
    seen = set()
    while cursor > start_ms:
        page = fetch_page(symbol, cursor)
        if not page:
            break
        # Bitget returns newest->oldest or oldest->newest depending on version; normalize by ts
        for row in page:
            ts = int(row[0])
            if ts in seen:
                continue
            seen.add(ts)
            out.append({"ts": ts, "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "vol": float(row[5])})
        oldest_ts = min(int(r[0]) for r in page)
        if oldest_ts >= cursor:
            break
        cursor = oldest_ts
        time.sleep(0.15)
    out.sort(key=lambda c: c["ts"])
    out = [c for c in out if c["ts"] >= start_ms]
    return out

if __name__ == "__main__":
    for sym in PAIRS:
        print(f"fetching {sym}...", flush=True)
        candles = fetch_symbol(sym, DAYS)
        fname = f"data_{sym}_30d.json"
        json.dump(candles, open(fname, "w"))
        span_h = (candles[-1]["ts"] - candles[0]["ts"]) / 3600000 if candles else 0
        print(f"  {sym}: {len(candles)} candles, span {span_h:.1f}h -> {fname}")
