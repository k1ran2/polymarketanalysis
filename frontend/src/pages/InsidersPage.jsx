import { useEffect, useRef, useState } from "react";
import { fetchInsiderMarkets, fetchInsiderMarketDetail, fetchInsiderStatus, scanInsidersNow } from "../api";
import { timeAgo, formatTenure, tierColor } from "../format";

function scoreColor(score) {
  if (score >= 80) return "#c0392b";
  if (score >= 60) return "#e07a1f";
  return "#8892a0";
}

export default function InsidersPage({ onOpenMarket }) {
  const [markets, setMarkets] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [minScore, setMinScore] = useState(0);
  const [includeNearResolved, setIncludeNearResolved] = useState(false);
  const [scanNotice, setScanNotice] = useState(null);
  const [selected, setSelected] = useState(null);   // { condition_id, question, ... } or null
  const [detail, setDetail] = useState(null);        // full GET /api/insiders/{condition_id} response
  const pollTimer = useRef(null);

  function loadMarkets() {
    fetchInsiderMarkets({ minScore, includeNearResolved }).then(setMarkets).catch((e) => setError(e.message));
  }

  useEffect(() => {
    loadMarkets();
    fetchInsiderStatus().then(setStatus).catch(() => {});
    return () => clearTimeout(pollTimer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minScore, includeNearResolved]);

  useEffect(() => {
    if (!selected) return;
    setDetail(null);
    fetchInsiderMarketDetail(selected.condition_id, { minScore })
      .then(setDetail)
      .catch((e) => setError(e.message));
  }, [selected, minScore]);

  async function handleScan() {
    setScanNotice(null);
    setError(null);
    try {
      const r = await scanInsidersNow();
      if (!r.started) {
        setScanNotice("A scan is already running — waiting for it to finish…");
      }
      pollForScanCompletion();
    } catch (e) {
      setError(e.message);
    }
  }

  function pollForScanCompletion() {
    fetchInsiderStatus().then((s) => {
      setStatus(s);
      if (s.scanning) {
        pollTimer.current = setTimeout(pollForScanCompletion, 2000);
      } else {
        setScanNotice(`Scan complete — data confirmed up to date as of ${timeAgo(s.last_scan_completed_at)}.`);
        loadMarkets();
        if (selected) {
          fetchInsiderMarketDetail(selected.condition_id, { minScore }).then(setDetail);
        }
      }
    });
  }

  const scanning = status?.scanning;

  return (
    <div>
      <div className="insiders-header">
        <div>
          <h2>Insider watch</h2>
          <p className="subtitle">
            Heuristic flags for positions that look like informed/early money — large relative to a
            wallet's own history, from a wallet barely active on Polymarket, or with ~no track
            record at all. <strong>First-pass and unvalidated</strong> — a lead to investigate, not a
            determination of wrongdoing.
          </p>
        </div>
        <button onClick={handleScan} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan now"}
        </button>
      </div>

      <div className="insiders-controls">
        <label>
          Min score:
          <input
            type="range" min="0" max="100" step="5" value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
          />
          {minScore}
        </label>
        <label>
          <input
            type="checkbox" checked={includeNearResolved}
            onChange={(e) => setIncludeNearResolved(e.target.checked)}
          />
          Show markets already priced near $0/$1 (no edge left)
        </label>
        {status && (
          <span className="insiders-meta">
            last scan: {timeAgo(status.last_scan_completed_at)}
            {" · "}alert threshold {markets?.alert_threshold ?? "—"}
            {" · "}background poller {status.poller_enabled ? `on (every ${Math.round(status.poller_interval_seconds / 60)}m)` : "off"}
          </span>
        )}
      </div>

      {scanNotice && <div className="notice">{scanNotice}</div>}
      {error && <div className="error">Error: {error}</div>}

      {selected ? (
        <div>
          <button className="back-link" onClick={() => setSelected(null)}>&larr; Back to flagged markets</button>
          <h3>{selected.question || selected.condition_id}</h3>
          {!detail && <div className="empty-state">Loading…</div>}
          {detail && (
            <div className="market-meta insiders-market-stats">
              <span>
                current price: <strong>{detail.current_price_yes != null ? `${(detail.current_price_yes * 100).toFixed(1)}¢ YES` : "—"}</strong>
              </span>
              <span>volume: <strong>${Math.round(detail.volume || 0).toLocaleString()}</strong></span>
              <span>24h: <strong>${Math.round(detail.volume_24hr || 0).toLocaleString()}</strong></span>
              <span>liquidity: <strong>${Math.round(detail.liquidity || 0).toLocaleString()}</strong></span>
              {detail.near_resolved && (
                <span className="tier-pill error-pill" title="Already priced within a fraction of a cent of $0/$1 — little room left to trade an edge">
                  near-resolved
                </span>
              )}
              <span
                className="insider-open-hint"
                onClick={() => onOpenMarket(selected.condition_id)}
                title="Open this market's full analysis"
              >
                open full market analysis →
              </span>
            </div>
          )}
          <div className="insider-list">
            {detail?.flags && detail.flags.map((f) => (
              <div key={`${f.wallet}-${f.side}`} className="insider-row">
                <div className="insider-score" style={{ color: scoreColor(f.insider_score) }}>
                  {f.insider_score.toFixed(0)}
                </div>
                <div className="insider-body">
                  <div className="market-meta">
                    <span>{f.side}</span>
                    <span>${Math.round(f.position_usd).toLocaleString()}</span>
                    <span>tenure: {formatTenure(f.tenure_days)}</span>
                    <span>{f.n_resolved} resolved trades</span>
                    <span className="mono">{f.wallet.slice(0, 6)}…{f.wallet.slice(-4)}</span>
                    <span
                      className="tier-pill"
                      style={{ background: tierColor(f.tier) }}
                      title="This wallet's Smart Score — historical skill, independent of this insider flag"
                    >
                      {f.tier}{f.score_percentile != null ? ` · ${f.score_percentile.toFixed(0)}` : ""}
                    </span>
                  </div>
                  <ul className="insider-reasons">
                    {f.reasons.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </div>
                <div
                  className="insider-when"
                  title={
                    f.first_seen_at != null
                      ? "When this system first observed the position -- not necessarily the exact trade time (see the Insiders tab's methodology note)"
                      : `Detected during a scan (position first-seen time not available) — ${timeAgo(f.detected_at)}`
                  }
                >
                  {f.first_seen_at != null ? timeAgo(f.first_seen_at) : timeAgo(f.detected_at)}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <>
          {!markets && !error && <div className="empty-state">Loading…</div>}
          {markets && markets.markets.length === 0 && (
            <div className="empty-state">
              No flagged markets yet — open some markets (or hit "Scan now") to run the detector
              against their current holders.
            </div>
          )}
          <div className="insider-list">
            {markets && markets.markets.map((m) => (
              <div key={m.condition_id} className="insider-market-row" onClick={() => setSelected(m)}>
                <div className="insider-market-row-main">
                  <div className="insider-market">{m.question || m.condition_id}</div>
                  <div className="market-meta">
                    {m.category && <span className="pill">{m.category}</span>}
                    {m.current_price_yes != null && <span>price: {(m.current_price_yes * 100).toFixed(1)}¢ YES</span>}
                    <span>YES: ${Math.round(m.yes_flagged_usd).toLocaleString()} ({m.yes_flag_count})</span>
                    <span>NO: ${Math.round(m.no_flagged_usd).toLocaleString()} ({m.no_flag_count})</span>
                    <span>{timeAgo(m.last_detected_at)}</span>
                  </div>
                </div>
                <div className="verdict-badge" style={{ borderColor: scoreColor(m.conviction), color: scoreColor(m.conviction) }}>
                  <span className="verdict-label">{m.label}</span>
                  <span className="verdict-conviction">{m.conviction.toFixed(0)}</span>
                </div>
                <div
                  className="insider-open-hint"
                  onClick={(e) => { e.stopPropagation(); onOpenMarket(m.condition_id); }}
                  title="Open this market's full analysis"
                >
                  open market →
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
