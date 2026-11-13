import { useEffect, useState } from "react";
import Alerts from "./pages/Alerts";
import DeepDives from "./pages/DeepDives";
import Lab from "./pages/Lab";
import Overview from "./pages/Overview";
import Scan from "./pages/Scan";
import Traders from "./pages/Traders";
import "./App.css";

const PAGES = [
  { id: "overview", label: "Overview", component: Overview },
  { id: "lab", label: "Strategy lab", component: Lab },
  { id: "scan", label: "Pattern scan", component: Scan },
  { id: "traders", label: "Traders", component: Traders },
  { id: "dives", label: "Deep dives", component: DeepDives },
  { id: "alerts", label: "Alerts", component: Alerts },
];

function pageFromUrl() {
  const id = new URLSearchParams(window.location.search).get("page");
  return PAGES.some((p) => p.id === id) ? id : "overview";
}

export default function App() {
  const [page, setPage] = useState(pageFromUrl);

  useEffect(() => {
    const onPop = () => setPage(pageFromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function go(id) {
    const url = new URL(window.location);
    if (id === "overview") url.searchParams.delete("page");
    else url.searchParams.set("page", id);
    window.history.pushState({}, "", url);
    setPage(id);
  }

  const Current = PAGES.find((p) => p.id === page).component;

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          Polymarket Research
          <span>Find and test strategies</span>
        </div>
        {PAGES.map((p) => (
          <button key={p.id} className={`nav-item ${p.id === page ? "active" : ""}`} onClick={() => go(p.id)}>
            {p.label}
          </button>
        ))}
        <div className="sidebar-foot">Research only. Not financial advice.</div>
      </nav>
      <main className="main">
        <Current />
      </main>
    </div>
  );
}
