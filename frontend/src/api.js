const BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

async function request(path, options) {
  const res = await fetch(BASE + path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export const api = {
  overview: () => request("/api/overview"),
  strategies: () => request("/api/strategies"),
  scan: () => request("/api/scan"),
  calibration: () => request("/api/calibration"),
  traders: () => request("/api/traders"),
  runLab: (body) =>
    request("/api/lab/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
};

export const fmt = {
  int: (n) => (n == null ? "–" : Math.round(n).toLocaleString()),
  usd: (n) => (n == null ? "–" : (n < 0 ? "-$" : "$") + Math.abs(Math.round(n)).toLocaleString()),
  usdCompact: (n) => {
    if (n == null) return "–";
    const a = Math.abs(n);
    const s = a >= 1e9 ? (a / 1e9).toFixed(1) + "B" : a >= 1e6 ? (a / 1e6).toFixed(1) + "M" : a >= 1e3 ? (a / 1e3).toFixed(0) + "k" : a.toFixed(0);
    return (n < 0 ? "-$" : "$") + s;
  },
  pct: (n, d = 1) => (n == null ? "–" : (n > 0 ? "+" : "") + n.toFixed(d) + "%"),
  pctPlain: (n, d = 1) => (n == null ? "–" : n.toFixed(d) + "%"),
  num: (n, d = 2) => (n == null ? "–" : n.toFixed(d)),
  date: (ts) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "–"),
  wallet: (w) => (w ? w.slice(0, 6) + "…" + w.slice(-4) : "–"),
  sign: (n) => (n > 0 ? "pos" : n < 0 ? "neg" : ""),
};

/** Shared verdict rule: an edge counts only if it is clearly positive on BOTH periods. */
export function verdict(train, validation) {
  if (!train?.n_trades || !validation?.n_trades) return { kind: "warn", label: "No trades" };
  if (validation.n_events < 100 || train.n_events < 100) return { kind: "warn", label: "Not enough data" };
  if (train.significant && validation.significant) return { kind: "good", label: "Edge held on unseen data" };
  if (validation.roi_hi < 0) return { kind: "bad", label: "Loses money" };
  return { kind: "bad", label: "No reliable edge" };
}
