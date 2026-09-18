import { useEffect, useState } from "react";
import {
  fetchTradingSettings, fetchTradingSignals, fetchTradingStatus, fetchTradingPerformance, fetchTrades,
  getLocalToken, pauseTrading, resumeTrading, saveTradingSettings, scanTradingNow, setLocalToken,
} from "../api";
import { timeAgo } from "../format";

function fmtUsd(n) {
  return n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`;
}

const SETTINGS_FIELDS = [
  { key: "max_stake_usd", label: "Max stake per trade ($)", step: "0.1" },
  { key: "max_open_positions", label: "Max open positions", step: "1" },
  { key: "max_open_exposure_usd", label: "Max total exposure ($)", step: "0.1" },
  { key: "max_daily_loss_usd", label: "Auto-halt after losing ($/day)", step: "0.1" },
  { key: "max_daily_spend_usd", label: "Max spend per day ($)", step: "0.1" },
  { key: "max_trades_per_day", label: "Max trades per day", step: "1" },
  { key: "min_conviction", label: "Min conviction (0-100)", step: "1" },
  { key: "max_entry_price", label: "Skip entries at/above (0-1)", step: "0.01" },
  { key: "min_market_liquidity_usd", label: "Min market liquidity ($)", step: "100" },
  { key: "min_hours_to_resolution", label: "Min hours until resolution", step: "1" },
  { key: "resignal_cooldown_hours", label: "Hours before re-signaling same market", step: "1" },
];

export default function TradingPage() {
  const [token, setToken] = useState(getLocalToken());
  const [tokenInput, setTokenInput] = useState("");
  const [status, setStatus] = useState(null);
  const [settings, setSettings] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [performance, setPerformance] = useState(null);
  const [trades, setTrades] = useState(null);
  const [signals, setSignals] = useState(null);
  const [error, setError] = useState(null);
  const [scanning, setScanning] = useState(false);

  function loadAll() {
    fetchTradingStatus().then(setStatus).catch((e) => setError(e.message));
    fetchTradingSettings().then(setSettings).catch(() => {});
    fetchTradingPerformance().then(setPerformance).catch(() => {});
    fetchTrades({ limit: 20 }).then((d) => setTrades(d.trades)).catch(() => {});
    fetchTradingSignals(20).then((d) => setSignals(d.signals)).catch(() => {});
  }

  useEffect(() => {
    if (token) loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  function saveToken() {
    setLocalToken(tokenInput.trim());
    setToken(tokenInput.trim());
  }

  async function handleSaveSettings(next) {
    setError(null);
    try {
      const saved = await saveTradingSettings(next);
      setSettings(saved);
      setStatus((s) => ({ ...s, enabled: saved.enabled }));
    } catch (e) {
      setError(e.message);
    }
  }

  async function toggleEnabled() {
    setError(null);
    try {
      if (status?.enabled) await pauseTrading();
      else await resumeTrading();
      loadAll();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleScanNow() {
    setError(null);
    setScanning(true);
    try {
      await scanTradingNow();
      setTimeout(() => { loadAll(); setScanning(false); }, 6000);
    } catch (e) {
      setError(e.message);
      setScanning(false);
    }
  }

  if (!token) {
    return (
      <div>
        <div className="insiders-header">
          <div>
            <h2>Trading</h2>
            <p className="subtitle">Connect this page to the backend's local trading API.</p>
          </div>
        </div>
        <div className="notice">
          Paste the local access token printed in the backend's console on startup (also in
          <code> backend/data/.local_token</code>). This just proves requests are coming from this app,
          not another webpage — it's separate from any exchange credentials.
        </div>
        <div className="insiders-controls" style={{ marginTop: 16 }}>
          <input
            type="password" placeholder="Local access token" value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            style={{ minWidth: 320 }}
          />
          <button className="btn-primary" onClick={saveToken} disabled={!tokenInput.trim()}>Connect</button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="insiders-header">
        <div>
          <h2>Trading</h2>
          <p className="subtitle">Auto-execute the Smart Money / Insider signals on live markets, with hard caps.</p>
        </div>
        <button className={status?.enabled ? "kill-switch" : "btn-primary"} onClick={toggleEnabled}>
          {status?.enabled ? "Pause trading" : "Enable trading"}
        </button>
      </div>

      <div className="overview-disclosure" style={{ marginBottom: 20 }}>
        <strong>Mock mode.</strong> No exchange credentials are connected — every "trade" below is
        simulated (a fabricated fill, nothing sent to Polymarket). This lets you watch the bot's
        real decisions against real live markets before ever connecting real money.
      </div>

      {error && <div className="error">Error: {error}</div>}

      {status && (
        <div className="card-grid">
          <div className="stat-card">
            <div className="stat-label">Status</div>
            <div className="stat-value">
              {status.halted_today ? "Halted" : status.enabled ? "Live" : "Paused"}
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Today's P&amp;L</div>
            <div className={"stat-value " + (status.today.realized_pnl_usd >= 0 ? "positive" : "negative")}>
              {fmtUsd(status.today.realized_pnl_usd)}
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Today's spend</div>
            <div className="stat-value">{fmtUsd(status.today.spent_usd)}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Trades today</div>
            <div className="stat-value">{status.today.n_trades}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Open positions</div>
            <div className="stat-value">{status.open_positions} <span className="stat-footnote">({fmtUsd(status.open_exposure_usd)})</span></div>
          </div>
        </div>
      )}

      {performance && performance.n_trades > 0 && (
        <div className="card-grid">
          <div className="stat-card">
            <div className="stat-label">All-time trades</div>
            <div className="stat-value">{performance.n_trades}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Win rate</div>
            <div className="stat-value">{(performance.win_rate * 100).toFixed(0)}%</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Total P&amp;L</div>
            <div className={"stat-value " + (performance.total_pnl_usd >= 0 ? "positive" : "negative")}>
              {fmtUsd(performance.total_pnl_usd)}
            </div>
          </div>
        </div>
      )}

      <section>
        <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
          <button className="btn-primary" onClick={handleScanNow} disabled={scanning}>
            {scanning ? "Scanning…" : "Scan now"}
          </button>
          <button className="back-link" onClick={() => setShowSettings((v) => !v)}>
            {showSettings ? "▾" : "▸"} Settings
          </button>
        </div>

        {showSettings && settings && (
          <div className="insiders-market-stats" style={{ marginTop: 12 }}>
            <div className="insiders-controls" style={{ flexWrap: "wrap" }}>
              <label>
                Strategy:
                <select value={settings.strategy} onChange={(e) => handleSaveSettings({ ...settings, strategy: e.target.value })}>
                  <option value="smart_money">Smart Money verdict</option>
                  <option value="insider">Insider score</option>
                </select>
              </label>
              <label>
                Min signal:
                <select value={settings.min_signal_strength} onChange={(e) => handleSaveSettings({ ...settings, min_signal_strength: e.target.value })}>
                  <option value="strong_buy">Strong Buy only</option>
                  <option value="lean">Strong Buy + Lean</option>
                  <option value="fade">Strong Buy + Lean + Fade</option>
                </select>
              </label>
            </div>
            <div className="insiders-controls" style={{ flexWrap: "wrap", marginTop: 10 }}>
              {SETTINGS_FIELDS.map((f) => (
                <label key={f.key}>
                  {f.label}:
                  <input
                    type="number" step={f.step} value={settings[f.key]}
                    onChange={(e) => setSettings({ ...settings, [f.key]: Number(e.target.value) })}
                    style={{ width: 90 }}
                  />
                </label>
              ))}
            </div>
            <button className="btn-primary" style={{ marginTop: 12 }} onClick={() => handleSaveSettings(settings)}>
              Save settings
            </button>
          </div>
        )}
      </section>

      <section>
        <h3>Trades</h3>
        {!trades || trades.length === 0 ? (
          <div className="empty-state">No trades yet.</div>
        ) : (
          <div className="table-scroll">
            <table className="holder-table">
              <thead>
                <tr><th>Market</th><th>Side</th><th>Source</th><th>Entry</th><th>Status</th><th>When</th><th>P&amp;L</th></tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id}>
                    <td>{t.question || t.condition_id}</td>
                    <td>{t.side}</td>
                    <td>{t.source}</td>
                    <td>{(t.avg_fill_price * 100).toFixed(1)}¢</td>
                    <td>{t.status}</td>
                    <td>{timeAgo(t.created_at)}</td>
                    <td className={t.realized_pnl_usd == null ? "" : t.realized_pnl_usd >= 0 ? "positive" : "negative"}>
                      {t.realized_pnl_usd == null ? "open" : fmtUsd(t.realized_pnl_usd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h3>Signals (traded &amp; skipped)</h3>
        {!signals || signals.length === 0 ? (
          <div className="empty-state">No signals evaluated yet — run "Scan now".</div>
        ) : (
          <div className="table-scroll">
            <table className="holder-table">
              <thead>
                <tr><th>Market</th><th>Side</th><th>Signal</th><th>Conviction</th><th>Action</th><th>When</th></tr>
              </thead>
              <tbody>
                {signals.map((s) => (
                  <tr key={s.id}>
                    <td>{s.condition_id}</td>
                    <td>{s.side}</td>
                    <td>{s.signal_type || "—"}</td>
                    <td>{s.conviction?.toFixed(0)}</td>
                    <td className={s.action === "traded" ? "positive" : ""}>
                      {s.action === "traded" ? "Traded" : `Skipped — ${s.skip_reason}`}
                    </td>
                    <td>{timeAgo(s.detected_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
