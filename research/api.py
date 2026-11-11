"""Read-only clients for Polymarket's public APIs (Gamma + Data API v2).

Data API v1 was retired in 2026 (HTTP 410). v2 differs in ways that matter here:
cursor pagination with no depth cap, `condition` instead of `market`, both sides
of a fill via `taker_only=false`, and every payload wrapped in `data`.
"""
from __future__ import annotations

import threading
import time
from typing import Iterator, Optional

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"


class ApiError(Exception):
    pass


class Client:
    """One shared httpx client with a simple global pace and patient retries."""

    def __init__(self, min_interval: float = 0.02, max_retries: int = 6, timeout: float = 45.0):
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "polymarket-research/0.1"})
        self._lock = threading.Lock()
        self._next = 0.0
        self.min_interval = min_interval
        self.max_retries = max_retries

    def _pace(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, params: Optional[dict] = None):
        last = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                r = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                last = repr(e)
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:120]}"
                retry_after = r.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else min(2 ** attempt, 20))
                continue
            raise ApiError(f"GET {url} {params} -> {r.status_code}: {r.text[:300]}")
        raise ApiError(f"GET {url} {params} failed after retries: {last}")

    # ---- Gamma ----------------------------------------------------------
    def closed_markets(self, end_date_min: str, volume_min: float, end_date_max: Optional[str] = None) -> Iterator[dict]:
        """Every closed market in the window, biggest first (keyset walk, no depth cap)."""
        params = dict(closed="true", limit=100, end_date_min=end_date_min, volume_num_min=volume_min,
                      include_tag="true", order="volumeNum", ascending="false")
        if end_date_max:
            params["end_date_max"] = end_date_max
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["after_cursor"] = cursor
            page = self.get(GAMMA + "/markets/keyset", p)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("next_cursor")
            if not cursor or not page.get("markets"):
                return

    def open_markets(self, volume_min: float = 0.0) -> Iterator[dict]:
        params = dict(closed="false", active="true", limit=100, volume_num_min=volume_min,
                      include_tag="true", order="volumeNum", ascending="false")
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["after_cursor"] = cursor
            page = self.get(GAMMA + "/markets/keyset", p)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("next_cursor")
            if not cursor or not page.get("markets"):
                return

    # ---- Data API v2 ----------------------------------------------------
    def price_history(self, token_id: str, bucket_seconds: Optional[int] = None) -> list:
        """Whole-life series for one token, oldest first. Last point of a resolved
        market is its settlement payout (resolution_seconds == 0)."""
        params = dict(token_id=token_id, interval="max")
        if bucket_seconds:
            params["bucket_seconds"] = bucket_seconds
        out, cursor = [], None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            page = self.get(DATA + "/prices-history", p)
            out.extend(page.get("data") or [])
            cursor = (page.get("pagination") or {}).get("next_cursor")
            if not cursor:
                return out

    def resolutions(self, condition_ids: list) -> list:
        out = []
        for i in range(0, len(condition_ids), 20):
            page = self.get(DATA + "/resolutions", dict(condition=",".join(condition_ids[i:i + 20])))
            out.extend(page.get("data") or [])
        return out

    def trades(self, condition_id: str, min_cash: float = 0.0, both_sides: bool = True,
               max_pages: int = 10_000) -> tuple:
        """(rows newest-first, truncated?) -- every fill of one market above `min_cash` USD."""
        params = dict(condition=condition_id, limit=1000, taker_only="false" if both_sides else "true")
        if min_cash > 0:
            params.update(filter_type="CASH", filter_amount=min_cash)
        out, cursor, pages = [], None, 0
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            page = self.get(DATA + "/trades", p)
            out.extend(page.get("data") or [])
            cursor = (page.get("pagination") or {}).get("next_cursor")
            pages += 1
            if not cursor:
                return out, False
            if pages >= max_pages:
                return out, True

    def user_pnl(self, wallet: str) -> list:
        page = self.get(DATA + "/user-pnl", dict(user=wallet, interval="max", fidelity="1d"))
        return ((page.get("data") or {}).get("points")) or []

    def holders(self, condition_id: str, limit: int = 100) -> list:
        page = self.get(DATA + "/holders", dict(condition=condition_id, limit=limit))
        return page.get("data") or []
