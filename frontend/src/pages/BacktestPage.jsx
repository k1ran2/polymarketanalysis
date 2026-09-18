import { useEffect, useRef, useState } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
} from "recharts";
import {
  browseResolvedMarkets, createBacktestRun, createBacktestSearch, fetchBacktestCategories, fetchBacktestRun,
  fetchBacktestRuns, fetchBacktestSearch, fetchStrategyDefaults,
} from "../api";
import { timeAgo } from "../format";
import { useChartWidth } from "../useChartWidth";
import { tooltipStyle, axisColor, gridColor, CHART_COLORS } from "../chartTheme";

const SIGNAL_STRENGTH_OPTIONS = [
  { value: "strong_buy", label: "Strong Buy only (highest conviction)" },
  { value: "lean", label: "Strong Buy + Lean" },
  { value: "fade", label: "Strong Buy + Lean + Fade" },
];

function fmtUsd(n) {
  return n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(Math.round(n)).toLocaleString()}`;
}

const CURRENT_YEAR = new Date().getFullYear();
const RESOLVED_YEARS = Array.from({ length: CURRENT_YEAR - 2019 }, (_, i) => CURRENT_YEAR - i);

function buildEquityCurve(trades) {
  const sorted = [...trades].sort((a, b) => a.signal_ts - b.signal_ts);
  let cum = 0;
  return sorted.map((t, i) => {
    cum += t.pnl_usd;
    return { i: i + 1, ts: t.signal_ts * 1000, cumPnl: cum };
  });
}

function fmtChartDate(ms) {
  return new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short" });
}

// The editable subset of a strategy's params is exactly the keys it has a
// label for (VerdictOverrides/InsiderOverrides on the backend) -- everything
// else in `params` is shown read-only as methodology reference.
function pickEditable(strategyInfo) {
  const out = {};
  for (const key of Object.keys(strategyInfo?.param_labels || {})) {
    out[key] = strategyInfo.params[key];
  }
  return out;
}

function humanizeKey(key) {
  return key.replace(/_/g, " ");
}

// Formula + editable weights for one strategy, fetched from
// GET /api/backtest/strategy-defaults so this can never drift from what
// config.py actually specifies. Editing a value here only ever affects the
// NEXT run started from this page (sent as verdict_overrides/
// insider_overrides) -- the live weights every other tab reads are read-only
// reference here, shown for full transparency about what the strategy does.
function MethodologyPanel({ info, weights, setWeights, onReset }) {
  if (!info) return null;
  const editableKeys = Object.keys(info.param_labels);
  const readonlyKeys = Object.keys(info.params).filter((k) => !editableKeys.includes(k));
  const isDefault = editableKeys.every((k) => weights[k] === info.params[k]);

  return (
    <div className="insiders-market-stats" style={{ marginTop: 10 }}>
      <p className="chart-caption" style={{ marginTop: 0 }}>{info.description}</p>

      <h4 style={{ margin: "14px 0 8px", fontSize: "0.85rem", fontWeight: 650 }}>
        Weights &amp; thresholds (editable — applies to your next run only)
      </h4>
      <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
        {editableKeys.map((key) => (
          <label key={key} title={info.param_labels[key]}>
            {humanizeKey(key)}:
            <input
              type="number" step="any" value={weights[key] ?? ""}
              onChange={(e) => setWeights((prev) => ({ ...prev, [key]: e.target.value === "" ? "" : Number(e.target.value) }))}
              style={{ width: 80 }}
            />
          </label>
        ))}
        {!isDefault && (
          <button className="back-link" onClick={onReset}>Reset to defaults</button>
        )}
      </div>

      {readonlyKeys.length > 0 && (
        <>
          <h4 style={{ margin: "14px 0 8px", fontSize: "0.85rem", fontWeight: 650 }}>
            Other thresholds (fixed — not yet editable here)
          </h4>
          <div className="market-meta">
            {readonlyKeys.map((key) => (
              <span key={key}>{humanizeKey(key)}: <strong>{info.params[key]}</strong></span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function BacktestPage() {
  const [pickMode, setPickMode] = useState("auto"); // "auto" | "manual"
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [year, setYear] = useState("");
  const [categories, setCategories] = useState([]);
  const [candidates, setCandidates] = useState(null);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [selected, setSelected] = useState([]); // [{condition_id, question}]

  // Auto-select controls: test a strategy across a spread of markets instead
  // of hand-picking each one -- categories empty means "all of them".
  const [autoCategories, setAutoCategories] = useState([]);
  const [autoYearMin, setAutoYearMin] = useState(2020);
  const [autoYearMax, setAutoYearMax] = useState(CURRENT_YEAR);
  const [autoCount, setAutoCount] = useState(30);

  const [strategy, setStrategy] = useState("smart_money");
  const [stakeUsd, setStakeUsd] = useState(100);
  const [minSignalStrength, setMinSignalStrength] = useState("strong_buy");
  const [minConviction, setMinConviction] = useState("");
  const [noEdgeThreshold, setNoEdgeThreshold] = useState(0.98);
  const [error, setError] = useState(null);

  // Strategy methodology + editable weights -- fetched from the backend so
  // this can never drift from what config.py actually specifies. Editing a
  // field here sends it as a per-run override (verdict_overrides/
  // insider_overrides); the live production weights everything else on the
  // site uses are never touched.
  const [strategyDefaults, setStrategyDefaults] = useState(null);
  const [verdictWeights, setVerdictWeights] = useState({});
  const [insiderWeights, setInsiderWeights] = useState({});
  const [showWeights, setShowWeights] = useState(false);

  // Weight sweep: runs the SAME market set once per candidate value of one
  // parameter, so results are actually comparable -- the real way to tell
  // whether a weight matters, instead of eyeballing single runs one at a
  // time (a handful of trades from one run can't tell you much either way).
  const [sweepParam, setSweepParam] = useState("");
  const [sweepValuesInput, setSweepValuesInput] = useState("");
  const [sweepRunning, setSweepRunning] = useState(false);
  const [sweepProgress, setSweepProgress] = useState(null);
  const [sweepResults, setSweepResults] = useState([]);
  const sweepStopRef = useRef(false);

  // ML-style weight search: random search over one or more parameter ranges
  // at once, tuned on a TRAIN slice of the market selection and graded, once,
  // on a held-out TEST slice never touched during tuning -- the sweep above
  // answers "does this one parameter matter", this answers "what's the best
  // combination, and does it actually generalize or just fit this history".
  const [searchRanges, setSearchRanges] = useState({}); // {key: {enabled, min, max}}
  const [searchNIterations, setSearchNIterations] = useState(15);
  const [searchTestFraction, setSearchTestFraction] = useState(0.3);
  const [searchSeed, setSearchSeed] = useState("");
  const [searchRunning, setSearchRunning] = useState(false);
  const [searchResult, setSearchResult] = useState(null);
  const searchPollTimer = useRef(null);

  // Two independent tracks, each with its own poll loop, so browsing a past
  // run never gets clobbered by a still-running backtest's own polling (and
  // vice versa): `displayedRunId`/`run` is whatever the results section
  // below is showing; `liveRunId`/`liveRun` is a run started from this page
  // that's still in flight, tracked purely so its progress/completion can
  // be surfaced even while a different run is being viewed.
  const [displayedRunId, setDisplayedRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [liveRunId, setLiveRunId] = useState(null);
  const [liveRun, setLiveRun] = useState(null);
  const [pastRuns, setPastRuns] = useState(null);
  const viewPollTimer = useRef(null);
  const livePollTimer = useRef(null);
  const displayedIdRef = useRef(null); // mirrors displayedRunId for the async poll callbacks below, to reject a stale in-flight response after the user switches to viewing a different run
  const [chartRef, chartWidth] = useChartWidth();
  const [sweepChartRef, sweepChartWidth] = useChartWidth();
  const [searchChartRef, searchChartWidth] = useChartWidth();

  useEffect(() => {
    loadPastRuns();
    fetchBacktestCategories().then((d) => setCategories(d.categories)).catch(() => {});
    fetchStrategyDefaults().then((d) => {
      setStrategyDefaults(d);
      setVerdictWeights(pickEditable(d.smart_money));
      setInsiderWeights(pickEditable(d.insider));
      if (d.general?.params?.max_no_edge_entry_price != null) {
        setNoEdgeThreshold(d.general.params.max_no_edge_entry_price);
      }
    }).catch(() => {});
    doSearch(); // show a default browsable list immediately, not only after a search
    return () => {
      clearTimeout(viewPollTimer.current);
      clearTimeout(livePollTimer.current);
      clearTimeout(searchPollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function loadPastRuns() {
    fetchBacktestRuns(10).then((d) => setPastRuns(d.runs)).catch(() => {});
  }

  function doSearch(overrides = {}) {
    setError(null);
    setCandidatesLoading(true);
    const s = overrides.search !== undefined ? overrides.search : search;
    const cat = overrides.category !== undefined ? overrides.category : category;
    const yr = overrides.year !== undefined ? overrides.year : year;
    browseResolvedMarkets({
      limit: 25,
      search: s || undefined,
      category: cat || undefined,
      endDateMin: yr ? `${yr}-01-01` : undefined,
      endDateMax: yr ? `${yr}-12-31` : undefined,
    })
      .then((d) => setCandidates(d.markets))
      .catch((e) => setError(e.message))
      .finally(() => setCandidatesLoading(false));
  }

  function toggleSelect(m) {
    setSelected((prev) => {
      const exists = prev.find((x) => x.condition_id === m.condition_id);
      if (exists) return prev.filter((x) => x.condition_id !== m.condition_id);
      if (prev.length >= 200) return prev;
      return [...prev, m];
    });
  }

  // Shared by a normal run and each point of a sweep (below) so both submit
  // an identical request shape -- `fixedConditionIds`/`weightOverrides` let
  // the sweep pin the market set and vary just one parameter across points.
  function buildRunBody({ fixedConditionIds, weightOverrides } = {}) {
    const body = {
      strategy,
      stake_usd: Number(stakeUsd) || 100,
      min_signal_strength: strategy === "smart_money" ? minSignalStrength : "strong_buy",
    };
    if (fixedConditionIds) {
      body.condition_ids = fixedConditionIds;
    } else if (pickMode === "manual") {
      body.condition_ids = selected.map((m) => m.condition_id);
    } else {
      body.market_selection = {
        categories: autoCategories,
        year_min: Number(autoYearMin),
        year_max: Number(autoYearMax),
        count: Number(autoCount) || 30,
      };
    }
    if (minConviction !== "") body.min_conviction = Number(minConviction);
    body.max_no_edge_entry_price = noEdgeThreshold;
    const overrides = weightOverrides || (strategy === "smart_money" ? verdictWeights : insiderWeights);
    if (strategy === "smart_money") body.verdict_overrides = overrides;
    else body.insider_overrides = overrides;
    return body;
  }

  async function handleRun() {
    setError(null);
    if (pickMode === "manual" && selected.length === 0) {
      setError("Pick at least one resolved market to backtest.");
      return;
    }
    try {
      const body = buildRunBody();
      const r = await createBacktestRun(body);
      if (!r.started) {
        setError(
          "A different backtest is already running (only one runs at a time) — showing its progress "
          + "below instead of starting yours. Try again once it finishes."
        );
      }
      setLiveRunId(r.run_id);
      setLiveRun(null);
      clearTimeout(livePollTimer.current);
      pollLive(r.run_id);
      viewRun(r.run_id);
    } catch (e) {
      setError(e.message);
    }
  }

  function parseSweepValues(input) {
    return input.split(",").map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
  }

  // A sane starting range for a parameter's search box, centered on its live
  // default -- 0-1 weights sweep the full 0-1 span (there's nowhere else
  // meaningful to look), everything else sweeps ±40% of the default so a
  // first search doesn't need the user to already know good bounds.
  function defaultRangeFor(defaultValue) {
    const v = Number(defaultValue) || 0;
    if (v >= 0 && v <= 1) return { min: 0, max: 1 };
    return { min: Math.round(v * 0.6 * 100) / 100, max: Math.round(v * 1.4 * 100) / 100 };
  }

  function toggleSearchParam(key, defaultValue) {
    setSearchRanges((prev) => {
      const existing = prev[key];
      if (existing?.enabled) return { ...prev, [key]: { ...existing, enabled: false } };
      const range = existing || defaultRangeFor(defaultValue);
      return { ...prev, [key]: { ...range, enabled: true } };
    });
  }

  function buildSearchBody() {
    const info = strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"];
    const param_space = {};
    for (const key of Object.keys(info.param_labels)) {
      const r = searchRanges[key];
      if (!r?.enabled) continue;
      const lo = Number(r.min);
      const hi = Number(r.max);
      if (Number.isNaN(lo) || Number.isNaN(hi) || lo === hi) continue;
      param_space[key] = [Math.min(lo, hi), Math.max(lo, hi)];
    }
    const body = {
      strategy,
      test_fraction: Number(searchTestFraction) || 0.3,
      n_iterations: Number(searchNIterations) || 15,
      param_space,
      stake_usd: Number(stakeUsd) || 100,
      min_signal_strength: strategy === "smart_money" ? minSignalStrength : "strong_buy",
      max_no_edge_entry_price: noEdgeThreshold,
    };
    if (minConviction !== "") body.min_conviction = Number(minConviction);
    if (searchSeed !== "") body.seed = Number(searchSeed);
    if (pickMode === "manual") {
      body.condition_ids = selected.map((m) => m.condition_id);
    } else {
      body.market_selection = {
        categories: autoCategories,
        year_min: Number(autoYearMin),
        year_max: Number(autoYearMax),
        count: Number(autoCount) || 30,
      };
    }
    return body;
  }

  function pollSearch(id) {
    fetchBacktestSearch(id).then((s) => {
      setSearchResult(s);
      if (s.status === "pending" || s.status === "running") {
        searchPollTimer.current = setTimeout(() => pollSearch(id), 3000);
      } else {
        setSearchRunning(false);
        loadPastRuns();
      }
    }).catch((e) => {
      setError(e.message);
      setSearchRunning(false);
    });
  }

  async function handleRunSearch() {
    setError(null);
    const body = buildSearchBody();
    if (Object.keys(body.param_space).length === 0) {
      setError("Check at least one parameter below and give it a range to search over.");
      return;
    }
    if (pickMode === "manual" && selected.length < 4) {
      setError("Pick at least 4 markets — a train/test split needs enough markets on both sides to mean anything.");
      return;
    }
    try {
      const r = await createBacktestSearch(body);
      if (!r.started) {
        setError("A different backtest or search is already running (only one runs at a time). Try again once it finishes.");
        return;
      }
      setSearchResult(null);
      setSearchRunning(true);
      clearTimeout(searchPollTimer.current);
      pollSearch(r.search_id);
    } catch (e) {
      setError(e.message);
    }
  }

  async function pollUntilDone(runId) {
    for (;;) {
      const r = await fetchBacktestRun(runId);
      if (r.status !== "pending" && r.status !== "running") return r;
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }

  async function runSweep() {
    const values = parseSweepValues(sweepValuesInput);
    if (!sweepParam || values.length === 0) {
      setError("Pick a parameter and enter at least one value to sweep (comma-separated).");
      return;
    }
    if (pickMode === "manual" && selected.length === 0) {
      setError("Pick at least one resolved market to backtest.");
      return;
    }
    setError(null);
    setSweepResults([]);
    setSweepRunning(true);
    sweepStopRef.current = false;
    // Auto-select mode would otherwise pick a DIFFERENT random market sample
    // for every point -- pinning the set from the first point's resolved
    // list is what makes the rest of the sweep an apples-to-apples comparison.
    let fixedConditionIds = pickMode === "manual" ? selected.map((m) => m.condition_id) : null;

    for (let i = 0; i < values.length; i++) {
      if (sweepStopRef.current) break;
      const value = values[i];
      setSweepProgress({ i: i + 1, total: values.length, value });
      const baseOverrides = strategy === "smart_money" ? verdictWeights : insiderWeights;
      const body = buildRunBody({ fixedConditionIds, weightOverrides: { ...baseOverrides, [sweepParam]: value } });
      try {
        const started = await createBacktestRun(body);
        if (!started.started) {
          setError("A different backtest was already running, so the sweep couldn't start its next point. Stopped early.");
          break;
        }
        const finished = await pollUntilDone(started.run_id);
        if (!fixedConditionIds) fixedConditionIds = finished.params.condition_ids;
        setSweepResults((prev) => [...prev, { value, runId: started.run_id, status: finished.status, summary: finished.summary, error: finished.error }]);
      } catch (e) {
        setSweepResults((prev) => [...prev, { value, error: e.message }]);
      }
    }
    setSweepRunning(false);
    setSweepProgress(null);
    loadPastRuns();
  }

  // Drives the results section (`run`) -- whatever the user last chose to look at.
  function pollDisplayed(id) {
    fetchBacktestRun(id).then((r) => {
      setRun((prev) => (id === displayedIdRef.current ? r : prev));
      if (r.status === "pending" || r.status === "running") {
        viewPollTimer.current = setTimeout(() => pollDisplayed(id), 3000);
      }
    }).catch((e) => setError(e.message));
  }

  // Drives the background "a backtest is running" banner + Past runs refresh,
  // independent of whatever's currently displayed.
  function pollLive(id) {
    fetchBacktestRun(id).then((r) => {
      setLiveRun(r);
      if (r.status === "pending" || r.status === "running") {
        livePollTimer.current = setTimeout(() => pollLive(id), 3000);
      } else {
        setLiveRunId(null);
        loadPastRuns();
      }
    }).catch(() => {});
  }

  function viewRun(id) {
    displayedIdRef.current = id;
    setDisplayedRunId(id);
    setRun(null);
    clearTimeout(viewPollTimer.current);
    pollDisplayed(id);
  }

  const trades = run?.trades || [];
  const equity = trades.length > 0 ? buildEquityCurve(trades) : [];
  const running = run && (run.status === "pending" || run.status === "running");
  const watchingLiveElsewhere = liveRunId && liveRunId !== displayedRunId;

  return (
    <div>
      <div className="insiders-header">
        <div>
          <h2>Backtest</h2>
          <p className="subtitle">
            Point-in-time replay of the Smart Money verdict and Insider-score signals against real,
            already-resolved markets — holders, Smart Scores and insider flags are all reconstructed
            using only data that existed as of each checkpoint, never today's numbers.
          </p>
        </div>
      </div>

      <div className="notice">
        <strong>Methodology note:</strong> entry price is Polymarket's last <em>traded</em> price nearest
        each checkpoint (CLOB price history) — not a true historical bid/ask, since Polymarket doesn't
        publish historical order-book depth. Results are therefore somewhat optimistic versus a real
        fill, especially on thin/illiquid markets. Only one simulated trade is taken per market (the
        first checkpoint whose signal clears the threshold), held to resolution — no early-exit
        modeling. The first backtest of any given market is slow (every historical trader who ever held
        it needs their own trading history fetched); re-running against the same market later is instant.
      </div>

      {error && <div className="error">Error: {error}</div>}

      <section>
        <h3>1. Pick markets</h3>
        <div className="insiders-controls" style={{ flexWrap: "wrap", marginBottom: 16 }}>
          <label>
            <input type="radio" name="pickMode" checked={pickMode === "auto"} onChange={() => setPickMode("auto")} />
            {" "}Test a strategy across many markets
          </label>
          <label>
            <input type="radio" name="pickMode" checked={pickMode === "manual"} onChange={() => setPickMode("manual")} />
            {" "}Pick specific markets myself
          </label>
        </div>

        {pickMode === "auto" ? (
          <>
            <p className="chart-caption">
              Runs the chosen strategy across a spread of resolved markets — different categories, years,
              and sizes (both big liquid markets and smaller ones, not just the biggest) — and shows
              cumulative P&amp;L over calendar time across all of them, live as results come in, like a
              stock-strategy backtest. Speed no longer depends much on market size or count: each market
              only fetches trading history for its biggest holders and anyone large enough to matter for
              insider detection (not every wallet that ever touched it), and several markets are processed
              at once. A strict signal like Strong Buy still only fires on a minority of checkpoints —
              that's the signal being selective, not a processing limit — so more markets means more real
              chances for it to fire, not just a longer wait.
            </p>
            <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
              <label>
                Categories:
                <select
                  multiple
                  value={autoCategories}
                  onChange={(e) => setAutoCategories(Array.from(e.target.selectedOptions, (o) => o.value))}
                  style={{ minWidth: 140, height: 90 }}
                >
                  {categories.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <span className="stat-footnote" style={{ maxWidth: 140 }}>none selected = all categories (⌘/ctrl-click for multiple)</span>
              <label>
                From year:
                <select value={autoYearMin} onChange={(e) => setAutoYearMin(e.target.value)}>
                  {RESOLVED_YEARS.slice().reverse().map((y) => <option key={y} value={y}>{y}</option>)}
                </select>
              </label>
              <label>
                To year:
                <select value={autoYearMax} onChange={(e) => setAutoYearMax(e.target.value)}>
                  {RESOLVED_YEARS.map((y) => <option key={y} value={y}>{y}</option>)}
                </select>
              </label>
              <label>
                How many markets:
                <input
                  type="number" min="1" max="500" value={autoCount}
                  onChange={(e) => setAutoCount(e.target.value)}
                  style={{ width: 64 }}
                />
              </label>
            </div>
          </>
        ) : (
          <>
            <div className="search-bar">
              <input
                placeholder="Search resolved markets by question (e.g. Fed, election, Lakers)… or leave blank to browse"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doSearch()}
              />
              <button onClick={() => doSearch()}>Search</button>
            </div>

            <div className="insiders-controls" style={{ flexWrap: "wrap", marginBottom: 16 }}>
              <label>
                Category:
                <select
                  value={category}
                  onChange={(e) => { setCategory(e.target.value); doSearch({ category: e.target.value }); }}
                >
                  <option value="">All categories</option>
                  {categories.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label>
                Resolved in:
                <select
                  value={year}
                  onChange={(e) => { setYear(e.target.value); doSearch({ year: e.target.value }); }}
                >
                  <option value="">Any year</option>
                  {RESOLVED_YEARS.map((y) => <option key={y} value={y}>{y}</option>)}
                </select>
              </label>
              <span className="insiders-meta">sorted by volume, highest first</span>
            </div>

            {candidatesLoading && <div className="empty-state">Loading…</div>}

            {!candidatesLoading && candidates && (
              <div className="insider-list">
                {candidates.length === 0 && <div className="empty-state">No resolved markets matched.</div>}
                {candidates.map((m) => {
                  const isSelected = selected.some((x) => x.condition_id === m.condition_id);
                  return (
                    <div
                      key={m.condition_id}
                      className="insider-market-row"
                      onClick={() => toggleSelect(m)}
                      style={isSelected ? { borderColor: "var(--accent)" } : undefined}
                    >
                      <div className="insider-market-row-main">
                        <div className="insider-market">{m.question}</div>
                        <div className="market-meta">
                          <span>${Math.round(m.volume).toLocaleString()} volume</span>
                          {m.resolved_yes != null && <span>resolved {m.resolved_yes ? "YES" : "NO"}</span>}
                          {m.end_date && <span>{new Date(m.end_date).toLocaleDateString()}</span>}
                        </div>
                      </div>
                      <input type="checkbox" checked={isSelected} readOnly />
                    </div>
                  );
                })}
              </div>
            )}

            {selected.length > 0 && (
              <p className="stat-footnote" style={{ marginTop: 12 }}>
                {selected.length} market{selected.length === 1 ? "" : "s"} selected.
              </p>
            )}
          </>
        )}
      </section>

      <section>
        <h3>2. Choose a strategy</h3>
        <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
          <label>
            <input type="radio" name="strategy" checked={strategy === "smart_money"} onChange={() => setStrategy("smart_money")} />
            {" "}Smart Money verdict
          </label>
          <label>
            <input type="radio" name="strategy" checked={strategy === "insider"} onChange={() => setStrategy("insider")} />
            {" "}Insider score
          </label>
        </div>

        <div className="insiders-controls" style={{ flexWrap: "wrap", marginTop: 10 }}>
          <label>
            Stake per trade ($):
            <input
              type="number" min="1" value={stakeUsd}
              onChange={(e) => setStakeUsd(e.target.value)}
              style={{ width: 90 }}
            />
          </label>
          {strategy === "smart_money" && (
            <label>
              Minimum signal:
              <select value={minSignalStrength} onChange={(e) => setMinSignalStrength(e.target.value)}>
                {SIGNAL_STRENGTH_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
          )}
          <label>
            Min conviction (0-100, blank = default):
            <input
              type="number" min="0" max="100" value={minConviction}
              onChange={(e) => setMinConviction(e.target.value)}
              style={{ width: 70 }}
            />
          </label>
          <label title="Skip a signal if the market already prices that side this close to $1 -- a win there pays pennies while a loss costs the full stake">
            Skip entries at/above (¢):
            <input
              type="number" min="50" max="100" step="0.1" value={Math.round(noEdgeThreshold * 1000) / 10}
              onChange={(e) => setNoEdgeThreshold(Number(e.target.value) / 100)}
              style={{ width: 70 }}
            />
          </label>
        </div>

        {strategyDefaults && (
          <div style={{ marginTop: 14 }}>
            <button className="back-link" onClick={() => setShowWeights((v) => !v)}>
              {showWeights ? "▾" : "▸"} Methodology &amp; weights for {strategy === "smart_money" ? "Smart Money verdict" : "Insider score"}
            </button>
            {showWeights && (
              <MethodologyPanel
                info={strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"]}
                weights={strategy === "smart_money" ? verdictWeights : insiderWeights}
                setWeights={strategy === "smart_money" ? setVerdictWeights : setInsiderWeights}
                onReset={() => {
                  const info = strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"];
                  const reset = pickEditable(info);
                  if (strategy === "smart_money") setVerdictWeights(reset);
                  else setInsiderWeights(reset);
                }}
              />
            )}
          </div>
        )}
      </section>

      <section>
        <h3>3. Run</h3>
        <button className="btn-primary" onClick={handleRun} disabled={!!liveRunId || sweepRunning}>
          {liveRunId ? "Running…" : "Run backtest"}
        </button>

        {watchingLiveElsewhere && (
          <div className="notice" style={{ marginTop: 12 }}>
            A backtest is still running in the background
            {liveRun ? ` (${liveRun.progress_done}/${liveRun.progress_total} markets done)` : ""} —
            you're viewing a different run below.{" "}
            <button className="back-link" style={{ display: "inline", fontSize: "inherit" }} onClick={() => viewRun(liveRunId)}>
              Switch to it
            </button>
          </div>
        )}

        {run && (
          <div style={{ marginTop: 18 }}>
            {(run.status === "pending" || run.status === "running") && (
              <div className="notice">
                Processing markets: {run.progress_done}/{run.progress_total} done (several run in parallel) —
                results below fill in live as each one finishes.
              </div>
            )}
            {run.status === "error" && <div className="error">Run failed: {run.error}</div>}

            {run.status === "done" && run.summary && (
              <div className="card-grid">
                <div className="stat-card">
                  <div className="stat-label">Trades taken</div>
                  <div className="stat-value">{run.summary.n_trades}</div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">Win rate</div>
                  <div className="stat-value">
                    {run.summary.win_rate != null ? `${(run.summary.win_rate * 100).toFixed(0)}%` : "—"}
                  </div>
                </div>
                <div className={"stat-card"}>
                  <div className="stat-label">Total P&amp;L</div>
                  <div className={"stat-value " + (run.summary.total_pnl_usd >= 0 ? "positive" : "negative")}>
                    {fmtUsd(run.summary.total_pnl_usd)}
                  </div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">ROI on staked $</div>
                  <div className={"stat-value " + (run.summary.roi_pct >= 0 ? "positive" : "negative")}>
                    {run.summary.roi_pct != null ? `${run.summary.roi_pct.toFixed(1)}%` : "—"}
                  </div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">Avg P&amp;L / trade</div>
                  <div className="stat-value">{fmtUsd(run.summary.avg_pnl_usd)}</div>
                </div>
              </div>
            )}

            {/* Equity curve + trades table render as soon as ANY trade exists, live while
                the run is still going (markets process concurrently -- see market_concurrency) --
                not gated behind the run finishing, since that can take a while across many markets. */}
            {equity.length > 1 && (
              <div ref={chartRef} style={{ marginTop: 20 }}>
                <h4 style={{ margin: "0 0 10px", fontSize: "0.9rem", fontWeight: 650 }}>
                  Cumulative P&amp;L over time{running ? " (live)" : ""}
                </h4>
                {chartWidth > 0 && (
                  <LineChart width={chartWidth} height={240} data={equity} margin={{ top: 10, right: 20, bottom: 10, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                    <XAxis
                      dataKey="ts" type="number" domain={["dataMin", "dataMax"]}
                      tickFormatter={fmtChartDate} tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor}
                    />
                    <YAxis tickFormatter={(v) => `$${Math.round(v)}`} tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor} width={70} />
                    <Tooltip {...tooltipStyle} formatter={(v) => [fmtUsd(v), "cumulative P&L"]} labelFormatter={fmtChartDate} />
                    <ReferenceLine y={0} stroke={axisColor} strokeDasharray="4 2" />
                    <Line dataKey="cumPnl" stroke={CHART_COLORS.accent} strokeWidth={2} dot={equity.length <= 30} isAnimationActive={false} />
                  </LineChart>
                )}
              </div>
            )}

            {run.status === "done" && run.summary?.by_signal_type && Object.keys(run.summary.by_signal_type).length > 1 && (
              <div style={{ marginTop: 20 }}>
                <h4 style={{ margin: "0 0 10px", fontSize: "0.9rem", fontWeight: 650 }}>
                  By signal type
                </h4>
                <p className="chart-caption">
                  If "Strong Buy + Lean" or "+ Fade" was allowed, this splits results back out by which
                  exact case fired -- the overall number above can hide a profitable strict case being
                  dragged down by a loose one lumped in with it, or vice versa.
                </p>
                <div className="table-scroll">
                  <table className="holder-table">
                    <thead>
                      <tr><th>Signal</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th><th>ROI</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(run.summary.by_signal_type).map(([sig, v]) => (
                        <tr key={sig}>
                          <td>{sig.replace(/_/g, " ")}</td>
                          <td>{v.n}</td>
                          <td>{(v.win_rate * 100).toFixed(0)}%</td>
                          <td className={v.pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(v.pnl_usd)}</td>
                          <td className={v.roi_pct >= 0 ? "positive" : "negative"}>{v.roi_pct != null ? `${v.roi_pct.toFixed(1)}%` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {run.status === "done" && run.summary?.by_category && Object.keys(run.summary.by_category).length > 1 && (
              <div style={{ marginTop: 20 }}>
                <h4 style={{ margin: "0 0 10px", fontSize: "0.9rem", fontWeight: 650 }}>By category</h4>
                <div className="table-scroll">
                  <table className="holder-table">
                    <thead>
                      <tr><th>Category</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(run.summary.by_category).map(([cat, v]) => (
                        <tr key={cat}>
                          <td>{cat}</td>
                          <td>{v.n}</td>
                          <td>{(v.win_rate * 100).toFixed(0)}%</td>
                          <td className={v.pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(v.pnl_usd)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div style={{ marginTop: 20 }}>
              <h4 style={{ margin: "0 0 10px", fontSize: "0.9rem", fontWeight: 650 }}>
                Trades{running ? ` (${trades.length} so far)` : ""}
              </h4>
              {trades.length === 0 ? (
                <div className="empty-state">
                  {running
                    ? "No trades yet — still processing."
                    : "No trades taken — no market's signal cleared the threshold at any checkpoint. Try a lower minimum signal/conviction, or different markets."}
                </div>
              ) : (
                <div className="table-scroll">
                  <table className="holder-table">
                    <thead>
                      <tr>
                        <th>Market</th><th>Side</th><th>Signal</th><th>Entry</th>
                        <th>When</th><th>Result</th><th>P&amp;L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.map((t) => (
                        <tr key={t.id}>
                          <td>{t.question || t.condition_id}</td>
                          <td>{t.side}</td>
                          <td>
                            {t.signal_type ? `${t.signal_type.replace(/_/g, " ")} · ` : ""}{t.signal_value.toFixed(0)}
                          </td>
                          <td>{(t.entry_price * 100).toFixed(1)}¢</td>
                          <td>{timeAgo(t.signal_ts)}</td>
                          <td>{t.won ? "Won" : "Lost"}</td>
                          <td className={t.pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(t.pnl_usd)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}
      </section>

      {strategyDefaults && (
        <section>
          <h3>4. Find optimal weights (sweep)</h3>
          <p className="chart-caption">
            Runs the SAME set of markets once per value below, varying only the one parameter chosen —
            the only way to actually tell whether a weight matters, rather than eyeballing single runs of
            a handful of trades each. Uses your current strategy, market selection, and other settings
            above; each point is a full backtest, so this can take a while for a large market set.
          </p>
          <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
            <label>
              Parameter:
              <select value={sweepParam} onChange={(e) => setSweepParam(e.target.value)}>
                <option value="">Choose…</option>
                {Object.keys(strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"].param_labels).map((k) => (
                  <option key={k} value={k}>{humanizeKey(k)}</option>
                ))}
              </select>
            </label>
            <label style={{ flex: 1, minWidth: 240 }}>
              Values to try (comma-separated):
              <input
                type="text" placeholder="e.g. 50, 55, 60, 65, 70, 75, 80"
                value={sweepValuesInput} onChange={(e) => setSweepValuesInput(e.target.value)}
                style={{ width: "100%" }}
              />
            </label>
            <button className="btn-primary" onClick={runSweep} disabled={sweepRunning || !!liveRunId}>
              {sweepRunning ? "Sweeping…" : "Run sweep"}
            </button>
            {sweepRunning && (
              <button className="back-link" onClick={() => { sweepStopRef.current = true; }}>Stop after current point</button>
            )}
          </div>

          {sweepProgress && (
            <div className="notice" style={{ marginTop: 12 }}>
              Testing {humanizeKey(sweepParam)} = {sweepProgress.value} (point {sweepProgress.i}/{sweepProgress.total})…
            </div>
          )}

          {sweepResults.length > 0 && (
            <>
              {sweepResults.length > 1 && (
                <div ref={sweepChartRef} style={{ marginTop: 16 }}>
                  {sweepChartWidth > 0 && (
                    <LineChart
                      width={sweepChartWidth} height={220}
                      data={sweepResults.filter((r) => r.summary).map((r) => ({ value: r.value, roi: r.summary.roi_pct ?? 0, n: r.summary.n_trades }))}
                      margin={{ top: 10, right: 20, bottom: 10, left: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                      <XAxis dataKey="value" type="number" tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor}
                        label={{ value: humanizeKey(sweepParam), position: "insideBottom", offset: -5, fontSize: 11, fill: axisColor }} />
                      <YAxis tickFormatter={(v) => `${v}%`} tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor} width={50} />
                      <Tooltip {...tooltipStyle} formatter={(v, name) => (name === "roi" ? [`${v.toFixed(1)}%`, "ROI"] : [v, "trades"])} />
                      <ReferenceLine y={0} stroke={axisColor} strokeDasharray="4 2" />
                      <Line dataKey="roi" stroke={CHART_COLORS.accent} strokeWidth={2} dot isAnimationActive={false} />
                    </LineChart>
                  )}
                </div>
              )}
              <div className="table-scroll" style={{ marginTop: 16 }}>
                <table className="holder-table">
                  <thead>
                    <tr><th>{humanizeKey(sweepParam)}</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th><th>ROI</th></tr>
                  </thead>
                  <tbody>
                    {sweepResults.map((r) => (
                      <tr key={r.value}>
                        <td>{r.value}</td>
                        {r.error || r.status === "error" ? (
                          <td colSpan={4} className="negative">error: {r.error || "run failed"}</td>
                        ) : (
                          <>
                            <td>{r.summary.n_trades}</td>
                            <td>{r.summary.win_rate != null ? `${(r.summary.win_rate * 100).toFixed(0)}%` : "—"}</td>
                            <td className={r.summary.total_pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(r.summary.total_pnl_usd)}</td>
                            <td className={r.summary.roi_pct >= 0 ? "positive" : "negative"}>{r.summary.roi_pct != null ? `${r.summary.roi_pct.toFixed(1)}%` : "—"}</td>
                          </>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>
      )}

      {strategyDefaults && (
        <section>
          <h3>5. Optimize (ML-style search, with held-out validation)</h3>
          <p className="chart-caption">
            Randomly samples combinations across ALL the ranges checked below (not one parameter at a
            time), running each on a TRAIN slice of the market selection above. The single best-found
            combination is then evaluated <strong>once</strong>, at the end, on a TEST slice held out from
            every tuning iteration — that test result, not the train result, is the honest answer to
            "did this actually find something real, or just fit this exact history". A gap where train
            looks great but test doesn't is overfitting, not a bug.
          </p>
          <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
            {Object.keys(strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"].param_labels).map((key) => {
              const info = strategyDefaults[strategy === "smart_money" ? "smart_money" : "insider"];
              const range = searchRanges[key];
              return (
                <label key={key} title={info.param_labels[key]} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <input
                    type="checkbox" checked={!!range?.enabled}
                    onChange={() => toggleSearchParam(key, info.params[key])}
                  />
                  {humanizeKey(key)}:
                  <input
                    type="number" step="any" placeholder="min"
                    value={range?.min ?? ""} disabled={!range?.enabled}
                    onChange={(e) => setSearchRanges((prev) => ({ ...prev, [key]: { ...prev[key], min: e.target.value } }))}
                    style={{ width: 64 }}
                  />
                  –
                  <input
                    type="number" step="any" placeholder="max"
                    value={range?.max ?? ""} disabled={!range?.enabled}
                    onChange={(e) => setSearchRanges((prev) => ({ ...prev, [key]: { ...prev[key], max: e.target.value } }))}
                    style={{ width: 64 }}
                  />
                </label>
              );
            })}
          </div>
          <div className="insiders-controls" style={{ flexWrap: "wrap", marginTop: 10 }}>
            <label>
              Iterations:
              <input
                type="number" min="1" max="100" value={searchNIterations}
                onChange={(e) => setSearchNIterations(e.target.value)}
                style={{ width: 60 }}
              />
            </label>
            <label title="Fraction of the market selection held out as the never-tuned-against TEST set">
              Test fraction:
              <input
                type="number" min="0.05" max="0.9" step="0.05" value={searchTestFraction}
                onChange={(e) => setSearchTestFraction(e.target.value)}
                style={{ width: 60 }}
              />
            </label>
            <label title="Optional -- fixes the random train/test split and parameter sampling so a re-run is reproducible">
              Seed (optional):
              <input
                type="number" value={searchSeed}
                onChange={(e) => setSearchSeed(e.target.value)}
                style={{ width: 70 }}
              />
            </label>
            <button className="btn-primary" onClick={handleRunSearch} disabled={searchRunning || !!liveRunId || sweepRunning}>
              {searchRunning ? "Searching…" : "Run search"}
            </button>
          </div>

          {searchResult && (searchResult.status === "pending" || searchResult.status === "running") && (
            <div className="notice" style={{ marginTop: 12 }}>
              {searchResult.progress_done}/{searchResult.progress_total} evaluations done
              {searchResult.progress_done >= (searchResult.progress_total - 1)
                ? " — final held-out test evaluation running now…"
                : " (train iterations, then one final held-out test evaluation)…"}
            </div>
          )}
          {searchResult?.status === "error" && <div className="error">Search failed: {searchResult.error}</div>}

          {searchResult?.status === "done" && (
            <div style={{ marginTop: 18 }}>
              <h4 style={{ margin: "0 0 8px", fontSize: "0.9rem", fontWeight: 650 }}>
                Best combination found (on TRAIN) vs. its held-out TEST result
              </h4>
              <div className="market-meta" style={{ marginBottom: 12 }}>
                {Object.entries(searchResult.best_overrides || {}).map(([k, v]) => (
                  <span key={k}>{humanizeKey(k)}: <strong>{Number(v).toFixed(3)}</strong></span>
                ))}
              </div>
              <div className="card-grid">
                <div className="stat-card">
                  <div className="stat-label">TRAIN ROI ({searchResult.best_train_summary?.n_trades ?? 0} trades)</div>
                  <div className={"stat-value " + (searchResult.best_train_summary?.roi_pct >= 0 ? "positive" : "negative")}>
                    {searchResult.best_train_summary?.roi_pct != null ? `${searchResult.best_train_summary.roi_pct.toFixed(1)}%` : "—"}
                  </div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">TEST ROI ({searchResult.test_summary?.n_trades ?? 0} trades) — the number that matters</div>
                  <div className={"stat-value " + (searchResult.test_summary?.roi_pct >= 0 ? "positive" : "negative")}>
                    {searchResult.test_summary?.roi_pct != null ? `${searchResult.test_summary.roi_pct.toFixed(1)}%` : "—"}
                  </div>
                </div>
              </div>
              {searchResult.test_summary && searchResult.best_train_summary
                && searchResult.test_summary.roi_pct != null && searchResult.best_train_summary.roi_pct != null
                && (searchResult.best_train_summary.roi_pct - searchResult.test_summary.roi_pct) > 15 && (
                <p className="chart-caption" style={{ color: "var(--danger)" }}>
                  TRAIN outperforms TEST by more than 15 points — a sign this combination may be fit to
                  quirks of the training markets rather than a generalizable edge. Treat the TEST number,
                  not the TRAIN one, as the honest estimate.
                </p>
              )}

              {searchResult.history?.length > 1 && (
                <div ref={searchChartRef} style={{ marginTop: 16 }}>
                  <h4 style={{ margin: "0 0 10px", fontSize: "0.9rem", fontWeight: 650 }}>TRAIN ROI by iteration</h4>
                  {searchChartWidth > 0 && (
                    <LineChart
                      width={searchChartWidth} height={220}
                      data={searchResult.history.map((h, i) => ({ i: i + 1, roi: h.summary?.roi_pct ?? 0 }))}
                      margin={{ top: 10, right: 20, bottom: 10, left: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                      <XAxis dataKey="i" type="number" tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor}
                        label={{ value: "iteration", position: "insideBottom", offset: -5, fontSize: 11, fill: axisColor }} />
                      <YAxis tickFormatter={(v) => `${v}%`} tick={{ fill: axisColor, fontSize: 11 }} stroke={gridColor} width={50} />
                      <Tooltip {...tooltipStyle} formatter={(v) => [`${v.toFixed(1)}%`, "TRAIN ROI"]} />
                      <ReferenceLine y={0} stroke={axisColor} strokeDasharray="4 2" />
                      {searchResult.test_summary?.roi_pct != null && (
                        <ReferenceLine y={searchResult.test_summary.roi_pct} stroke={CHART_COLORS.warning} strokeDasharray="4 2"
                          label={{ value: "TEST", position: "right", fontSize: 10, fill: axisColor }} />
                      )}
                      <Line dataKey="roi" stroke={CHART_COLORS.accent} strokeWidth={2} dot isAnimationActive={false} />
                    </LineChart>
                  )}
                </div>
              )}

              <div className="table-scroll" style={{ marginTop: 16 }}>
                <table className="holder-table">
                  <thead>
                    <tr>
                      <th>#</th>
                      {Object.keys(searchResult.best_overrides || {}).map((k) => <th key={k}>{humanizeKey(k)}</th>)}
                      <th>Trades</th><th>Win rate</th><th>ROI</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(searchResult.history || []).map((h, i) => (
                      <tr key={h.run_id} style={h.overrides && searchResult.best_overrides
                        && JSON.stringify(h.overrides) === JSON.stringify(searchResult.best_overrides)
                        ? { fontWeight: 650 } : undefined}>
                        <td>{i + 1}</td>
                        {Object.keys(searchResult.best_overrides || {}).map((k) => (
                          <td key={k}>{h.overrides?.[k] != null ? Number(h.overrides[k]).toFixed(3) : "—"}</td>
                        ))}
                        <td>{h.summary?.n_trades ?? "—"}</td>
                        <td>{h.summary?.win_rate != null ? `${(h.summary.win_rate * 100).toFixed(0)}%` : "—"}</td>
                        <td className={h.summary?.roi_pct >= 0 ? "positive" : "negative"}>
                          {h.summary?.roi_pct != null ? `${h.summary.roi_pct.toFixed(1)}%` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>
      )}

      {pastRuns && pastRuns.length > 0 && (
        <section>
          <h3>Past runs</h3>
          <div className="insider-list">
            {pastRuns.map((r) => (
              <div key={r.id} className="insider-market-row" onClick={() => viewRun(r.id)}
                   style={r.id === displayedRunId ? { borderColor: "var(--accent)" } : undefined}>
                <div className="insider-market-row-main">
                  <div className="insider-market">
                    {r.params.strategy === "smart_money" ? "Smart Money" : "Insider score"} · {r.params.condition_ids.length} market{r.params.condition_ids.length === 1 ? "" : "s"}
                  </div>
                  <div className="market-meta">
                    <span>{r.status}</span>
                    {r.summary && <span>{r.summary.n_trades} trades</span>}
                    {r.summary?.total_pnl_usd != null && (
                      <span className={r.summary.total_pnl_usd >= 0 ? "positive" : "negative"}>{fmtUsd(r.summary.total_pnl_usd)}</span>
                    )}
                    <span>{timeAgo(r.created_at)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
