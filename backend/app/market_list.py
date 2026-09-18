"""
Market-list metadata refresh: one Gamma call, no holders.

The list page needs a browsable set of active markets to pick from. This pulls
the top markets by 24h volume and upserts their metadata; holder analysis only
happens when a market is opened (app.market_analysis).

The Gamma events payload is large (~10MB), so the refresh never runs on the
request path: `maybe_refresh_async` serves cached rows immediately and kicks a
background refresh when the cache is stale.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import config as cfg
from . import db
from .backfill import LiveDataSource, _extract_markets_from_events, upsert_market

_last_refresh = 0.0
_refreshing = threading.Lock()


def last_refresh_ts() -> Optional[int]:
    """Unix timestamp of the last completed market-list refresh, or None if it hasn't run yet."""
    return int(_last_refresh) if _last_refresh else None


def maybe_refresh_async(force: bool = False):
    """Start a background market-list refresh if the cache is stale. Non-blocking."""
    now = time.time()
    if not force and (now - _last_refresh) < cfg.ON_DEMAND.market_list_fresh_seconds:
        return
    if not _refreshing.acquire(blocking=False):
        return  # a refresh is already running

    def _run():
        try:
            with db.db_session() as conn:
                refresh_market_list(conn, force=True)
        except Exception as e:  # noqa: BLE001
            print(f"[market_list] refresh failed: {e}")
        finally:
            _refreshing.release()

    threading.Thread(target=_run, name="market-list-refresh", daemon=True).start()


def refresh_market_list(conn, force: bool = False, source=None) -> int:
    """Upsert the top active markets' metadata. Returns how many were written."""
    global _last_refresh
    now = time.time()
    if not force and (now - _last_refresh) < cfg.ON_DEMAND.market_list_fresh_seconds:
        return 0

    source = source or LiveDataSource()
    events = source.get_top_events(max(cfg.ON_DEMAND.market_list_size * 2, 40))
    all_markets = _extract_markets_from_events(events)

    # dedupe by condition_id, then top-N by *24h* volume (live activity, not
    # year-old cumulative volume -- otherwise the list is all longshot
    # "will <celebrity> win 2028" markets that are just one whale's NO position),
    # capped per event so it isn't 6 legs of one match.
    by_cid: dict[str, dict] = {}
    for m in all_markets:
        prev = by_cid.get(m["condition_id"])
        if prev is None or (m["volume_24hr"] or 0) > (prev["volume_24hr"] or 0):
            by_cid[m["condition_id"]] = m

    per_event_cap = 3  # at most 3 markets from any one event, for variety
    picked: list[dict] = []
    ev_counts: dict = {}
    for m in sorted(by_cid.values(), key=lambda x: (x["volume_24hr"] or 0, x["volume"] or 0), reverse=True):
        ev = m.get("event_id")
        if ev_counts.get(ev, 0) >= per_event_cap:
            continue
        picked.append(m)
        ev_counts[ev] = ev_counts.get(ev, 0) + 1
        if len(picked) >= cfg.ON_DEMAND.market_list_size:
            break

    now_ts = int(now)
    conn.execute("UPDATE markets SET list_rank = NULL WHERE list_rank IS NOT NULL")
    for rank, m in enumerate(picked):
        upsert_market(conn, m, now_ts)
        conn.execute("UPDATE markets SET list_rank = ? WHERE condition_id = ?", (rank, m["condition_id"]))
    conn.commit()
    _last_refresh = now
    return len(picked)
