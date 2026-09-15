#!/usr/bin/env python3
"""Generate zero-JavaScript static chart pages (owner 2026-09-15).

Why: the owner's mobile network strips/blocks CDN scripts and even live
fetches failed intermittently, leaving the interactive chart blank. This
generator bakes everything — candles, trades, stats — into plain HTML +
inline SVG with NO JavaScript at all. Any browser that can show HTML can
show it, even Opera Mini / data-saver modes.

Run by the tick workflow after every run (data stays fresh) and manually:
    python lab/gen_static_chart.py
Writes docs/chart-lite.html (all pairs, stacked) — one page, one fetch.
"""
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

PAIRS = [p.strip().upper() for p in
         __import__("os").environ.get("PAIRS", "NEARUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT").split(",") if p.strip()]
CANDLES_WINDOW = 300          # 2m candles ≈ 10h — keeps the page light for slow networks
BG = "#0a0e17"
GRID = "rgba(255,255,255,0.05)"
TXT = "#8b93a7"
GREEN = "#16c784"
RED = "#ea3943"
ACCENT = "#00ceca"


def fetch_1m(symbol, limit=620):
    url = ("https://api.bitget.com/api/v2/mix/market/candles"
           f"?symbol={symbol}&productType=USDT-FUTURES&granularity=1m&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read().decode())["data"]
    # rows newest-first: [ts, o, h, l, c, baseVol, usdtVol, ...]
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4])} for r in rows]


def resample_2m(c1m):
    out = []
    for c in c1m:
        bucket = (c["ts"] // 120_000) * 120_000
        if out and out[-1]["ts"] == bucket:
            b = out[-1]
            b["h"] = max(b["h"], c["h"]); b["l"] = min(b["l"], c["l"]); b["c"] = c["c"]
        else:
            out.append({"ts": bucket, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]})
    return out


