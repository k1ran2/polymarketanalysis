import { useState } from "react";
import MarketList from "./pages/MarketList";
import MarketDetail from "./pages/MarketDetail";
import InsidersPage from "./pages/InsidersPage";
import BacktestPage from "./pages/BacktestPage";
import Disclaimer from "./components/Disclaimer";
import "./App.css";

const TABS = ["markets", "insiders", "backtest"];

function tabFromUrl() {
  const t = new URLSearchParams(window.location.search).get("tab");
  return TABS.includes(t) ? t : "markets";
}

export default function App() {
  const [conditionId, setConditionId] = useState(
    () => new URLSearchParams(window.location.search).get("market") || null
  );
  const [tab, setTab] = useState(tabFromUrl);

  function openMarket(id) {
    setConditionId(id);
    const url = new URL(window.location);
    url.searchParams.set("market", id);
    window.history.pushState({}, "", url);
  }

  function goBack() {
    setConditionId(null);
    const url = new URL(window.location);
    url.searchParams.delete("market");
    window.history.pushState({}, "", url);
  }

  function switchTab(next) {
    setConditionId(null);
    setTab(next);
    const url = new URL(window.location);
    url.searchParams.delete("market");
    if (next === "markets") url.searchParams.delete("tab");
    else url.searchParams.set("tab", next);
    window.history.pushState({}, "", url);
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>Polymarket Smart Money Dashboard</h1>
        <p className="subtitle">Read-only research tool — not a trading bot.</p>
        <nav className="app-nav">
          <button className={tab === "markets" ? "nav-tab active" : "nav-tab"} onClick={() => switchTab("markets")}>
            Markets
          </button>
          <button className={tab === "insiders" ? "nav-tab active" : "nav-tab"} onClick={() => switchTab("insiders")}>
            Insiders
          </button>
          <button className={tab === "backtest" ? "nav-tab active" : "nav-tab"} onClick={() => switchTab("backtest")}>
            Backtest
          </button>
        </nav>
      </header>
      <main>
        {conditionId ? (
          <MarketDetail key={conditionId} conditionId={conditionId} onBack={goBack} />
        ) : tab === "insiders" ? (
          <InsidersPage onOpenMarket={openMarket} />
        ) : tab === "backtest" ? (
          <BacktestPage />
        ) : (
          <MarketList onOpenMarket={openMarket} />
        )}
      </main>
      <Disclaimer />
    </div>
  );
}
