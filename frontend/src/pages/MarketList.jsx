import { useEffect, useState } from "react";
import { fetchMarkets, searchMarkets } from "../api";
import VerdictBadge from "../components/VerdictBadge";
import { timeAgo } from "../format";

export default function MarketList({ onOpenMarket }) {
  const [markets, setMarkets] = useState([]);
  const [listRefreshedAt, setListRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchNotice, setSearchNotice] = useState(null);
  const [sortBy, setSortBy] = useState("activity");

  function loadDefault(sort = sortBy) {
    setLoading(true);
    setError(null);
    fetchMarkets({ sortBy: sort })
      .then((data) => { setMarkets(data.markets); setListRefreshedAt(data.list_refreshed_at); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadDefault();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSort(newSort) {
    setSortBy(newSort);
    if (!query.trim()) loadDefault(newSort);
  }

  async function handleSearch(e) {
    e.preventDefault();
    if (!query.trim()) {
      loadDefault();
      return;
    }
    setSearching(true);
    setError(null);
    setSearchNotice(null);
    try {
      const data = await searchMarkets(query.trim());
      setMarkets(data.results);
      if (data.live_search_unavailable) {
        setSearchNotice(
          "Live search is unavailable right now (couldn't reach Polymarket's API) — showing locally cached markets that match your query instead."
        );
      } else if (data.results.some((r) => r.approximate)) {
        setSearchNotice("Some results use approximate holder data (refining in the background).");
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setSearching(false);
    }
  }

  return (
    <div>
      <form className="search-bar" onSubmit={handleSearch}>
        <input
          type="text"
          placeholder="Search any live Polymarket market…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={searching}>{searching ? "Searching…" : "Search"}</button>
        {query && (
          <button type="button" className="secondary" onClick={() => { setQuery(""); setSearchNotice(null); loadDefault(); }}>
            Clear
          </button>
        )}
      </form>

      <div className="sort-bar">
        <span>Sort by:</span>
        {[["activity", "24h activity"], ["volume", "Total volume"], ["conviction", "Conviction"], ["gap", "Undervalued gap"]].map(([key, label]) => (
          <button
            key={key}
            className={sortBy === key ? "sort-active" : ""}
            onClick={() => handleSort(key)}
          >
            {label}
          </button>
        ))}
        <span className="list-freshness">list refreshed {timeAgo(listRefreshedAt)}</span>
      </div>

      {searchNotice && <div className="notice">{searchNotice}</div>}
      {error && <div className="error">Error: {error}</div>}
      {loading && <div className="empty-state">Loading markets…</div>}

      {!loading && markets.length === 0 && !error && (
        <div className="empty-state">No markets found. Try a different search.</div>
      )}

      <div className="market-table">
        {markets.map((m) => (
          <div key={m.condition_id} className="market-row" onClick={() => onOpenMarket(m.condition_id)}>
            <div className="market-row-main">
              <div className="market-question">{m.question}</div>
              <div className="market-meta">
                {m.category && <span className="pill">{m.category}</span>}
                <span>${Math.round(m.volume || 0).toLocaleString()} volume</span>
                {m.analyzed
                  ? <span>{m.rated_holder_count}/{m.holder_count} holders scored</span>
                  : <span className="approx-badge">not analyzed — open to score</span>}
                {m.stale && <span className="stale-badge">stale data</span>}
              </div>
            </div>
            {m.verdict
              ? <VerdictBadge label={m.verdict.label} conviction={m.verdict.conviction} />
              : <div className="verdict-badge" style={{ borderColor: "var(--neutral)", color: "var(--neutral)" }}>
                  <span className="verdict-label">—</span>
                </div>}
          </div>
        ))}
      </div>
    </div>
  );
}
