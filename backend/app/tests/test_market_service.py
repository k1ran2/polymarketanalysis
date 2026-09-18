"""
Integration test for market_service against an in-memory SQLite DB (synthetic
fixture data, clearly separate from the real-data dashboard.db used for the
live demo). Exercises the DB-join -> Holder -> scoring.py path end-to-end,
including a Rated (non-Unrated) holder, which the real-data seed set in
app/fixtures/ happens not to cover (none of our ~90 real seed wallets holds
one of our real seed markets, since coverage at this Phase-1 seed scale is
small) -- this test proves that code path independent of demo-data luck.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SCHEMA
from app.market_service import build_market_detail, build_market_summary, get_holders_for_market


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def test_market_detail_includes_rated_holder_score_and_tier():
    conn = make_conn()
    now = int(time.time())

    conn.execute(
        "INSERT INTO markets (condition_id, question, slug, category, outcomes, outcome_prices, "
        "clob_token_ids, volume, volume_24hr, liquidity, active, closed, updated_at) VALUES "
        "('c1','Will X happen?','will-x','test',?,?,?,50000,1000,5000,1,0,?)",
        (json.dumps(["Yes", "No"]), json.dumps(["0.4", "0.6"]), json.dumps(["tok0", "tok1"]), now),
    )
    # A Gold-tier trader with a high percentile score
    conn.execute(
        "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
        "VALUES ('0xgold', 80, 50000, 'Gold', 92.0, 2.1, ?)", (now,),
    )
    # A Bronze-tier trader with a low percentile score (dumb money)
    conn.execute(
        "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
        "VALUES ('0xbronze', 8, 600, 'Bronze', 15.0, -1.8, ?)", (now,),
    )
    # A second Gold-tier trader also long YES, so n_rated_holders clears the
    # insufficient-data gate (default requires >=3 rated holders, §3.8).
    conn.execute(
        "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
        "VALUES ('0xgold2', 60, 20000, 'Gold', 85.0, 1.7, ?)", (now,),
    )
    conn.executemany(
        "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("c1", "0xgold", "YES", 10000.0, "holders_fast_path", now),
            ("c1", "0xgold2", "YES", 8000.0, "holders_fast_path", now),
            ("c1", "0xbronze", "NO", 1000.0, "holders_fast_path", now),
            ("c1", "0xunseen", "NO", 500.0, "holders_fast_path", now),  # not in traders table -> Unrated
        ],
    )
    conn.commit()

    row = conn.execute("SELECT * FROM markets WHERE condition_id='c1'").fetchone()
    detail = build_market_detail(conn, row)

    holders_by_wallet = {h["wallet"]: h for h in detail["holders"]}
    assert holders_by_wallet["0xgold"]["tier"] == "Gold"
    assert holders_by_wallet["0xgold"]["score_percentile"] == 92.0
    assert holders_by_wallet["0xgold"]["signed_score"] == 42.0  # (92-50) on YES

    assert holders_by_wallet["0xbronze"]["tier"] == "Bronze"
    assert holders_by_wallet["0xbronze"]["score_percentile"] == 15.0
    assert holders_by_wallet["0xbronze"]["signed_score"] == 35.0  # -(15-50) on NO

    assert holders_by_wallet["0xunseen"]["tier"] == "Unrated"
    assert holders_by_wallet["0xunseen"]["score_percentile"] is None

    # Distribution should only include the 3 rated holders, net leaning YES-positive-signed
    assert detail["distribution"]["n"] == 3
    assert detail["distribution"]["weighted_mean"] > 0

    # Verdict: smart $ (18000 Gold on YES) dominates dumb $ (1000 Bronze on NO);
    # unrated share = 500/19500 ~ 2.6% -> well under the insufficient-data gate,
    # and n_rated_holders=3 clears the min-rated-holders gate.
    assert detail["verdict"]["unrated_volume_share"] < 0.10
    assert "Strong Buy YES" in detail["verdict"]["label"] or detail["verdict"]["label"] == "Lean YES"

    summary = build_market_summary(conn, row)
    assert summary["verdict"]["label"] == detail["verdict"]["label"]
    assert summary["holder_count"] == 4

    conn.close()


# ---------------------------------------------------------------------------
# get_holders_for_market -- must surface trend/recent_percentile (§3.3
# fade-pool inputs) from the traders table, not just tier/percentile. These
# were computed and stored by pipeline.py all along but never selected here
# before, which silently kept FadePoolConfig.exclude_improving_from_fade
# inert despite defaulting to True -- see scoring.holder_dumb_pool_eligible.
# ---------------------------------------------------------------------------

def test_get_holders_for_market_surfaces_trend_and_recent_percentile():
    conn = make_conn()
    now = int(time.time())
    conn.execute(
        "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, "
        "recent_percentile, trend, updated_at) VALUES "
        "('0xw', 20, 5000, 'Bronze', 20.0, -1.0, 55.0, 'Improving', ?)", (now,),
    )
    conn.execute(
        "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
        "VALUES ('c1', '0xw', 'NO', 1000.0, 'holders_fast_path', ?)", (now,),
    )
    conn.commit()

    holders = get_holders_for_market(conn, "c1")
    assert len(holders) == 1
    assert holders[0].trend == "Improving"
    assert holders[0].recent_percentile == 55.0
    conn.close()


def test_live_verdict_does_not_fade_an_improving_low_lifetime_scorer():
    """End-to-end through the real DB-join path (not a hand-built Holder
    list): a wallet with a low lifetime score but an Improving trend must no
    longer drag the market toward a Fade verdict, now that
    get_holders_for_market actually surfaces trend/recent_percentile."""
    conn = make_conn()
    now = int(time.time())
    conn.execute(
        "INSERT INTO markets (condition_id, question, slug, category, outcomes, outcome_prices, "
        "clob_token_ids, volume, volume_24hr, liquidity, active, closed, updated_at) VALUES "
        "('c2','Will Y happen?','will-y','test',?,?,?,50000,1000,5000,1,0,?)",
        (json.dumps(["Yes", "No"]), json.dumps(["0.5", "0.5"]), json.dumps(["tok0", "tok1"]), now),
    )
    for i in range(6):
        conn.execute(
            "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, trend, updated_at) "
            "VALUES (?, 20, 5000, 'Bronze', 20.0, -1.0, 'Improving', ?)", (f"0xno{i}", now),
        )
        conn.execute(
            "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
            "VALUES ('c2', ?, 'NO', 1000.0, 'holders_fast_path', ?)", (f"0xno{i}", now),
        )
    conn.execute(
        "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
        "VALUES ('0xyes1', 60, 20000, 'Silver', 55.0, 0.5, ?)", (now,),
    )
    conn.execute(
        "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
        "VALUES ('c2', '0xyes1', 'YES', 100.0, 'holders_fast_path', ?)", (now,),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM markets WHERE condition_id='c2'").fetchone()
    detail = build_market_detail(conn, row)
    assert "Fade NO" not in detail["verdict"]["label"]
    conn.close()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
