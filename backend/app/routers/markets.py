from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .. import config as cfg
from .. import db
from ..market_service import build_market_summary, build_market_detail
from ..market_analysis import (
    ensure_market_and_snapshot, pending_wallets, start_background_scoring,
    is_scoring, analyze_market, scan_insiders_for_market,
)
from ..market_list import maybe_refresh_async, last_refresh_ts
from ..backfill import LiveDataSource
from ..polymarket_client import PolymarketAPIError

router = APIRouter(prefix="/api/markets", tags=["markets"])


@router.get("")
def list_markets(
    category: Optional[str] = Query(None),
    min_volume: float = Query(0.0),
    sort_by: str = Query("activity", pattern="^(conviction|activity|volume|gap)$"),
    limit: int = Query(cfg.ON_DEMAND.market_list_size, le=200),
):
    """
    Market list page (§1, §9): top active markets with the §3.8 verdict where
    one has been computed. Metadata is refreshed from Gamma on a short cadence
    (one call, no holders); verdicts come from whatever holder analysis already
    ran for each market -- opening a market is what triggers its analysis.
    """
    maybe_refresh_async()  # non-blocking; serves cached rows now, refreshes in the background

    with db.db_session() as conn:
        # Only the curated list (list_rank set by market_list.refresh_market_list);
        # markets that got into the table via search are reachable by search/link,
        # not shown here.
        rows = conn.execute(
            "SELECT * FROM markets WHERE list_rank IS NOT NULL AND volume >= ? ORDER BY list_rank",
            (min_volume,),
        ).fetchall()
        if not rows:  # cold start before the first refresh completes
            rows = conn.execute(
                "SELECT * FROM markets WHERE volume >= ? ORDER BY volume DESC LIMIT ?",
                (min_volume, cfg.ON_DEMAND.market_list_size),
            ).fetchall()
        if category:
            rows = [r for r in rows if (r["category"] or "").lower() == category.lower()]

        summaries = [build_market_summary(conn, r) for r in rows]

        if sort_by == "gap":
            summaries.sort(key=lambda s: abs((s["verdict"] or {}).get("undervalued_gap") or 0), reverse=True)
        elif sort_by == "conviction":
            summaries.sort(key=lambda s: (s["verdict"] or {}).get("conviction") or 0, reverse=True)
        elif sort_by == "volume":
            summaries.sort(key=lambda s: s["volume"] or 0, reverse=True)
        else:  # activity = 24h volume
            summaries.sort(key=lambda s: s["volume_24hr"] or 0, reverse=True)

        return {"markets": summaries[:limit], "total": len(summaries), "list_refreshed_at": last_refresh_ts()}


@router.get("/{condition_id}")
def get_market_detail(
    condition_id: str,
    wait: bool = Query(False, description="block until scoring finishes instead of returning early"),
    refresh: bool = Query(False, description="force a fresh holder snapshot + re-score"),
):
    """
    Market detail (§9): holder table + §3.5/§3.6/§3.7/§3.8.

    Default (fast) path: refresh the holder snapshot (cheap), return immediately
    with whatever Smart Scores are already cached, and score the rest in the
    background -- `analyzing: true` + `pending: N` until it's done. Poll this
    endpoint until `analyzing` is false. `?wait=1` runs it all inline instead.
    """
    now_ts = int(time.time())

    if wait:
        try:
            return analyze_market(condition_id, force=refresh)
        except KeyError:
            raise HTTPException(404, "Market not found -- try /api/search?q=... first")
        except PolymarketAPIError as e:
            raise HTTPException(502, f"Polymarket API unavailable: {e}")

    source = LiveDataSource()
    with db.db_session() as conn:
        try:
            market_row = ensure_market_and_snapshot(conn, source, condition_id, now_ts, force_snapshot=refresh)
        except PolymarketAPIError:
            market_row = conn.execute("SELECT * FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
        if market_row is None:
            raise HTTPException(404, "Market not found -- try /api/search?q=... first")

        pend = pending_wallets(conn, condition_id, now_ts)

    if pend:
        start_background_scoring(condition_id)
    # Cheap (no network calls) -- runs on every open, not just when wallets
    # need (re)scoring, so an already-fully-scored market still gets checked
    # for insider patterns against its current holder snapshot. Must happen
    # BEFORE build_market_detail, which reads insider_flags -- otherwise the
    # response reflects last time's (possibly empty) scan, not this one's.
    scan_insiders_for_market(condition_id, now_ts)

    with db.db_session() as conn:
        market_row = conn.execute("SELECT * FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
        detail = build_market_detail(conn, market_row)
    detail["analyzing"] = bool(pend) or is_scoring(condition_id)
    detail["pending"] = len(pend)
    detail["analyzed_at"] = now_ts
    return detail
