"""
Unit tests for the insider-pattern heuristic (app.insiders). First-pass,
unvalidated scoring -- these tests pin down the *shape* of the behavior
(new/thin/large scores high, established/normal-sized scores low) rather than
exact magic numbers, since the weights in config.INSIDER are explicitly
tunable.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SCHEMA, MIGRATIONS
from app.scoring import Holder
from app.insiders import compute_insider_score, scan_market, summarize_market, list_flags, list_markets_with_flags
from app import config as cfg

NOW = 1_700_000_000


def test_below_min_position_is_not_flagged():
    h = Holder("w", "YES", 10.0, 80.0, "Gold", n_trades=200, first_active_ts=NOW - 1000 * 86400)
    assert compute_insider_score(h, NOW) is None


def test_new_wallet_thin_history_large_position_scores_high():
    h = Holder(
        "w", "NO", 50_000.0, None, "Unrated",
        n_trades=0, first_active_ts=None, avg_bet_usd=None,
    )
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert flag.insider_score > 70
    assert any("no observed" in r or "resolved trade" in r for r in flag.reasons)


def test_established_normal_sized_wallet_scores_low():
    h = Holder(
        "w", "YES", 6_000.0, 55.0, "Gold",
        n_trades=300, first_active_ts=NOW - 900 * 86400, avg_bet_usd=1_800.0,
    )
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert flag.insider_score < 30
    assert flag.size_multiple < 5


def test_unknown_tenure_with_substantial_history_is_not_treated_as_new():
    # A wallet with 300 resolved trades but a missing first_active_ts (e.g. an
    # older traders row from before tenure tracking existed) must not read as
    # "no observed trading history" -- it has plenty of observed history.
    h = Holder(
        "w", "NO", 100_000.0, 45.0, "Gold",
        n_trades=300, first_active_ts=None, avg_bet_usd=900.0,
    )
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert not any("no observed" in r for r in flag.reasons)
    assert flag.insider_score < 60  # thin/age factors shouldn't dominate here


def test_sudden_large_bet_relative_to_own_history_is_flagged():
    # established, not new, plenty of history -- but this one bet is 20x their norm
    h = Holder(
        "w", "YES", 20_000.0, 60.0, "Gold",
        n_trades=100, first_active_ts=NOW - 500 * 86400, avg_bet_usd=1_000.0,
    )
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert flag.size_multiple == 20
    assert any("typical bet size" in r for r in flag.reasons)
    assert flag.insider_score > 30  # size alone should move the needle even though age/thin don't


def test_not_yet_fetched_wallet_is_not_flagged():
    # Mid-background-scoring (or a wallet Polymarket's API errored on) looks
    # identical to a genuinely-new thin-history wallet -- must not be flagged
    # until we've actually confirmed that read (fetch_status == "ok").
    for status in ("never_fetched", "error"):
        h = Holder(
            "w", "NO", 50_000.0, None, "Unrated",
            n_trades=0, first_active_ts=None, fetch_status=status,
        )
        assert compute_insider_score(h, NOW) is None


def test_reasons_always_non_empty():
    h = Holder("w", "YES", 5_000.0, 50.0, "Silver", n_trades=50, first_active_ts=NOW - 200 * 86400, avg_bet_usd=100.0)
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert len(flag.reasons) >= 1


# ---------------------------------------------------------------------------
# scan_market: the multi-outcome market-maker exclusion
# ---------------------------------------------------------------------------

def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # already applied
    return conn


def _insert_market(conn, cid, event_id, outcome_prices="[]"):
    conn.execute(
        "INSERT INTO markets (condition_id, event_id, question, outcomes, outcome_prices, "
        "clob_token_ids, updated_at) VALUES (?,?,?,?,?,?,?)",
        (cid, event_id, f"Will {cid} win?", "[]", outcome_prices, "[]", NOW),
    )


def _insert_holder(conn, cid, wallet, side, usd):
    conn.execute(
        "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
        "VALUES (?,?,?,?,'holders_fast_path',?)",
        (cid, wallet, side, usd, NOW),
    )


def test_wallet_shorting_every_outcome_of_one_event_is_excluded():
    conn = _make_conn()
    mm_wallet = "0xmarketmaker"
    for i in range(5):
        cid = f"c{i}"
        _insert_market(conn, cid, event_id="election-2028")
        _insert_holder(conn, cid, mm_wallet, "NO", 50_000_000.0)
    conn.commit()

    holders_of_c0 = [Holder(mm_wallet, "NO", 50_000_000.0, None, "Unrated", n_trades=0)]
    alertable = scan_market(conn, "c0", holders_of_c0, NOW)

    assert alertable == []
    row = conn.execute(
        "SELECT * FROM insider_flags WHERE condition_id='c0' AND wallet=?", (mm_wallet,)
    ).fetchone()
    assert row is None
    conn.close()


def test_isolated_large_position_is_not_excluded_as_market_maker():
    conn = _make_conn()
    wallet = "0xisolated"
    _insert_market(conn, "c0", event_id="single-game")
    _insert_holder(conn, "c0", wallet, "NO", 50_000.0)
    conn.commit()

    holders = [Holder(wallet, "NO", 50_000.0, None, "Unrated", n_trades=0)]
    alertable = scan_market(conn, "c0", holders, NOW)

    row = conn.execute(
        "SELECT * FROM insider_flags WHERE condition_id='c0' AND wallet=?", (wallet,)
    ).fetchone()
    assert row is not None
    conn.close()


# ---------------------------------------------------------------------------
# summarize_market: the market-level roll-up
# ---------------------------------------------------------------------------

def _insert_flag(conn, cid, wallet, side, usd, score, now=NOW):
    import json
    conn.execute(
        """
        INSERT INTO insider_flags (condition_id, wallet, side, position_usd, insider_score,
            reasons, tenure_days, n_resolved, size_multiple, detected_at, alerted_at)
        VALUES (?,?,?,?,?,?,NULL,0,NULL,?,NULL)
        """,
        (cid, wallet, side, usd, score, json.dumps(["test"]), now),
    )


def test_summarize_market_no_flags_is_no_activity():
    conn = _make_conn()
    _insert_market(conn, "c0", event_id="e")
    conn.commit()
    s = summarize_market(conn, "c0")
    assert s.label == "No significant insider activity"
    assert s.conviction == 0.0
    conn.close()


def test_summarize_market_one_sided_leans_that_side():
    conn = _make_conn()
    _insert_market(conn, "c0", event_id="e")
    _insert_flag(conn, "c0", "0xa", "YES", 100_000.0, 90.0)
    _insert_flag(conn, "c0", "0xb", "YES", 50_000.0, 80.0)
    conn.commit()
    s = summarize_market(conn, "c0")
    assert s.label == "Insiders lean YES"
    assert s.net_insider_flow > 0
    assert s.yes_flag_count == 2 and s.no_flag_count == 0
    assert s.conviction > 0
    conn.close()


def test_summarize_market_balanced_both_sides_reads_as_conflicting():
    conn = _make_conn()
    _insert_market(conn, "c0", event_id="e")
    _insert_flag(conn, "c0", "0xa", "YES", 100_000.0, 90.0)
    _insert_flag(conn, "c0", "0xb", "NO", 100_000.0, 90.0)
    conn.commit()
    s = summarize_market(conn, "c0")
    assert s.label == "Insider activity on both sides"
    assert abs(s.net_insider_flow) < 1e-6
    assert s.conviction > 0  # still a real signal, just not directional
    conn.close()


def test_summarize_market_conviction_scales_with_severity_and_size():
    conn = _make_conn()
    _insert_market(conn, "c0", event_id="e")
    _insert_flag(conn, "c0", "0xa", "YES", 10_000.0, 30.0)  # small, low severity
    conn.commit()
    low = summarize_market(conn, "c0")

    conn2 = _make_conn()
    _insert_market(conn2, "c0", event_id="e")
    _insert_flag(conn2, "c0", "0xa", "YES", 300_000.0, 95.0)  # large, high severity
    conn2.commit()
    high = summarize_market(conn2, "c0")

    assert high.conviction > low.conviction
    conn.close()
    conn2.close()


# ---------------------------------------------------------------------------
# Validation against publicly reported real Polymarket insider cases
# (Sept 2026 research -- see config.InsiderConfig's docstring for sources).
# Exact wallet-level numbers (position size, precise wallet age) generally
# aren't published, so these are APPROXIMATE reconstructions from what was
# reported, not ground truth -- the point is "does the detector clear
# alert_threshold for the *shape* of a documented case", not exact-number
# fidelity. There is no equivalent set of confirmed non-insiders to check
# false positives against, which is the real limit on how "validated" this
# heuristic can honestly be called.
# ---------------------------------------------------------------------------

def _cp(seconds_old, position_usd, n_trades=0, avg_bet_usd=None, side="YES"):
    return Holder(
        "0xcase", side, position_usd, None, "Unrated",
        n_trades=n_trades, first_active_ts=NOW - seconds_old, avg_bet_usd=avg_bet_usd,
    )


def test_case_iran_peace_deal_wallet_created_2h_before_trade():
    # Reported: account created ~2 hours before its first trade, bought a
    # large "Yes" position when the market implied only 6%.
    h = _cp(seconds_old=2 * 3600, position_usd=50_000.0)
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert flag.insider_score >= cfg.INSIDER.alert_threshold


def test_case_maduro_wallet_days_old_with_cluster():
    # Reported: 3 wallets, created/funded "days" earlier, activated hours
    # before the news -- approximated as ~3 days old, and (since they acted
    # together) with a cluster_factor reflecting the other 2.
    h = _cp(seconds_old=3 * 86400, position_usd=34_000.0)
    flag = compute_insider_score(h, NOW, cluster_factor=1.0)
    assert flag is not None
    assert flag.insider_score >= cfg.INSIDER.alert_threshold


def test_case_openai_browser_bet_brand_new_wallet():
    # Reported: brand-new wallet, $40,000 bet.
    h = _cp(seconds_old=6 * 3600, position_usd=40_000.0)
    flag = compute_insider_score(h, NOW)
    assert flag is not None
    assert flag.insider_score >= cfg.INSIDER.alert_threshold


def test_case_without_cluster_context_scores_lower_than_with():
    # Same wallet/position; the only difference is whether other new/thin
    # wallets are known to have arrived around the same time. Confirms the
    # cluster factor is actually doing something, not just decorative.
    h = _cp(seconds_old=20 * 3600, position_usd=50_000.0)
    solo = compute_insider_score(h, NOW)
    clustered = compute_insider_score(h, NOW, cluster_factor=1.0)
    assert clustered.insider_score > solo.insider_score


def test_established_trader_is_not_flagged_by_these_cases_thresholds():
    # The negative control: an ordinary, long-established, moderately-sized
    # trader should NOT clear alert_threshold just for placing one large-ish
    # bet. (Still not a real false-positive-rate measurement -- see module
    # docstring above -- just confirms the heuristic isn't hair-triggered.)
    h = Holder(
        "0xnormal", "YES", 8_000.0, 55.0, "Gold",
        n_trades=150, first_active_ts=NOW - 400 * 86400, avg_bet_usd=6_000.0,
        win_rate_weighted=0.58,
    )
    flag = compute_insider_score(h, NOW)
    assert flag is None or flag.insider_score < cfg.INSIDER.alert_threshold


# ---------------------------------------------------------------------------
# Grouped-by-market listing (the Insiders tab: one row per market, not one
# row per flagged wallet)
# ---------------------------------------------------------------------------

def test_list_markets_with_flags_groups_one_row_per_market():
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1")
    _insert_market(conn, "c2", event_id="e2")
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 80.0, now=NOW)
    _insert_flag(conn, "c1", "0xb", "NO", 30_000.0, 60.0, now=NOW - 100)
    _insert_flag(conn, "c2", "0xc", "YES", 20_000.0, 40.0, now=NOW - 50)
    conn.commit()

    markets = list_markets_with_flags(conn)
    assert len(markets) == 2  # not 3 -- c1's two flags collapse into one row
    by_cid = {m["condition_id"]: m for m in markets}
    assert by_cid["c1"]["yes_flag_count"] == 1 and by_cid["c1"]["no_flag_count"] == 1
    assert by_cid["c1"]["last_detected_at"] == NOW  # the newer of its two flags
    conn.close()


def test_list_markets_with_flags_respects_min_score():
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1")
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 40.0)  # below a high min_score
    conn.commit()

    assert list_markets_with_flags(conn, min_score=70.0) == []
    assert len(list_markets_with_flags(conn, min_score=0.0)) == 1
    conn.close()


def test_near_resolved_market_hidden_by_default():
    """
    A market priced at $0.999 has no room left to trade an edge -- it should
    be dropped from the default Insiders listing but still reachable with
    include_near_resolved=True (e.g. for an audit view).
    """
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1", outcome_prices='["0.999", "0.001"]')
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 80.0)
    conn.commit()

    assert list_markets_with_flags(conn) == []
    shown = list_markets_with_flags(conn, include_near_resolved=True)
    assert len(shown) == 1
    assert shown[0]["current_price_yes"] == 0.999
    conn.close()


def test_near_resolved_market_on_no_side_also_hidden():
    """Symmetric: YES priced at $0.001 (i.e. NO is the ~certain outcome) is equally hidden."""
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1", outcome_prices='["0.0005", "0.9995"]')
    _insert_flag(conn, "c1", "0xa", "NO", 50_000.0, 80.0)
    conn.commit()

    assert list_markets_with_flags(conn) == []
    conn.close()


def test_market_with_room_to_trade_is_not_hidden():
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1", outcome_prices='["0.78", "0.22"]')
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 80.0)
    conn.commit()

    markets = list_markets_with_flags(conn)
    assert len(markets) == 1
    assert markets[0]["current_price_yes"] == 0.78
    conn.close()


def test_near_resolved_margin_is_one_cent():
    """
    Pins the current margin at $0.01 -- 99.5c is within a cent of $1 (hidden),
    98.5c is a full cent and a half away (not hidden). Catches an accidental
    change to near_resolved_price_margin as clearly as a config-value assert
    would, but through the behavior a caller actually sees.
    """
    conn = _make_conn()
    _insert_market(conn, "hidden", event_id="e1", outcome_prices='["0.995", "0.005"]')
    _insert_market(conn, "shown", event_id="e2", outcome_prices='["0.985", "0.015"]')
    _insert_flag(conn, "hidden", "0xa", "YES", 50_000.0, 80.0)
    _insert_flag(conn, "shown", "0xb", "YES", 50_000.0, 80.0)
    conn.commit()

    markets = {m["condition_id"] for m in list_markets_with_flags(conn)}
    assert markets == {"shown"}
    conn.close()


def test_missing_price_is_not_treated_as_near_resolved():
    """No outcome_prices on record (e.g. a stale/never-refreshed market) shouldn't hide the flag."""
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1", outcome_prices="[]")
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 80.0)
    conn.commit()

    assert len(list_markets_with_flags(conn)) == 1
    conn.close()


def test_list_flags_filtered_to_one_market():
    conn = _make_conn()
    _insert_market(conn, "c1", event_id="e1")
    _insert_market(conn, "c2", event_id="e2")
    _insert_flag(conn, "c1", "0xa", "YES", 50_000.0, 80.0)
    _insert_flag(conn, "c2", "0xb", "NO", 30_000.0, 60.0)
    conn.commit()

    flags = list_flags(conn, condition_id="c1")
    assert len(flags) == 1
    assert flags[0]["wallet"] == "0xa"
    conn.close()