def svg_chart(candles, trades, symbol):
    """Candles as inline SVG — no canvas, no JS."""
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 340, 8, 64, 16, 24
    cw = (W - PAD_L - PAD_R) / CANDLES_WINDOW
    lo = min(c["l"] for c in candles); hi = max(c["h"] for c in candles)
    span = (hi - lo) or 1e-9
    def y(p): return PAD_T + (hi - p) / span * (H - PAD_T - PAD_B)
    def x(i): return PAD_L + i * cw

    parts = [f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;display:block">'
             f'<rect width="{W}" height="{H}" fill="{BG}"/>']
    # grid + price axis
    for k in range(5):
        p = lo + span * k / 4
        yy = y(p)
        parts.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W-PAD_R}" y2="{yy:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{W-PAD_R+6}" y="{yy+4:.1f}" fill="{TXT}" font-size="11" font-family="monospace">{p:.4g}</text>')
    # time ticks
    t0, t1 = candles[0]["ts"], candles[-1]["ts"]
    for k in range(5):
        tt = t0 + (t1 - t0) * k / 4
        xx = x((tt - t0) / (t1 - t0) * (CANDLES_WINDOW - 1))
        hh = datetime.fromtimestamp(tt / 1000, timezone.utc).strftime("%H:%M")
        parts.append(f'<text x="{xx:.0f}" y="{H-6}" fill="{TXT}" font-size="10" font-family="monospace" text-anchor="middle">{hh}</text>')
    # candles
    body = max(1.0, cw * 0.62)
    for i, c in enumerate(candles):
        up = c["c"] >= c["o"]
        col = GREEN if up else RED
        xx = x(i) + cw / 2
        parts.append(f'<line x1="{xx:.1f}" y1="{y(c["h"]):.1f}" x2="{xx:.1f}" y2="{y(c["l"]):.1f}" stroke="{col}" stroke-width="1"/>')
        top, bot = y(max(c["o"], c["c"])), y(min(c["o"], c["c"]))
        if bot - top < 1: bot = top + 1
        parts.append(f'<rect x="{xx-body/2:.1f}" y="{top:.1f}" width="{body:.1f}" height="{bot-top:.1f}" fill="{col}"/>')
    # trade markers (entries ▲/▼, exits ○)
    t0s = t0 / 1000; t1s = t1 / 1000
    for tr in trades:
        for kind, ts, px in (("e", tr["entry_time"], tr["entry_price"]),
                             ("x", tr.get("exit_time") or 0, tr.get("exit_price") or 0)):
            if not ts or not (t0s <= ts <= t1s) or not px: continue
            xx = x((ts - t0s) / (t1s - t0s) * (CANDLES_WINDOW - 1)) + cw / 2
            long_ = tr["direction"] == "LONG"
            col = GREEN if (long_ if kind == "e" else tr["net_pnl"] >= 0) else RED
            if kind == "e":
                d = f'M {xx:.0f} {y(px)+18:.0f} l 5 8 l -10 0 z' if long_ else \
                    f'M {xx:.0f} {y(px)-18:.0f} l 5 -8 l -10 0 z'
            else:
                yy = y(px)
                d = f'M {xx-4:.0f} {yy-4:.0f} l 8 8 m 0 -8 l -8 8'
            parts.append(f'<path d="{d}" fill="{col}" stroke="{col}" stroke-width="1.4"/>')
    parts.append("</svg>")
    return "".join(parts)


def fmt_pair(p): return p.replace("USDT", "/USDT")


def main():
    db = sqlite3.connect("data/trades.db")
    db.row_factory = sqlite3.Row
    now_s = time.time()
    secs = []
    trades_all = [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY entry_time DESC")]
    total_pnl = sum(t["net_pnl"] or 0 for t in trades_all)
    wins = sum(1 for t in trades_all if (t["net_pnl"] or 0) > 0)
    closed = sum(1 for t in trades_all if t["exit_time"])
    last24 = [t for t in trades_all if t["entry_time"] > now_s - 86400]

    for sym in PAIRS:
        try:
            c2 = resample_2m(fetch_1m(sym))[-CANDLES_WINDOW:]
        except Exception as e:
            print(f"  {sym}: candle fetch failed ({e}) — section skipped", file=sys.stderr)
            continue
        tr = [t for t in last24 if t["symbol"] == sym]
        chg = (c2[-1]["c"] / c2[0]["c"] - 1) * 100
        secs.append(f'''
<section style="margin:0 0 28px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;padding:0 4px 8px">
    <h2 style="margin:0;font-size:16px;color:#e7ecf3">{fmt_pair(sym)}</h2>
    <div style="font-size:13px;color:{'#16c784' if chg >= 0 else '#ea3943'}">
      {c2[-1]['c']:.4g} &nbsp;{chg:+.2f}% (24h) &nbsp;<span style="color:{TXT}">{len(tr)} trades 24h</span>
    </div>
  </div>
  {svg_chart(c2, tr, sym)}
</section>''')
        print(f"  {sym}: {len(c2)} candles, {len(tr)} trades in window")

    rows = "".join(
        f"<tr><td>{fmt_pair(t['symbol'])}</td>"
        f"<td style='color:{'#16c784' if t['direction']=='LONG' else '#ea3943'}'>{t['direction']}</td>"
        f"<td>{datetime.fromtimestamp(t['entry_time'], timezone.utc):%m-%d %H:%M}</td>"
        f"<td>{t['entry_price']:.4g}</td>"
        f"<td>{t['exit_price'] or '—':.4g}</td>"
        f"<td style='color:{'#16c784' if (t['net_pnl'] or 0) >= 0 else '#ea3943'}'>{(t['net_pnl'] or 0):+.4f}</td>"
        f"<td>{t['engine'] or ''}</td></tr>"
        for t in trades_all[:25])

    wr = (wins / closed * 100) if closed else 0
    html = f'''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEAR Scalper — Static Chart (no-JS)</title></head>
<body style="margin:0;background:{BG};color:#e7ecf3;font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif">
<div style="max-width:760px;margin:0 auto;padding:14px 10px 40px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;margin-bottom:6px">
    <h1 style="margin:0;font-size:17px;letter-spacing:.04em">NEAR SCALPER · STATIC CHART</h1>
    <span style="font-size:12px;color:{TXT}">auto-generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · no-JS fallback</span>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;font-size:13px;color:{TXT};border-bottom:1px solid {GRID};padding-bottom:10px;margin-bottom:14px">
    <span>net PnL: <b style="color:{'#16c784' if total_pnl >= 0 else '#ea3943'}">{total_pnl:+.4f} USDT</b></span>
    <span>closed: {closed}</span>
    <span>win rate: {wr:.0f}%</span>
    <span>24h trades: {len(last24)}</span>
  </div>
  {''.join(secs) if secs else '<p style="color:#ea3943">Chart data unavailable right now — the live version at chart.html has full history.</p>'}
  <h2 style="font-size:15px;color:#e7ecf3;margin:22px 0 8px">LAST 25 EXECUTIONS</h2>
  <div style="overflow-x:auto">
  <table style="border-collapse:collapse;font-size:12px;white-space:nowrap;color:#c6cddb">
    <tr style="color:{TXT};text-align:left"><th>pair</th><th>side</th><th>entry (UTC)</th><th>entry</th><th>exit</th><th>net PnL</th><th>engine</th></tr>
    {rows}
  </table></div>
  <p style="font-size:12px;color:{TXT};margin-top:16px">▲ entry · ✕ exit · interactive version: <a href="chart.html" style="color:{ACCENT}">chart.html</a></p>
</div></body></html>'''
    with open("docs/chart-lite.html", "w") as f:
        f.write(html)
    print(f"docs/chart-lite.html written ({len(html)//1024} KB, {len(secs)} pair sections)")


if __name__ == "__main__":
    main()
