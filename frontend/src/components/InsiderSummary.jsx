import { tierColor, timeAgo } from "../format";

// Market-level roll-up of app/insiders.py's per-holder flags (see
// market_service.build_market_detail -> insider_summary). Unvalidated
// heuristic -- a lead to look into, not a verdict.
function labelColor(label) {
  if (!label) return "var(--neutral)";
  if (label === "Insider activity on both sides") return "var(--danger)";
  if (label.startsWith("Insiders lean")) return "var(--warning)";
  return "var(--neutral)"; // No significant insider activity
}

export default function InsiderSummary({ summary }) {
  if (!summary) return null;
  const color = labelColor(summary.label);
  const hasActivity = summary.label !== "No significant insider activity";

  return (
    <section className="insider-summary-card" style={hasActivity ? { borderLeftColor: color } : undefined}>
      <div className="insider-summary-header">
        <h3>Insider activity</h3>
        <div className="verdict-badge" style={{ borderColor: color, color }}>
          <span className="verdict-label">{summary.label}</span>
          <span className="verdict-conviction" title="Conviction (0-100) -- how much + how severe, not which side">
            {summary.conviction.toFixed(0)}
          </span>
        </div>
      </div>

      {hasActivity && (
        <>
          <div className="market-meta insider-summary-breakdown">
            <span>YES: ${Math.round(summary.yes_flagged_usd).toLocaleString()} across {summary.yes_flag_count} flag{summary.yes_flag_count === 1 ? "" : "s"}</span>
            <span>NO: ${Math.round(summary.no_flagged_usd).toLocaleString()} across {summary.no_flag_count} flag{summary.no_flag_count === 1 ? "" : "s"}</span>
          </div>
          {summary.top_flags.length > 0 && (
            <ul className="insider-reasons insider-summary-flags">
              {summary.top_flags.map((f, i) => (
                <li key={i}>
                  <span className="mono">{f.wallet.slice(0, 6)}…{f.wallet.slice(-4)}</span>
                  {" "}{f.side} ${Math.round(f.position_usd).toLocaleString()} — insider score {f.insider_score.toFixed(0)}
                  {" "}
                  <span
                    className="tier-pill"
                    style={{ background: tierColor(f.tier) }}
                    title="This wallet's Smart Score — historical skill, independent of this insider flag"
                  >
                    {f.tier}{f.score_percentile != null ? ` · ${f.score_percentile.toFixed(0)}` : ""}
                  </span>
                  {" "}({f.reasons[0]})
                  {f.first_seen_at != null && (
                    <span className="stat-footnote" title="When this system first observed the position -- not necessarily the exact trade time">
                      {" "}· first seen {timeAgo(f.first_seen_at)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <p className="stat-footnote">
        First-pass, unvalidated heuristic (app/insiders.py) — a lead to investigate, not a
        determination of wrongdoing. Full detail on the Insiders tab.
      </p>
    </section>
  );
}
