import { useEffect, useState } from "react";
import { fmt } from "../api";

export function Stat({ label, value, hint, tone }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value ${tone || ""}`}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Badge({ kind, children }) {
  return <span className={`badge ${kind || ""}`}>{children}</span>;
}

export function PageHead({ title, children }) {
  return (
    <div className="page-head">
      <h1>{title}</h1>
      <p>{children}</p>
    </div>
  );
}

/** Load once on mount; returns { data, error, loading }. */
export function useLoad(loader) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    let live = true;
    loader()
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((e) => live && setState({ data: null, error: e.message, loading: false }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return state;
}

export function LoadState({ state }) {
  if (state.loading) return <div className="empty">Loading…</div>;
  if (state.error) return <div className="card error">Could not load: {state.error}</div>;
  return null;
}

/** The standard result block for one period of one backtest. */
export function ResultStats({ stats }) {
  if (!stats?.n_trades) return <div className="empty">No trades matched.</div>;
  return (
    <div className="mini-stats">
      <div>
        <div className="label">Return on stake</div>
        <div className={`value ${fmt.sign(stats.roi_pct)}`}>{fmt.pct(stats.roi_pct)}</div>
        <div className="note">
          likely range {fmt.pct(stats.roi_lo)} to {fmt.pct(stats.roi_hi)}
        </div>
      </div>
      <div>
        <div className="label">Win rate vs price paid</div>
        <div className="value">
          {fmt.pctPlain(stats.win_rate * 100)} vs {fmt.pctPlain(stats.avg_cost * 100)}
        </div>
        <div className="note">edge {fmt.pct(stats.edge_pts, 2)} pts</div>
      </div>
      <div>
        <div className="label">Trades / independent events</div>
        <div className="value">
          {fmt.int(stats.n_trades)} / {fmt.int(stats.n_events)}
        </div>
      </div>
      <div>
        <div className="label">Profit on $100 stakes</div>
        <div className={`value ${fmt.sign(stats.pnl)}`}>{fmt.usd(stats.pnl)}</div>
        <div className="note">worst drop {fmt.usd(stats.max_dd)}</div>
      </div>
      <div>
        <div className="label">Sharpe</div>
        <div className="value">{fmt.num(stats.sharpe)}</div>
        <div className="note">avg hold {fmt.num(stats.avg_hold_days, 1)} days</div>
      </div>
    </div>
  );
}

/** Click-to-sort table. columns: [{ key, label, num, render }] */
export function DataTable({ columns, rows, initialSort, initialDir = "desc", limit }) {
  const [sort, setSort] = useState({ key: initialSort, dir: initialDir });
  const sorted = [...rows];
  if (sort.key) {
    sorted.sort((a, b) => {
      const x = a[sort.key];
      const y = b[sort.key];
      if (x == null) return 1;
      if (y == null) return -1;
      const c = typeof x === "number" ? x - y : String(x).localeCompare(String(y));
      return sort.dir === "asc" ? c : -c;
    });
  }
  const shown = limit ? sorted.slice(0, limit) : sorted;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={`sortable ${c.num ? "num" : ""}`}
                onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key && s.dir === "desc" ? "asc" : "desc" }))}
              >
                {c.label}
                {sort.key === c.key ? (sort.dir === "desc" ? " ↓" : " ↑") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c.key} className={c.num ? "num" : ""}>
                  {c.render ? c.render(r[c.key], r) : r[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
