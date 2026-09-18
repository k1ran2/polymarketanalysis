import { PieChart, Pie, Cell, Tooltip, Legend } from "recharts";
import { useChartWidth } from "../useChartWidth";
import { tooltipStyle, legendStyle, CHART_COLORS } from "../chartTheme";

const COLORS = [CHART_COLORS.danger, CHART_COLORS.warning, CHART_COLORS.accent, CHART_COLORS.neutral];

export default function WhalePie({ title, concentration }) {
  const [ref, width] = useChartWidth();

  const data = (concentration?.buckets ?? [])
    .map((b) => ({ name: b.label, value: b.usd_share * 100, holders: b.holder_count }))
    .filter((d) => d.value > 0.01);

  const w = Math.min(width || 300, 360);

  return (
    <div className="whale-pie" ref={ref}>
      <h4>{title}</h4>
      {data.length === 0 || !width ? (
        <div className="empty-state">No holders on this side yet.</div>
      ) : (
        <PieChart width={w} height={240}>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy={95}
            outerRadius={82}
            startAngle={90}
            endAngle={-270}
            isAnimationActive={false}
            stroke="#12151c"
            strokeWidth={2}
          >
            {data.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
          </Pie>
          <Tooltip {...tooltipStyle} formatter={(v, _n, e) => [`${v.toFixed(1)}%  (${e.payload.holders} holders)`, e.payload.name]} />
          <Legend {...legendStyle} />
        </PieChart>
      )}
      <div className="whale-stats">
        <span>Gini: <strong>{concentration?.gini != null ? concentration.gini.toFixed(2) : "—"}</strong></span>
        <span>avg score top 1%: <strong>{concentration?.avg_score_top1 != null ? concentration.avg_score_top1.toFixed(0) : "—"}</strong></span>
        <span>avg score rest: <strong>{concentration?.avg_score_rest != null ? concentration.avg_score_rest.toFixed(0) : "—"}</strong></span>
      </div>
    </div>
  );
}
