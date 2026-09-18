import { useEffect, useState } from "react";
import { fetchMarketDetail } from "../api";
import VerdictBadge from "../components/VerdictBadge";
import SignedHistogram from "../components/SignedHistogram";
import WhalePie from "../components/WhalePie";
import InsiderSummary from "../components/InsiderSummary";
import { formatTenure, tierColor } from "../format";

// Mounted fresh per market (App keys this component on conditionId).
export default function MarketDetail({ conditionId, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let timer;

    function load() {
      fetchMarketDetail(conditionId)
        .then((d) => {
          if (cancelled) return;
          setData(d);
          // Backend scores the remaining holders in the background; poll until done.
          if (d.analyzing) timer = setTimeout(load, 3000);
        })
        .catch((e) => !cancelled && setError(e.message));
    }
    load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [conditionId]);

  if (error) {
    return (
      <div>
        <button className="back-link" onClick={onBack}>&larr; Back to markets</button>
        <div className="error">Error: {error}</div>
      </div>
    );
  }
  if (!data) {
    return (
      <div>
        <button className="back-link" onClick={onBack}>&larr; Back to markets</button>
        <div className="empty-state">Loading market…</div>
      </div>
    );
  }

  const { aggregation, verdict, whale_concentration, distribution, holders, insider_summary } = data;

  return (
    <div>
      <button className="back-link" onClick={onBack}>&larr; Back to markets</button>

      <div className="detail-header">
        <h2>{data.question}</h2>
        <VerdictBadge label={verdict.label} conviction={verdict.conviction} />
      </div>
      <div className="market-meta">
        {data.category && <span className="pill">{data.category}</span>}
        <span>${Math.round(data.volume || 0).toLocaleString()} volume</span>
        <span>current YES price: {(data.current_price_yes * 100).toFixed(1)}¢</span>
        <span>{data.rated_holder_count}/{data.holder_count} holders scored</span>
        {data.stale && <span className="stale-badge">stale — {Math.round(data.staleness_seconds / 60)}m old</span>}
      </div>
      {data.analyzing && (
        <div className="notice">
          Scoring {data.pending} more {data.pending === 1 ? "trader" : "traders"} from their
          Polymarket history… numbers below will sharpen automatically.
        </div>
      )}

      <section className="card-grid">
        <div className="stat-card">
          <div className="stat-label">Net smart-money flow</div>
          <div className={"stat-value " + (aggregation.net_smart_flow >= 0 ? "positive" : "negative")}>
            {aggregation.net_smart_flow >= 0 ? "+" : ""}${Math.round(aggregation.net_smart_flow).toLocaleString()}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Smart-implied probability</div>
          <div className={"stat-value" + (aggregation.smart_implied_probability == null ? " stat-value-muted" : "")}>
            {aggregation.smart_implied_probability != null ? `${(aggregation.smart_implied_probability * 100).toFixed(1)}%` : "not enough rated $ yet"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Undervalued gap</div>
          <div className={aggregation.undervalued_gap == null ? "stat-value stat-value-muted" : "stat-value " + (aggregation.undervalued_gap >= 0 ? "positive" : "negative")}>
            {aggregation.undervalued_gap != null ? `${(aggregation.undervalued_gap * 100).toFixed(1)}pts` : "not enough rated $ yet"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">YES $-weighted score</div>
          <div className={"stat-value" + (aggregation.usd_weighted_score_yes == null ? " stat-value-muted" : "")}>
            {aggregation.usd_weighted_score_yes != null ? aggregation.usd_weighted_score_yes.toFixed(1) : "no rated YES holders"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">NO $-weighted score</div>
          <div className={"stat-value" + (aggregation.usd_weighted_score_no == null ? " stat-value-muted" : "")}>
            {aggregation.usd_weighted_score_no != null ? aggregation.usd_weighted_score_no.toFixed(1) : "no rated NO holders"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Unrated $ share</div>
          <div className={"stat-value" + (verdict.unrated_volume_share == null ? " stat-value-muted" : "")}>
            {verdict.unrated_volume_share != null ? `${(verdict.unrated_volume_share * 100).toFixed(0)}%` : "—"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg wallet tenure — YES</div>
          <div className={"stat-value" + (aggregation.avg_tenure_days_yes == null ? " stat-value-muted" : "")}>
            {aggregation.avg_tenure_days_yes != null ? formatTenure(aggregation.avg_tenure_days_yes) : "unknown"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg wallet tenure — NO</div>
          <div className={"stat-value" + (aggregation.avg_tenure_days_no == null ? " stat-value-muted" : "")}>
            {aggregation.avg_tenure_days_no != null ? formatTenure(aggregation.avg_tenure_days_no) : "unknown"}
          </div>
        </div>
      </section>
      <p className="stat-footnote">
        Tenure = $-weighted time since each side's holders' earliest trade we've observed on
        Polymarket (a lower bound, not literal wallet age — see the Insiders tab).
      </p>

      <InsiderSummary summary={insider_summary} />

      <section>
        <h3>Signed holder-score distribution</h3>
        <SignedHistogram distribution={distribution} />
      </section>

      <section className="pie-row">
        <WhalePie title="YES side concentration" concentration={whale_concentration.yes} />
        <WhalePie title="NO side concentration" concentration={whale_concentration.no} />
      </section>

      <section>
        <h3>Holders ({holders.length})</h3>
        <div className="table-scroll">
        <table className="holder-table">
          <thead>
            <tr>
              <th>Wallet</th>
              <th>Side</th>
              <th>Position ($)</th>
              <th>Tier</th>
              <th>Smart score</th>
              <th>Tenure</th>
            </tr>
          </thead>
          <tbody>
            {holders.map((h) => (
              <tr key={`${h.wallet}-${h.side}`}>
                <td className="mono">{h.wallet.slice(0, 6)}…{h.wallet.slice(-4)}</td>
                <td>{h.side}</td>
                <td>${Math.round(h.position_usd).toLocaleString()}</td>
                <td>
                  {h.fetch_status === "error" ? (
                    <span className="tier-pill error-pill" title="Polymarket's API errored fetching this wallet's history — will retry">
                      API error
                    </span>
                  ) : (
                    <span className="tier-pill" style={{ background: tierColor(h.tier) }}>{h.tier}</span>
                  )}
                </td>
                <td>{h.score_percentile != null ? h.score_percentile.toFixed(0) : "—"}</td>
                <td>{formatTenure(h.tenure_days)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </section>
    </div>
  );
}
