const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

async function request(path, options) {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

function getJSON(path) {
  return request(path);
}

function postJSON(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchMarkets({ category, minVolume, sortBy = "conviction", limit = 50 } = {}) {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (minVolume) params.set("min_volume", String(minVolume));
  if (sortBy) params.set("sort_by", sortBy);
  if (limit) params.set("limit", String(limit));
  return getJSON(`/api/markets?${params.toString()}`);
}

export function fetchMarketDetail(conditionId) {
  return getJSON(`/api/markets/${conditionId}`);
}

export function searchMarkets(query) {
  return getJSON(`/api/search?q=${encodeURIComponent(query)}`);
}

export function fetchDisclaimer() {
  return getJSON(`/api/disclaimer`);
}

export function fetchInsiderMarkets({ minScore, limit = 100, includeNearResolved = false } = {}) {
  const params = new URLSearchParams();
  if (minScore) params.set("min_score", String(minScore));
  if (limit) params.set("limit", String(limit));
  if (includeNearResolved) params.set("include_near_resolved", "true");
  return getJSON(`/api/insiders?${params.toString()}`);
}

export function fetchInsiderMarketDetail(conditionId, { minScore } = {}) {
  const params = new URLSearchParams();
  if (minScore) params.set("min_score", String(minScore));
  return getJSON(`/api/insiders/${conditionId}?${params.toString()}`);
}

export function fetchInsiderStatus() {
  return getJSON(`/api/insiders/status`);
}

export function scanInsidersNow() {
  return request(`/api/insiders/scan-now`, { method: "POST" });
}

export function browseResolvedMarkets({
  limit = 30, offset = 0, order = "volumeNum", search, category, endDateMin, endDateMax,
} = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  params.set("order", order);
  if (search) params.set("search", search);
  if (category) params.set("category", category);
  if (endDateMin) params.set("end_date_min", endDateMin);
  if (endDateMax) params.set("end_date_max", endDateMax);
  return getJSON(`/api/backtest/markets?${params.toString()}`);
}

export function fetchBacktestCategories() {
  return getJSON(`/api/backtest/categories`);
}

export function fetchStrategyDefaults() {
  return getJSON(`/api/backtest/strategy-defaults`);
}

export function createBacktestRun(body) {
  return postJSON(`/api/backtest/runs`, body);
}

export function fetchBacktestRun(runId) {
  return getJSON(`/api/backtest/runs/${runId}`);
}

export function fetchBacktestRuns(limit = 20) {
  return getJSON(`/api/backtest/runs?limit=${limit}`);
}

export function createBacktestSearch(body) {
  return postJSON(`/api/backtest/search`, body);
}

export function fetchBacktestSearch(searchId) {
  return getJSON(`/api/backtest/search/${searchId}`);
}

export function fetchBacktestSearches(limit = 20) {
  return getJSON(`/api/backtest/searches?limit=${limit}`);
}
