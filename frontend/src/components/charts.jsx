import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fmt } from "../api";

const axis = { stroke: "var(--axis)", tick: { fill: "var(--muted)", fontSize: 11 }, tickLine: false };
const grid = <CartesianGrid stroke="var(--grid)" vertical={false} />;

function Tip({ active, payload, label, title, rows }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="chart-tip">
      <div style={{ fontWeight: 600, marginBottom: 2 }}>{title ? title(label, p) : label}</div>
      {rows(p).map(([k, v]) => (
        <div key={k}>
          <span className="k">{k}: </span>
          {v}
        </div>
      ))}
    </div>
  );
}

/** Cumulative profit over time for one or two periods (each its own series). */
export function EquityChart({ series }) {
  const data = [];
  series.forEach((s, i) => s.points.forEach((p) => data.push({ t: p.t, [`s${i}`]: p.pnl, name: s.name })));
  data.sort((a, b) => a.t - b.t);
  if (!data.length) return <div className="empty">No trades to plot.</div>;
  return (
    <>
      {series.length > 1 && (
        <div className="legend">
          {series.map((s, i) => (
            <span key={s.name}>
              <i className={i ? "s2" : ""} />
              {s.name}
            </span>
          ))}
        </div>
      )}
      <div className="chart">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 4 }}>
            {grid}
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(t) => fmt.date(t)} {...axis} minTickGap={60} />
            <YAxis tickFormatter={(v) => fmt.usdCompact(v)} {...axis} axisLine={false} width={52} />
            <ReferenceLine y={0} stroke="var(--axis)" />
            <Tooltip
              content={<Tip title={(t) => fmt.date(t)} rows={(p) => [[p.name, fmt.usd(p.s0 ?? p.s1)]]} />}
              cursor={{ stroke: "var(--axis)" }}
            />
            {series.map((s, i) => (
              <Line key={s.name} dataKey={`s${i}`} stroke={i ? "var(--series-2)" : "var(--series-1)"} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

/** Price paid vs how often that side actually won. On the dashed line = fairly priced. */
export function CalibrationChart({ points }) {
  const data = points.map((p) => ({ ...p, price: p.price * 100, win: p.win * 100, fair: p.price * 100 }));
  return (
    <>
      <div className="legend">
        <span>
          <i />
          Actual win rate
        </span>
        <span>
          <i className="dash" />
          Fair (win rate = price)
        </span>
      </div>
      <div className="chart">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 16, left: 4 }}>
            {grid}
            <XAxis dataKey="price" type="number" domain={[0, 100]} ticks={[0, 20, 40, 60, 80, 100]} tickFormatter={(v) => v + "¢"} {...axis}
              label={{ value: "Price", position: "insideBottom", offset: -8, fill: "var(--muted)", fontSize: 11 }} />
            <YAxis domain={[0, 100]} ticks={[0, 20, 40, 60, 80, 100]} tickFormatter={(v) => v + "%"} {...axis} axisLine={false} width={40} />
            <Tooltip
              content={
                <Tip
                  title={(_, p) => `Priced around ${p.price.toFixed(0)}¢`}
                  rows={(p) => [
                    ["Won", p.win.toFixed(1) + "%"],
                    ["Gap", (p.win - p.price > 0 ? "+" : "") + (p.win - p.price).toFixed(1) + " pts"],
                    ["Markets", fmt.int(p.n_markets)],
                  ]}
                />
              }
            />
            <Line dataKey="fair" stroke="var(--muted)" strokeDasharray="4 3" strokeWidth={1} dot={false} isAnimationActive={false} />
            <Line dataKey="win" stroke="var(--series-1)" strokeWidth={2} dot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface)", strokeWidth: 2 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

/** One bar per group; value already in display units. */
export function GroupBars({ data, xKey, yKey, yFormat, tipRows, xLabel, baseline }) {
  return (
    <div className="chart">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 12, bottom: 16, left: 4 }} barCategoryGap="22%">
          {grid}
          <XAxis dataKey={xKey} {...axis} label={{ value: xLabel, position: "insideBottom", offset: -8, fill: "var(--muted)", fontSize: 11 }} />
          <YAxis tickFormatter={yFormat} {...axis} axisLine={false} width={44} />
          {baseline != null && <ReferenceLine y={baseline} stroke="var(--muted)" strokeDasharray="4 3" />}
          <Tooltip content={<Tip title={(l) => `Group ${l}`} rows={tipRows} />} cursor={{ fill: "var(--series-1-soft)" }} />
          <Bar dataKey={yKey} fill="var(--series-1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
