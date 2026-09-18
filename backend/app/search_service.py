"""
§6: search any live Polymarket market on demand.

GET /api/search?q=...:
  1. Match locally cached markets instantly (question / slug LIKE).
  2. Also hit Gamma /public-search live so search isn't limited to a
     pre-seeded list, and upsert the metadata of anything it turns up.
  3. Return market summaries. Holder analysis is NOT done here -- it happens
     when the user opens a market (app.market_analysis), which keeps search
     fast. Opening a searched market works exactly like opening a listed one.
  4. If the live call fails, fall back to local matches with a flag (§10).
"""
from __future__ import annotations

import time

from . import config as cfg
from .backfill import _extract_markets_from_events, upsert_market
from .polymarket_client import GammaClient, PolymarketAPIError
from .market_service import build_market_summary


def _bump_search_cache(conn, condition_id: str, volume: float, now_ts: int):
    row = conn.execute("SELECT * FROM search_cache WHERE condition_id=?", (condition_id,)).fetchone()
    count = (row["search_count"] + 1) if row else 1
    if row:
        conn.execute(
            "UPDATE search_cache SET search_count=?, last_searched_at=? WHERE condition_id=?",
            (count, now_ts, condition_id),
        )
    else:
        conn.execute(
            "INSERT INTO search_cache (condition_id, search_count, last_searched_at, reconstruction_queued) "
            "VALUES (?,?,?,0)",
            (condition_id, count, now_ts),
        )
    # Flag markets that earn full trade reconstruction later (Phase 2 picks this up).
    if count >= cfg.ON_DEMAND.repeat_search_threshold or (volume or 0) >= cfg.ON_DEMAND.full_reconstruction_min_volume:
        conn.execute("UPDATE search_cache SET reconstruction_queued=1 WHERE condition_id=?", (condition_id,))


def search_markets(conn, query: str, limit_per_type: int = 10) -> dict:
    now_ts = int(time.time())
    query = query.strip()
    if not query:
        return {"query": query, "results": [], "live_search_unavailable": False}

    condition_ids: set[str] = set()
    for r in conn.execute(
        "SELECT condition_id FROM markets WHERE question LIKE ? OR slug LIKE ? ORDER BY volume DESC LIMIT 25",
        (f"%{query}%", f"%{query}%"),
    ):
        condition_ids.add(r["condition_id"])

    live_unavailable = False
    try:
        resp = GammaClient().public_search(query, events_status="active", limit_per_type=limit_per_type)
        events = resp.get("events", []) if isinstance(resp, dict) else []
        for m in _extract_markets_from_events(events):
            upsert_market(conn, m, now_ts)
            _bump_search_cache(conn, m["condition_id"], m["volume"], now_ts)
            condition_ids.add(m["condition_id"])
    except PolymarketAPIError:
        live_unavailable = True
    except Exception:
        live_unavailable = True  # DNS / network -- degrade, don't 500 (§10)

    results = []
    if condition_ids:
        rows = conn.execute(
            "SELECT * FROM markets WHERE condition_id IN ({}) ORDER BY volume DESC".format(
                ",".join("?" * len(condition_ids))
            ),
            tuple(condition_ids),
        ).fetchall()
        results = [build_market_summary(conn, r) for r in rows]

    return {"query": query, "results": results, "live_search_unavailable": live_unavailable}
