"""
Thin, rate-limited HTTP clients for the three Polymarket read-only APIs
used by this dashboard (§2). All auth-free.

Rate limiting: a simple token-bucket per base URL, refilled continuously.
On 429, back off with exponential delay + jitter and retry a bounded number
of times (§10: "Rate-limit each API client (token bucket) and back off on
429s — Data API is the tight one at ~1,000/10s.").

These clients raise PolymarketAPIError on unrecoverable failures; callers
(backfill script, on-demand search) are expected to catch that and fall back
to a cached snapshot with a staleness badge (§10) rather than crash.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config as cfg


class PolymarketAPIError(Exception):
    pass


class TokenBucket:
    """
    Thread-safe: the backfill script fetches multiple wallets concurrently
    (app.config.BACKFILL.fetch_concurrency) sharing one client/bucket per API,
    so acquire() needs a lock -- without it, concurrent threads could each read
    a stale `self.tokens` and collectively over-spend the budget.
    """

    def __init__(self, max_requests: int, per_seconds: float):
        self.capacity = max_requests
        self.tokens = float(max_requests)
        self.refill_rate = max_requests / per_seconds  # tokens per second
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.last_refill = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_rate
            time.sleep(max(wait, 0.01))


class RateLimitedClient:
    """Wraps httpx with a token bucket + 429 backoff for one Polymarket API base."""

    def __init__(self, base_url: str, max_requests: int, per_seconds: float, max_retries: int = 3,
                 timeout: float = 10.0, backoff_cap: float = 4.0):
        self.base_url = base_url
        self.bucket = TokenBucket(max_requests, per_seconds)
        self.max_retries = max_retries
        self.backoff_cap = backoff_cap
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def get(self, path: str, params: Optional[dict] = None) -> dict | list:
        attempt = 0
        while True:
            self.bucket.acquire()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise PolymarketAPIError(f"GET {self.base_url}{path} failed: {e}") from e
                time.sleep(self._backoff(attempt))
                continue

            if resp.status_code == 429:
                attempt += 1
                if attempt > self.max_retries:
                    raise PolymarketAPIError(f"GET {self.base_url}{path} rate-limited after {attempt} retries")
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                time.sleep(delay)
                continue

            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise PolymarketAPIError(f"GET {self.base_url}{path} server error {resp.status_code}")
                time.sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise PolymarketAPIError(
                    f"GET {self.base_url}{path} -> {resp.status_code}: {resp.text[:300]}"
                )

            try:
                return resp.json()
            except json.JSONDecodeError as e:
                raise PolymarketAPIError(f"GET {self.base_url}{path} returned non-JSON: {e}") from e

    def post(self, path: str, json_body: Optional[dict] = None) -> dict | list:
        attempt = 0
        while True:
            self.bucket.acquire()
            try:
                resp = self._client.post(path, json=json_body)
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise PolymarketAPIError(f"POST {self.base_url}{path} failed: {e}") from e
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code == 429:
                attempt += 1
                if attempt > self.max_retries:
                    raise PolymarketAPIError(f"POST {self.base_url}{path} rate-limited")
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                raise PolymarketAPIError(f"POST {self.base_url}{path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    def _backoff(self, attempt: int) -> float:
        # Default cap (4s) is kept low so a genuinely dead/blocked host fails
        # within a few seconds -- §6 promises the on-demand search path
        # returns "in seconds", so we shouldn't burn a minute retrying a
        # connection that's never going to succeed. The backfill script
        # constructs its DataAPIClient with a much higher backoff_cap (see
        # BackfillConfig.data_api_backoff_cap) since it's a background job
        # that should absorb transient 429s via patient retrying rather than
        # give up on a wallet quickly.
        base = min(2 ** attempt, self.backoff_cap)
        return base + random.uniform(0, 0.5)

    def close(self):
        self._client.close()


class GammaClient(RateLimitedClient):
    def __init__(self):
        # The /events payload with include_liquidity is ~10MB; give it room.
        super().__init__(cfg.GAMMA_BASE, *cfg.RATE_LIMITS.gamma, timeout=30.0)

    def get_active_events(self, limit: int = 500, order: str = "volume24hr", offset: int = 0) -> list:
        return self.get("/events", {
            "active": "true", "closed": "false", "limit": limit,
            "order": order, "ascending": "false", "offset": offset,
            # Gamma omits per-market liquidity unless asked; §3.5 ranks the
            # undervalued gap by market liquidity, so we need it populated.
            "include_liquidity": "true",
        })

    def public_search(self, query: str, events_status: str = "active", limit_per_type: int = 10) -> dict:
        return self.get("/public-search", {
            "q": query, "events_status": events_status, "limit_per_type": limit_per_type,
            "include_liquidity": "true",
        })

    def get_event(self, event_id: str) -> dict:
        return self.get(f"/events/{event_id}")

    def get_resolved_markets(self, limit: int = 50, offset: int = 0, order: str = "volumeNum",
                              ascending: bool = False, tag_id: Optional[int] = None,
                              end_date_min: Optional[str] = None, end_date_max: Optional[str] = None) -> list:
        """
        Resolved markets to choose from for backtesting (app.backtest_data).
        `closed=true` + `order` by volume/date lets the UI page through a
        real spread of market types/sizes rather than only the newest;
        `tag_id`/`end_date_min`/`end_date_max` narrow by category/date range
        (all confirmed live to genuinely filter server-side -- see
        routers.backtest.CATEGORY_TAG_IDS for how a category name maps to a
        tag_id, via GET https://gamma-api.polymarket.com/tags/slug/<slug>).

        NOTE: Gamma's `volume`/`endDate`/`startDate` fields sort as STRINGS
        (confirmed live: "volume" desc returned $999 before $400M+ markets) --
        `volumeNum` is the numeric field that actually sorts by size.
        """
        params = {"closed": "true", "limit": limit, "offset": offset, "order": order,
                  "ascending": "true" if ascending else "false"}
        if tag_id is not None:
            params["tag_id"] = tag_id
        if end_date_min:
            params["end_date_min"] = end_date_min
        if end_date_max:
            params["end_date_max"] = end_date_max
        return self.get("/markets", params)

    def get_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        # NOTE confirmed live: `condition_ids` alone is silently ignored by
        # Gamma (returns an unfiltered page of markets, or nothing, depending
        # on what else is on the request) -- pairing it with `closed=true`
        # is what actually applies the filter. Since app.backtest_data only
        # ever wants a market it can already tell (from the browse listing)
        # is resolved, this is a safe pairing rather than a scope narrowing.
        results = self.get("/markets", {"condition_ids": condition_id, "closed": "true"})
        return results[0] if results else None


class DataAPIClient(RateLimitedClient):
    def __init__(self, max_retries: int = 3, backoff_cap: float = 4.0):
        # Defaults match the fast-fail behavior on-demand search needs (§6).
        # backfill.py's LiveDataSource passes BackfillConfig.data_api_max_retries
        # / data_api_backoff_cap instead, since a background script can afford
        # to retry a rate-limited wallet patiently rather than give up on it.
        super().__init__(cfg.DATA_API_BASE, *cfg.RATE_LIMITS.data_api,
                          max_retries=max_retries, backoff_cap=backoff_cap)

    def get_closed_positions(self, user: str, limit: int = 50, offset: int = 0, sort_by: str = "REALIZEDPNL") -> list:
        return self.get("/closed-positions", {
            "user": user, "limit": limit, "offset": offset, "sortBy": sort_by,
        })

    def get_open_positions(self, user: str) -> list:
        return self.get("/positions", {"user": user})

    def get_holders(self, market_condition_id: str, limit: int = 100) -> list:
        return self.get("/holders", {"market": market_condition_id, "limit": limit})

    def get_leaderboard(self, category: str = "OVERALL", time_period: str = "ALL",
                         order_by: str = "PNL", limit: int = 50, offset: int = 0) -> list:
        return self.get("/v1/leaderboard", {
            "category": category, "timePeriod": time_period, "orderBy": order_by,
            "limit": limit, "offset": offset,
        })

    def get_trades(self, condition_id: str, limit: int = 500, offset: int = 0) -> list:
        """
        Full public trade tape for a market (app.backtest_data) -- NOT the
        CLOB's /data/trades, which 401s without a private API key. This is
        the only way to reconstruct who held what at a past point in time,
        since Polymarket exposes no historical positions/holders snapshot.
        """
        return self.get("/trades", {"market": condition_id, "limit": limit, "offset": offset})


class ClobClient(RateLimitedClient):
    def __init__(self):
        super().__init__(cfg.CLOB_BASE, *cfg.RATE_LIMITS.clob)

    def get_trades(self, market_condition_id: str, next_cursor: Optional[str] = None) -> dict:
        params = {"market": market_condition_id}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return self.get("/data/trades", params)

    def get_prices(self, body: dict) -> dict:
        return self.post("/prices", body)

    def get_midpoints(self, body: dict) -> dict:
        return self.post("/midpoints", body)

    def get_price_history(self, token_id: str, start_ts: int, end_ts: int, fidelity_minutes: int = 60) -> list:
        """
        Historical price series for one outcome token (app.backtest_data).
        This is the last TRADED price at each sample -- Polymarket doesn't
        publicly expose historical order-book depth, so there's no true
        historical bid/ask to pull. See BacktestConfig's docstring.

        NOTE confirmed live: `interval=max` silently returns an EMPTY history
        for an older/already-resolved market (seemingly only serves
        recent/live markets) -- explicit `startTs`/`endTs` is what actually
        returns the real series for a market from the past.
        """
        data = self.get("/prices-history", {
            "market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity_minutes,
        })
        return data.get("history", []) if isinstance(data, dict) else []
