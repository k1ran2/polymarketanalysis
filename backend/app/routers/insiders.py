from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from .. import config as cfg
from .. import db
from ..insiders import current_price_yes_from_json, is_near_resolved, list_flags, list_markets_with_flags
from ..poller import start_manual_scan, status as poller_status

router = APIRouter(prefix="/api/insiders", tags=["insiders"])


@router.get("")
def get_insider_markets(
    min_score: float = Query(0.0, ge=0, le=100),
    limit: int = Query(100, le=500),
    include_near_resolved: bool = Query(
        False, description="Include markets already priced within 1 cent of $0/$1 -- "
                            "no edge left to trade there, hidden by default.",
    ),
):
    """
    One row per market with any flagged position (app.insiders, a first-pass
    heuristic -- see its module docstring), most-recently-detected first.
    Click through to GET /api/insiders/{condition_id} for the wallet-level
    detail on one market.
    """
    with db.db_session() as conn:
        markets = list_markets_with_flags(
            conn, min_score=min_score, limit=limit, include_near_resolved=include_near_resolved,
        )
    return {"markets": markets, "alert_threshold": cfg.INSIDER.alert_threshold, **poller_status()}


@router.get("/status")
def get_status():
    """Poller state: enabled?, interval, whether a scan is running right now, and when the last one finished."""
    return poller_status()


@router.post("/scan-now")
def scan_now():
    """
    Kick off one poll cycle in the background instead of waiting for the next
    scheduled one -- re-analyzing every tracked market can take a while, so
    this returns immediately; poll GET /status until scanning is false.
    """
    started = start_manual_scan()
    return {"started": started, **poller_status()}


@router.get("/{condition_id}")
def get_insider_market_detail(
    condition_id: str,
    min_score: float = Query(0.0, ge=0, le=100),
):
    """Every flagged wallet for one market, most-suspicious first, plus the market's current stats."""
    with db.db_session() as conn:
        row = conn.execute(
            "SELECT question, slug, category, outcomes, outcome_prices, volume, volume_24hr, "
            "liquidity, end_date, updated_at FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Market not found")
        flags = list_flags(conn, limit=500, min_score=min_score, condition_id=condition_id)

    outcomes = json.loads(row["outcomes"]) if row["outcomes"] else None
    current_price_yes = current_price_yes_from_json(row["outcome_prices"])

    return {
        "condition_id": condition_id, "question": row["question"],
        "slug": row["slug"], "category": row["category"], "flags": flags,
        "outcomes": outcomes, "current_price_yes": current_price_yes,
        "volume": row["volume"], "volume_24hr": row["volume_24hr"], "liquidity": row["liquidity"],
        "end_date": row["end_date"],
        "near_resolved": is_near_resolved(current_price_yes),
    }
