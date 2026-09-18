import { useEffect, useState } from "react";
import MarketList from "./pages/MarketList";
import MarketDetail from "./pages/MarketDetail";
import InsidersPage from "./pages/InsidersPage";
import BacktestPage from "./pages/BacktestPage";
import OverviewPage from "./pages/OverviewPage";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import Disclaimer from "./components/Disclaimer";
import "./App.css";

const TABS = ["overview", "markets", "insiders", "backtest"];

function stateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const t = params.get("tab");
  return {
    tab: TABS.includes(t) ? t : "overview",
    conditionId: params.get("market") || null,
  };
}

export default function App() {
  const [{ tab, conditionId }, setUrlState] = useState(stateFromUrl);

  // The old version only ever pushed history entries, never listened for the
  // browser's own back/forward navigating them -- so those buttons silently
  // did nothing. Reading location on every popstate (not just on load) is
  // the actual fix.
  useEffect(() => {
    function onPopState() {
      setUrlState(stateFromUrl());
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function openMarket(id) {
    setUrlState((prev) => ({ ...prev, conditionId: id }));
    const url = new URL(window.location);
    url.searchParams.set("market", id);
    window.history.pushState({}, "", url);
  }

  function goBack() {
    setUrlState((prev) => ({ ...prev, conditionId: null }));
    const url = new URL(window.location);
    url.searchParams.delete("market");
    window.history.pushState({}, "", url);
  }

  function switchTab(next) {
    setUrlState({ tab: next, conditionId: null });
    const url = new URL(window.location);
    url.searchParams.delete("market");
    if (next === "overview") url.searchParams.delete("tab");
    else url.searchParams.set("tab", next);
    window.history.pushState({}, "", url);
  }

  return (
    <div className="shell">
      <Sidebar tab={tab} onSwitch={switchTab} />
      <div className="shell-main">
        <TopBar />
        <div className="shell-content">
          {conditionId ? (
            <MarketDetail key={conditionId} conditionId={conditionId} onBack={goBack} />
          ) : tab === "insiders" ? (
            <InsidersPage onOpenMarket={openMarket} />
          ) : tab === "backtest" ? (
            <BacktestPage />
          ) : tab === "markets" ? (
            <MarketList onOpenMarket={openMarket} />
          ) : (
            <OverviewPage onOpenMarket={openMarket} onSwitchTab={switchTab} />
          )}
          <Disclaimer />
        </div>
      </div>
    </div>
  );
}
