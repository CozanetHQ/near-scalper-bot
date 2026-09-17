// OWNER 2026-09-17: "Set Trading Mode" UI toggle — the owner wanted to flip
// LIVE/PAPER from the chart URL instead of digging through GitHub Actions.
//
// SECURITY MODEL (unchanged in spirit from set_mode.yml):
//   • The AI reasoning layer still has NO path to flip the mode. This function
//     only dispatches the existing "Set Trading Mode" workflow — the same
//     force-close + audit + commit logic still runs on GitHub, queueing
//     behind any in-flight tick (concurrency group: tick).
//   • Only the OWNER can fire it: requests must carry SET_MODE_KEY (a secret
//     only the owner knows, stored as a Vercel encrypted env var). The key
//     is checked with a timing-safe compare; failures return a generic 401
//     and never reveal whether the key or the mode was wrong.
//   • GH_DISPATCH_TOKEN (encrypted env var) is used to call the GitHub
//     dispatch API. Neither secret is ever logged.
//   • Same-origin + gh-pages origin only (CORS allowlist), so another site
//     can't silently drive this from the owner's browser.
export default async function handler(req, res) {
  const ALLOWED_ORIGINS = [
    "https://cozanethq.github.io",
    "https://cozanet-scalper-chart.vercel.app",
  ];
  const origin = req.headers.origin || "";
  const cors = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[1];
  res.setHeader("Access-Control-Allow-Origin", cors);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Cache-Control", "no-store");
  if (req.method === "OPTIONS") return res.status(204).end();

  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });

  const key = String(req.body?.key || "");
  const mode = String(req.body?.mode || "").toUpperCase();
  const reason = String(req.body?.reason || "").slice(0, 200);

  const expected = process.env.SET_MODE_KEY || "";
  if (!expected || key.length !== expected.length) {
    return res.status(401).json({ error: "invalid key" });
  }
  // timing-safe compare
  const a = Buffer.from(key), b = Buffer.from(expected);
  if (!crypto.timingSafeEqual(a, b)) {
    return res.status(401).json({ error: "invalid key" });
  }

  if (mode !== "LIVE" && mode !== "PAPER") {
    return res.status(400).json({ error: "mode must be LIVE or PAPER" });
  }

  const token = process.env.GH_DISPATCH_TOKEN || "";
  if (!token) return res.status(500).json({ error: "dispatch token not configured" });

  try {
    const r = await fetch(
      "https://api.github.com/repos/CozanetHQ/near-scalper-bot/actions/workflows/set_mode.yml/dispatches",
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Accept": "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "cozanet-mode-ui",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: {
            mode,
            reason: reason || "Owner UI toggle (chart.html)",
          },
        }),
      }
    );
    if (r.status === 204) {
      return res.status(200).json({
        ok: true,
        mode,
        note: "Switch queued behind any running tick; positions are force-closed first, then the flag flips. The MODE badge follows within one tick.",
      });
    }
    const t = await r.text();
    return res.status(502).json({ error: "GitHub dispatch failed", status: r.status, body: t.slice(0, 300) });
  } catch (e) {
    return res.status(502).json({ error: "dispatch error: " + String(e).slice(0, 200) });
  }
}
