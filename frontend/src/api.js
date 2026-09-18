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

// The /api/trading/* local-access token -- see backend/app/local_auth.py's
// module docstring for why this exists (CORS alone doesn't stop a request
// from another webpage open in the same browser from reaching this server).
// Stored per-browser in localStorage; the user pastes it in once from the
// backend's startup console output.
const LOCAL_TOKEN_KEY = "polymarket_dashboard_local_token";

export function getLocalToken() {
  try {
    return localStorage.getItem(LOCAL_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setLocalToken(token) {
  try {
    localStorage.setItem(LOCAL_TOKEN_KEY, token);
  } catch {
    // ignore -- private browsing / storage blocked; the token just won't persist
  }
}

async function tradingRequest(path, options = {}) {
  const headers = { ...(options.headers || {}), "X-Local-Token": getLocalToken() };
  return request(path, { ...options, headers });
}

function tradingGet(path) {
  return tradingRequest(path);
}

function tradingPost(path, body) {
  return tradingRequest(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

function tradingPut(path, body) {
  return tradingRequest(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
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

// --- Live trading (mock mode -- see backend/app/trade_watcher.py) ---------

export function fetchTradingStatus() {
  return tradingGet(`/api/trading/status`);
}

export function fetchTradingSettings() {
  return tradingGet(`/api/trading/settings`);
}

export function saveTradingSettings(body) {
  return tradingPut(`/api/trading/settings`, body);
}

export function pauseTrading() {
  return tradingPost(`/api/trading/pause`);
}

export function resumeTrading() {
  return tradingPost(`/api/trading/resume`);
}

export function scanTradingNow() {
  return tradingPost(`/api/trading/scan-now`);
}

export function fetchTradingSignals(limit = 50) {
  return tradingGet(`/api/trading/signals?limit=${limit}`);
}

export function fetchTrades({ status, source, limit = 100 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (source) params.set("source", source);
  params.set("limit", String(limit));
  return tradingGet(`/api/trading/trades?${params.toString()}`);
}

export function logManualTrade(body) {
  return tradingPost(`/api/trading/trades`, body);
}

export function fetchTradingPerformance() {
  return tradingGet(`/api/trading/performance`);
}

export function fetchCredentialsStatus() {
  return tradingGet(`/api/trading/credentials`);
}

export function connectCredentials(body) {
  return tradingRequest(`/api/trading/credentials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function disconnectCredentials() {
  return tradingRequest(`/api/trading/credentials`, { method: "DELETE" });
}
