import { useState } from "react";
import { api, fmt, verdict } from "../api";
import { EquityChart } from "../components/charts";
import { Badge, PageHead, ResultStats } from "../components/ui";

const CATEGORIES = ["Sports", "Politics", "Crypto", "Economy", "Culture", "World", "Tech", "Weather", "Other"];

const PRESETS = [
  { label: "Strong favourites", form: { side: "favourite", price_min: 0.85, price_max: 0.95 } },
  { label: "Long shots", form: { side: "underdog", price_min: 0.05, price_max: 0.15 } },
  { label: "Favourites, last 3 days", form: { side: "favourite", price_min: 0.7, price_max: 0.95, days_to_end_max: 3 } },
  { label: "Buy the dip", form: { side: "favourite", price_min: 0.5, price_max: 0.9, momentum_max: -0.08 } },
  { label: "Follow a rally", form: { side: "favourite", price_min: 0.5, price_max: 0.9, momentum_min: 0.08 } },
];

const EMPTY = {
  side: "favourite",
  price_min: 0.8,
  price_max: 0.95,
  categories: [],
  days_to_end_min: "",
  days_to_end_max: "",
  momentum_min: "",
  momentum_max: "",
  half_spread: 0.01,
  fees: true,
};

function clean(form) {
  const out = { ...form };
  for (const k of ["days_to_end_min", "days_to_end_max", "momentum_min", "momentum_max"]) {
    out[k] = out[k] === "" || out[k] == null ? null : Number(out[k]);
  }
  for (const k of ["price_min", "price_max", "half_spread"]) out[k] = Number(out[k]);
  return out;
}

export default function Lab() {
  const [form, setForm] = useState(EMPTY);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const toggleCat = (c) =>
    setForm((f) => ({ ...f, categories: f.categories.includes(c) ? f.categories.filter((x) => x !== c) : [...f.categories, c] }));

  async function run(next = form) {
    setRunning(true);
    setError(null);
    try {
      setResult(await api.runLab(clean(next)));
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  }

  function preset(p) {
    const next = { ...EMPTY, ...p.form };
    setForm(next);
    run(next);
  }

  const v = result && verdict(result.train.stats, result.validation.stats);

  return (
    <>
      <PageHead title="Strategy lab">
        Describe a simple rule and see how it would have done. Results come back in about a second, with costs
        included, on two separate periods.
      </PageHead>

      <div className="card">
        <h2>Rule</h2>
        <p className="sub">Start from a preset or set your own conditions. One trade per market, $100 each, held to resolution.</p>
        <div className="presets">
          {PRESETS.map((p) => (
            <button key={p.label} className="chip" onClick={() => preset(p)}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="form-grid">
          <div className="field">
            <label>Buy which side</label>
            <select value={form.side} onChange={set("side")}>
              <option value="favourite">The favourite (priced above 50¢)</option>
              <option value="underdog">The underdog (priced below 50¢)</option>
              <option value="outcome0">Always YES / first outcome</option>
              <option value="outcome1">Always NO / second outcome</option>
            </select>
          </div>
          <div className="field">
            <label>Price of that side (0–1)</label>
            <div className="pair">
              <input type="number" step="0.01" min="0.01" max="0.99" value={form.price_min} onChange={set("price_min")} />
              <span>to</span>
              <input type="number" step="0.01" min="0.01" max="0.99" value={form.price_max} onChange={set("price_max")} />
            </div>
          </div>
          <div className="field">
            <label>Days until scheduled end</label>
            <div className="pair">
              <input type="number" placeholder="any" value={form.days_to_end_min} onChange={set("days_to_end_min")} />
              <span>to</span>
              <input type="number" placeholder="any" value={form.days_to_end_max} onChange={set("days_to_end_max")} />
            </div>
          </div>
          <div className="field">
            <label>Price change over last 2 days</label>
            <div className="pair">
              <input type="number" step="0.01" placeholder="any" value={form.momentum_min} onChange={set("momentum_min")} />
              <span>to</span>
              <input type="number" step="0.01" placeholder="any" value={form.momentum_max} onChange={set("momentum_max")} />
            </div>
          </div>
          <div className="field">
            <label>Assumed half-spread (cost per share)</label>
            <input type="number" step="0.005" min="0" max="0.1" value={form.half_spread} onChange={set("half_spread")} />
          </div>
          <div className="field">
            <label>Fees</label>
            <select value={form.fees ? "on" : "off"} onChange={(e) => setForm((f) => ({ ...f, fees: e.target.value === "on" }))}>
              <option value="on">Current Polymarket taker fees</option>
              <option value="off">No fees</option>
            </select>
          </div>
        </div>
        <div className="field" style={{ marginTop: 14 }}>
          <label>Categories (none selected = all)</label>
          <div className="chips">
            {CATEGORIES.map((c) => (
              <button key={c} className={`chip ${form.categories.includes(c) ? "on" : ""}`} onClick={() => toggleCat(c)}>
                {c}
              </button>
            ))}
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => run()} disabled={running}>
            {running ? "Running…" : "Run backtest"}
          </button>
          <button className="btn ghost" onClick={() => { setForm(EMPTY); setResult(null); }}>
            Reset
          </button>
          {error && <span className="error">{error}</span>}
        </div>
      </div>

      {result && (
        <>
          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <h2>Result</h2>
              <Badge kind={v.kind}>{v.label}</Badge>
            </div>
            <p className="sub">
              A rule counts as an edge only if it is clearly profitable on both periods. A random pick on the unseen
              period returned {fmt.pct(result.baseline_random_validation.roi_pct)} under the same costs.
            </p>
            <div className="grid cols-2">
              <div>
                <strong>Training period</strong>
                <ResultStats stats={result.train.stats} />
              </div>
              <div>
                <strong>Unseen period</strong>
                <ResultStats stats={result.validation.stats} />
              </div>
            </div>
          </div>
          <div className="card">
            <h2>Cumulative profit</h2>
            <p className="sub">Profit booked on the day each market resolved.</p>
            <EquityChart
              series={[
                { name: "Training period", points: result.train.equity },
                { name: "Unseen period", points: result.validation.equity },
              ]}
            />
          </div>
        </>
      )}
    </>
  );
}
