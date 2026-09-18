import { useEffect, useState } from "react";
import {
  fetchBacktestRuns, fetchInsiderMarkets, fetchInsiderStatus, fetchMarkets,
} from "../api";
import { timeAgo } from "../format";
import VerdictBadge from "../components/VerdictBadge";

function fmtUsd(n) {
  return n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(Math.round(n)).toLocaleString()}`;
}

export default function OverviewPage({ onOpenMarket, onSwitchTab }) {
  const [topMarkets, setTopMarkets] = useState(null);
  const [insiderMarkets, setInsiderMarkets] = useState(null);
  const [insiderStatus, setInsiderStatus] = useState(null);
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchMarkets({ sortBy: "conviction", limit: 5 }).then((d) => setTopMarkets(d.markets)).catch((e) => setError(e.message));
    fetchInsiderMarkets({ limit: 5 }).then((d) => setInsiderMarkets(d.markets)).catch(() => {});
    fetchInsiderStatus().then(setInsiderStatus).catch(() => {});
    fetchBacktestRuns(5).then((d) => setRuns(d.runs)).catch(() => {});
  }, []);

  return (
    <div>
      <div className="insiders-header">
        <div>
          <h2>Overview</h2>
          <p className="subtitle">
            Where things stand right now across research, strategy signals, and backtesting — a starting
            point, not a replacement for the dedicated pages.
          </p>
        </div>
      </div>

      {error && <div className="error">Error: {error}</div>}

      <div className="overview-disclosure" style={{ marginBottom: 32 }}>
        <strong>Honest state of the strategies:</strong> a held-out (never-tuned-against) validation run
        found neither Smart Money nor Insider scoring reliably beats its default weights yet — Smart
        Money scored -19.2% ROI and Insider scored -11.1% ROI on markets excluded from tuning. Treat
        every verdict/score below as a research signal to investigate, not a demonstrated edge to act on.
      </div>

      <div className="overview-section">
        <h3>Highest-conviction live markets</h3>
        {!topMarkets && <div className="empty-state">Loading…</div>}
        {topMarkets && topMarkets.length === 0 && <div className="empty-state">No scored markets yet.</div>}
        {topMarkets && topMarkets.slice(0, 5).map((m) => (
          <div key={m.condition_id} className="overview-row" style={{ cursor: "pointer" }} onClick={() => onOpenMarket(m.condition_id)}>
            <div>
              <div className="overview-row-main">{m.question}</div>
              <div className="overview-row-meta">${Math.round(m.volume || 0).toLocaleString()} volume</div>
            </div>
            {m.verdict ? <VerdictBadge label={m.verdict.label} conviction={m.verdict.conviction} /> : <span className="overview-row-meta">—</span>}
          </div>
        ))}
        {topMarkets && topMarkets.length > 0 && (
          <button className="back-link" style={{ marginTop: 10 }} onClick={() => onSwitchTab("markets")}>View all markets →</button>
        )}
      </div>

      <div className="overview-section">
        <h3>Recent insider flags</h3>
        <p className="chart-caption" style={{ marginTop: -10 }}>
          {insiderStatus
            ? `Background scan ${insiderStatus.poller_enabled ? `on, every ${Math.round(insiderStatus.poller_interval_seconds / 60)}m` : "off"} — last completed ${timeAgo(insiderStatus.last_scan_completed_at)}.`
            : ""}
        </p>
        {!insiderMarkets && <div className="empty-state">Loading…</div>}
        {insiderMarkets && insiderMarkets.length === 0 && <div className="empty-state">No flagged markets right now.</div>}
        {insiderMarkets && insiderMarkets.slice(0, 5).map((m) => (
          <div key={m.condition_id} className="overview-row" style={{ cursor: "pointer" }} onClick={() => onOpenMarket(m.condition_id)}>
            <div>
              <div className="overview-row-main">{m.question || m.condition_id}</div>
              <div className="overview-row-meta">
                YES ${Math.round(m.yes_flagged_usd).toLocaleString()} ({m.yes_flag_count}) · NO ${Math.round(m.no_flagged_usd).toLocaleString()} ({m.no_flag_count}) · {timeAgo(m.last_detected_at)}
              </div>
            </div>
            <span className="overview-row-meta">{m.label} · {m.conviction.toFixed(0)}</span>
          </div>
        ))}
        {insiderMarkets && insiderMarkets.length > 0 && (
          <button className="back-link" style={{ marginTop: 10 }} onClick={() => onSwitchTab("insiders")}>View all insider flags →</button>
        )}
      </div>

      <div className="overview-section">
        <h3>Recent backtest runs</h3>
        {!runs && <div className="empty-state">Loading…</div>}
        {runs && runs.length === 0 && <div className="empty-state">No backtests run yet.</div>}
        {runs && runs.slice(0, 5).map((r) => (
          <div key={r.id} className="overview-row" style={{ cursor: "pointer" }} onClick={() => onSwitchTab("backtest")}>
            <div>
              <div className="overview-row-main">
                {r.params.strategy === "smart_money" ? "Smart Money" : "Insider score"} · {r.params.condition_ids?.length ?? "?"} markets
              </div>
              <div className="overview-row-meta">{r.status} · {timeAgo(r.created_at)}</div>
            </div>
            {r.summary?.total_pnl_usd != null ? (
              <span className={r.summary.total_pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(r.summary.total_pnl_usd)}</span>
            ) : (
              <span className="overview-row-meta">—</span>
            )}
          </div>
        ))}
        {runs && runs.length > 0 && (
          <button className="back-link" style={{ marginTop: 10 }} onClick={() => onSwitchTab("backtest")}>Go to Backtest →</button>
        )}
      </div>
    </div>
  );
}
