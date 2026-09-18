import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ReferenceArea, Legend,
} from "recharts";
import { useChartWidth } from "../useChartWidth";
import { tooltipStyle, legendStyle, axisColor, gridColor, CHART_COLORS } from "../chartTheme";

// §3.6: $-weighted histogram of signed holder scores, split by side, with a
// normal-curve overlay for comparison.
//
// Each holder's money lands on the x-axis at (score-50), flipped negative if
// they're on NO -- so the *sign* alone can't tell you which side a bar came
// from (a skilled YES holder and an unskilled NO holder can land at the same
// spot). Stacking YES (blue) and NO (red) within each bin, instead of a
// single merged bar, removes that ambiguity.
function buildHistogram(points, meanVal, stdev, binCount = 20) {
  if (!points || points.length === 0) return { bins: [] };

  const min = Math.min(-60, ...points.map((p) => p.signed_score));
  const max = Math.max(60, ...points.map((p) => p.signed_score));
  const binWidth = (max - min) / binCount || 1;

  const bins = Array.from({ length: binCount }, (_, i) => ({
    x: min + (i + 0.5) * binWidth,
    yesWeight: 0,
    noWeight: 0,
  }));

  let totalWeight = 0;
  for (const p of points) {
    let idx = Math.floor((p.signed_score - min) / binWidth);
    idx = Math.max(0, Math.min(binCount - 1, idx));
    if (p.side === "YES") bins[idx].yesWeight += p.weight;
    else bins[idx].noWeight += p.weight;
    totalWeight += p.weight;
  }

  if (stdev && stdev > 0) {
    for (const b of bins) {
      const z = (b.x - meanVal) / stdev;
      b.curveWeight = (Math.exp(-0.5 * z * z) / (stdev * Math.sqrt(2 * Math.PI))) * binWidth * totalWeight;
    }
  }
  return { bins };
}

function plainEnglishSummary(mean, skew) {
  if (mean == null) return null;
  const lean = Math.abs(mean) < 2 ? "is roughly balanced between YES and NO" : mean > 0
    ? `leans YES overall (money-weighted average conviction +${mean.toFixed(0)})`
    : `leans NO overall (money-weighted average conviction ${mean.toFixed(0)})`;
  let tail = "";
  if (skew != null && Math.abs(skew) > 0.4) {
    const dir = skew > 0 ? "YES" : "NO";
    tail = ` A handful of high-conviction ${dir} bets pull the tail further right than a typical bell curve would.`;
  }
  return `Rated money on this market ${lean}.${tail}`;
}

export default function SignedHistogram({ distribution }) {
  const [ref, width] = useChartWidth();
  const empty = !distribution || distribution.n === 0;
  const { points, weighted_mean, weighted_stdev, skewness } = distribution || {};
  const { bins } = empty ? { bins: [] } : buildHistogram(points, weighted_mean, weighted_stdev);
  const summary = empty ? null : plainEnglishSummary(weighted_mean, skewness);

  return (
    <div ref={ref}>
      {empty ? (
        <div className="empty-state">No rated holders yet to build a distribution.</div>
      ) : (
        <>
          {summary && <p className="chart-caption">{summary}</p>}
          {width > 0 && (
            <ComposedChart width={width} height={280} data={bins} margin={{ top: 10, right: 20, bottom: 10, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
              <XAxis
                dataKey="x"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(v) => v.toFixed(0)}
                tick={{ fill: axisColor, fontSize: 11 }}
                stroke={gridColor}
                label={{ value: "← more conviction on NO   |   more conviction on YES →", position: "insideBottom", offset: -5, fontSize: 11, fill: axisColor }}
              />
              <YAxis tickFormatter={(v) => `$${Math.round(v).toLocaleString()}`} width={70} tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor} />
              <Tooltip
                {...tooltipStyle}
                formatter={(value, name) => [
                  `$${Math.round(value).toLocaleString()}`,
                  { yesWeight: "$ from YES holders", noWeight: "$ from NO holders", curveWeight: "normal curve (reference)" }[name] || name,
                ]}
                labelFormatter={(v) => `conviction score ${v.toFixed(1)}`}
              />
              <Legend
                {...legendStyle}
                formatter={(name) => ({ yesWeight: "$ backing YES", noWeight: "$ backing NO", curveWeight: "normal curve (reference)" }[name] || name)}
              />
              {/* A shaded band, not text labels, marks ±1 stdev -- sits behind the bars so it reads as
                  context rather than competing with them. The exact numbers live in the stat strip
                  below, tied back here by matching swatch colors, instead of cramming labels onto the
                  chart where they collide whenever the mean sits close to 0 or to the band edge. */}
              {weighted_mean != null && weighted_stdev > 0 && (
                <ReferenceArea
                  x1={weighted_mean - weighted_stdev} x2={weighted_mean + weighted_stdev}
                  fill={CHART_COLORS.neutral} fillOpacity={0.09} stroke="none" ifOverflow="extendDomain"
                />
              )}
              <ReferenceLine x={0} stroke={axisColor} strokeDasharray="4 2" />
              <Bar dataKey="yesWeight" stackId="side" fill={CHART_COLORS.accent} name="yesWeight" isAnimationActive={false} radius={[0, 0, 0, 0]} />
              <Bar dataKey="noWeight" stackId="side" fill={CHART_COLORS.danger} name="noWeight" isAnimationActive={false} />
              <Line dataKey="curveWeight" stroke={CHART_COLORS.warning} strokeWidth={2} dot={false} name="curveWeight" isAnimationActive={false} />
              {/* Drawn after the bars/curve (Recharts layers children in order) so it sits on top instead of hidden underneath. */}
              {weighted_mean != null && (
                <ReferenceLine x={weighted_mean} stroke={CHART_COLORS.violet} strokeWidth={2} ifOverflow="extendDomain" />
              )}
            </ComposedChart>
          )}
        </>
      )}
      {!empty && (
        <div className="dist-stats">
          <div className="dist-stat" title="$-weighted average of (score-50), signed by side -- the line drawn on the chart above">
            <div className="dist-stat-label"><span className="dist-stat-swatch" style={{ background: CHART_COLORS.violet }} />mean (μ)</div>
            <div className="dist-stat-value">{weighted_mean != null ? weighted_mean.toFixed(1) : "—"}</div>
          </div>
          <div className="dist-stat" title="$-weighted spread around the mean -- the shaded band on the chart above; wider means conviction is more spread out, not concentrated near the mean">
            <div className="dist-stat-label"><span className="dist-stat-swatch" style={{ background: CHART_COLORS.neutral }} />±1 stdev (σ)</div>
            <div className="dist-stat-value">{weighted_stdev != null ? weighted_stdev.toFixed(1) : "—"}</div>
          </div>
          <div className="dist-stat" title="0 = symmetric; positive = a long tail of high-conviction YES bets pulling the curve rightward; negative = the same on NO">
            <div className="dist-stat-label">skewness</div>
            <div className="dist-stat-value">{skewness != null ? skewness.toFixed(2) : "—"}</div>
          </div>
          <div className="dist-stat" title="Number of holders with a Smart Score (Bronze+) counted in this distribution">
            <div className="dist-stat-label">n rated</div>
            <div className="dist-stat-value">{distribution.n}</div>
          </div>
        </div>
      )}
    </div>
  );
}
