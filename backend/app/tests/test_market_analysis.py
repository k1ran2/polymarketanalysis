"""
Integration test for the on-demand analyze flow (app.market_analysis) against
the offline fixtures, on a throwaway DB.

Fixtures cover one market's holders (the Fed 50+ market) and a few wallets'
closed-position history -- enough to exercise: Gamma metadata -> live holder
snapshot -> score the holders we can -> assemble the §3.5/§3.8 payload.
"""
import json
import time
from pathlib import Path

import pytest

from app import db
from app.backfill import FixtureDataSource, _extract_markets_from_events, upsert_market
from app import market_analysis
from app import market_list

FIX = Path(__file__).resolve().parents[1] / "fixtures"
FED_50_CID = "0x5e464d85eb49f22d876f3ed6168a7db5e2288e9ae1eb91effd2758e994676f86"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    # market_list caches its last-refresh time module-globally; reset it.
    monkeypatch.setattr(market_list, "_last_refresh", 0.0)
    # Force the analyzer to use fixtures, not live APIs.
    monkeypatch.setattr(market_analysis, "_make_source", lambda offline: FixtureDataSource())
    yield


def _seed_market_metadata():
    src = FixtureDataSource()
    with db.db_session() as conn:
        for m in _extract_markets_from_events(src.get_top_events(10)):
            upsert_market(conn, m, int(time.time()))


def test_analyze_market_offline_end_to_end(tmp_db):
    _seed_market_metadata()

    detail = market_analysis.analyze_market(FED_50_CID, offline=True)

    assert detail["condition_id"] == FED_50_CID
    assert detail["analyzing"] is False
    assert detail["holder_count"] > 0
    assert 0 <= detail["rated_holder_count"] <= detail["holder_count"]

    # Every holder the fast path returned got a traders row (scored or Unrated).
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0] >= detail["holder_count"]

    # Payload shape the frontend depends on.
    for key in ("aggregation", "verdict", "whale_concentration", "distribution", "holders"):
        assert key in detail
    assert detail["verdict"]["label"]
    assert detail["whale_concentration"]["yes"]["buckets"]
    assert isinstance(detail["holders"], list) and detail["holders"]

    # Holder table is sorted by position size desc.
    usds = [h["position_usd"] for h in detail["holders"]]
    assert usds == sorted(usds, reverse=True)


def test_leaderboard_seed_scores_wallets_offline(tmp_db):
    from app.backfill import run_backfill
    run_backfill(offline=True)
    with db.db_session() as conn:
        rated = conn.execute("SELECT COUNT(*) FROM traders WHERE percentile IS NOT NULL").fetchone()[0]
    assert rated >= 1  # fixtures carry a few scorable wallets


def test_reopen_is_served_from_cache(tmp_db):
    _seed_market_metadata()
    market_analysis.analyze_market(FED_50_CID, offline=True)

    now = int(time.time())
    src = FixtureDataSource()
    with db.db_session() as conn:
        # snapshot is fresh -> ensure_market_and_snapshot must not refetch
        row = market_analysis.ensure_market_and_snapshot(conn, src, FED_50_CID, now)
        assert row is not None
        assert market_analysis.pending_wallets(conn, FED_50_CID, now) == []
