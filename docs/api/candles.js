// Vercel serverless proxy (owner 2026-09-15): the browser fetching Bitget
// directly failed with "Chart data fetch failed" specifically on the
// owner's Nigerian mobile IP — Bitget geo-restricts/throttles many African
// ISPs at the API edge. This function runs on Vercel's own servers (never
// geo-blocked) and just relays the JSON, same-origin, no CORS issues.
export default async function handler(req, res) {
  const { symbol, limit = "1000" } = req.query;
  if (!symbol) return res.status(400).json({ error: "symbol required" });
  try {
    const url = `https://api.bitget.com/api/v2/mix/market/candles?symbol=${encodeURIComponent(symbol)}&productType=USDT-FUTURES&granularity=1m&limit=${encodeURIComponent(limit)}`;
    const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
    const j = await r.json();
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Cache-Control", "s-maxage=15, stale-while-revalidate=30");
    res.status(200).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e) });
  }
}
