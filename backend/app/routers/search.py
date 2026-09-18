from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db
from ..search_service import search_markets

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def search(q: str = Query(..., min_length=1)):
    """§6: search any live Polymarket market on demand (not limited to a pre-tracked list)."""
    with db.db_session() as conn:
        return search_markets(conn, q)
