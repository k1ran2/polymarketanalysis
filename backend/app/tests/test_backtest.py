"""
Unit tests for the backtest data reconstruction (app.backtest_data) and
engine (app.backtest_engine). Pure/deterministic pieces only -- no network --
run against a synthetic in-memory market so the pnl math and no-lookahead
gating can be checked exactly, not just "looks reasonable".
"""
import dataclasses
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SCHEMA, MIGRATIONS
from app.backtest_data import (
    bulk_fetch_status, bulk_price_series, bulk_share_timeline, bulk_wallet_positions,
    price_at_or_before, price_at_or_before_series, reconstruct_holders_at, wallet_positions_asof,
)
from app.backtest_engine import (
    _build_checkpoints, _has_no_edge, _holders_at, _holders_at_fast, _parse_verdict_side,
    _search_objective, _summarize_run, _wallets_worth_scoring, build_insider_cfg, build_verdict_cfg,
    get_run, get_search, list_searches, reconcile_orphaned_runs, start_search, train_test_split_markets,
)
from app.backtest_data import BacktestMarket
from app import config as cfg
from app.insiders import compute_insider_score
from app.scoring import Holder

NOW = 1_700_000_000
DAY = 86400


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _insert_trade(conn, cid, wallet, side, outcome, price, size, ts):
    conn.execute(
        "INSERT INTO backtest_market_trades (condition_id, wallet, side, outcome, price, size, timestamp) "
        "VALUES (?,?,?,?,?,?,?)",
        (cid, wallet, side, outcome, price, size, ts),
    )


def _insert_price(conn, token_id, cid, ts, price):
    conn.execute(
        "INSERT INTO backtest_price_history (token_id, condition_id, timestamp, price) VALUES (?,?,?,?)",
        (token_id, cid, ts, price),
    )


# ---------------------------------------------------------------------------
# reconstruct_holders_at
# ---------------------------------------------------------------------------

def test_reconstruction_nets_buys_and_sells():
    conn = _make_conn()
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 100, NOW)
    _insert_trade(conn, "c1", "0xa", "SELL", "YES", 0.6, 40, NOW + 10)
    conn.commit()

    recon = reconstruct_holders_at(conn, "c1", NOW + 100)
    assert recon["shares"][("0xa", "YES")] == 60
    conn.close()


def test_reconstruction_ignores_trades_after_as_of():
    conn = _make_conn()
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 100, NOW)
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 500, NOW + 1000)  # after the checkpoint
    conn.commit()

    recon = reconstruct_holders_at(conn, "c1", NOW + 10)
    assert recon["shares"][("0xa", "YES")] == 100  # the later buy must not leak in (no-lookahead)
    conn.close()


def test_reconstruction_tracks_first_seen_per_wallet_and_side():
    conn = _make_conn()
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 10, NOW)
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 10, NOW + 500)
    conn.commit()

    recon = reconstruct_holders_at(conn, "c1", NOW + 1000)
    assert recon["first_seen"][("0xa", "YES")] == NOW  # earliest, not latest
    conn.close()


# ---------------------------------------------------------------------------
# price_at_or_before
# ---------------------------------------------------------------------------

def test_price_prefers_most_recent_sample_at_or_before():
    conn = _make_conn()
    _insert_price(conn, "tok1", "c1", NOW - 100, 0.40)
    _insert_price(conn, "tok1", "c1", NOW - 10, 0.55)
    _insert_price(conn, "tok1", "c1", NOW + 100, 0.90)  # after -- must not be used
    conn.commit()

    assert price_at_or_before(conn, "tok1", NOW) == 0.55
    conn.close()


def test_price_falls_back_to_nearest_after_within_window():
    conn = _make_conn()
    _insert_price(conn, "tok1", "c1", NOW + 30, 0.62)  # only sample is slightly after t
    conn.commit()

    assert price_at_or_before(conn, "tok1", NOW, lookback_hours=1) == 0.62
    conn.close()


def test_price_none_when_nothing_within_lookback():
    conn = _make_conn()
    _insert_price(conn, "tok1", "c1", NOW - 10 * DAY, 0.30)
    conn.commit()

    assert price_at_or_before(conn, "tok1", NOW, lookback_hours=48) is None
    conn.close()


# ---------------------------------------------------------------------------
# wallet_positions_asof -- the no-lookahead gate on a wallet's own history
# ---------------------------------------------------------------------------

def test_wallet_positions_asof_excludes_future_closes():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
        "realized_pnl, cur_price, timestamp) VALUES (?,?,?,?,?,?,?)",
        ("0xa", "cX", 0.3, 1000, 200, 1.0, NOW - DAY),
    )
    conn.execute(
        "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
        "realized_pnl, cur_price, timestamp) VALUES (?,?,?,?,?,?,?)",
        ("0xa", "cY", 0.7, 2000, -500, 0.0, NOW + DAY),  # closes AFTER the checkpoint
    )
    conn.commit()

    positions = wallet_positions_asof(conn, "0xa", NOW)
    assert len(positions) == 1
    assert positions[0].condition_id == "cX"
    conn.close()


# ---------------------------------------------------------------------------
# Bulk loaders -- must be exactly equivalent to the per-timestamp functions
# above, just loaded once and sliced in memory. This equivalence is the
# entire safety argument for the speed optimization: these are drop-in
# replacements for a checkpoint loop calling the single-timestamp versions
# once per wallet/token per checkpoint, verified here against random data
# and end-to-end against a golden fixture in test_backtest_golden.py.
# ---------------------------------------------------------------------------

def test_bulk_price_series_matches_price_at_or_before_on_random_queries():
    conn = _make_conn()
    rng = random.Random(7)
    for _ in range(30):
        _insert_price(conn, "tok1", "c1", NOW + rng.randint(-5 * DAY, 5 * DAY), rng.uniform(0.01, 0.99))
    conn.commit()

    series = bulk_price_series(conn, "tok1")
    assert series == sorted(series)  # ascending, as documented
    for _ in range(50):
        ts = NOW + random.Random().randint(-6 * DAY, 6 * DAY)
        lookback = rng.choice([1, 12, 48, 240])
        assert price_at_or_before_series(series, ts, lookback_hours=lookback) == price_at_or_before(conn, "tok1", ts, lookback_hours=lookback)
    conn.close()


def test_bulk_wallet_positions_matches_wallet_positions_asof():
    conn = _make_conn()
    rng = random.Random(11)
    wallets = ["0xa", "0xb", "0xc"]
    for w in wallets:
        for _ in range(rng.randint(0, 8)):
            conn.execute(
                "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
                "realized_pnl, cur_price, timestamp) VALUES (?,?,?,?,?,?,?)",
                (w, f"c{rng.randint(0,5)}", rng.uniform(0, 1), rng.uniform(1, 1000),
                 rng.uniform(-500, 500), rng.choice([0.0, 1.0]), NOW + rng.randint(-10 * DAY, 10 * DAY)),
            )
    conn.commit()

    bulk = bulk_wallet_positions(conn, wallets + ["0xnoposition"])
    assert bulk["0xnoposition"] == []
    for w in wallets:
        for as_of in (NOW - 5 * DAY, NOW, NOW + 5 * DAY, NOW + 20 * DAY):
            prefix_len = bisect_len(bulk[w], as_of)
            expected = wallet_positions_asof(conn, w, as_of)
            assert len(bulk[w][:prefix_len]) == len(expected)
            assert {p.timestamp for p in bulk[w][:prefix_len]} == {p.timestamp for p in expected}
    conn.close()


def bisect_len(sorted_positions, as_of_ts):
    import bisect as _bisect
    return _bisect.bisect_right([p.timestamp for p in sorted_positions], as_of_ts)


def test_bulk_wallet_positions_as_of_ts_excludes_later_closes_but_keeps_earlier_ones():
    """The as_of_ts bound (added for a real, measured speed win -- a backtest
    never needs a wallet's positions closed after its last checkpoint) must
    exclude everything after the bound while including everything at/before
    it, exactly matching what an unbounded load sliced with a bisect would
    give -- a caller must never see a DIFFERENT result, only fewer wasted
    rows loaded to get it."""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
        "realized_pnl, cur_price, timestamp) VALUES ('0xa', 'c1', 0.5, 100, 10, 1.0, ?)", (NOW,),
    )
    conn.execute(
        "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
        "realized_pnl, cur_price, timestamp) VALUES ('0xa', 'c2', 0.5, 100, 10, 1.0, ?)", (NOW + DAY,),
    )
    conn.execute(
        "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
        "realized_pnl, cur_price, timestamp) VALUES ('0xa', 'c3', 0.5, 100, 10, 1.0, ?)", (NOW + 30 * DAY,),
    )
    conn.commit()

    unbounded = bulk_wallet_positions(conn, ["0xa"])
    bounded = bulk_wallet_positions(conn, ["0xa"], as_of_ts=NOW + DAY)

    assert len(unbounded["0xa"]) == 3          # nothing filtered without a bound
    assert len(bounded["0xa"]) == 2            # the +30 day position excluded
    assert {p.timestamp for p in bounded["0xa"]} == {NOW, NOW + DAY}  # boundary is inclusive
    conn.close()


def test_bulk_fetch_status_matches_individual_lookups():
    conn = _make_conn()
    conn.execute("INSERT INTO backtest_wallet_sync (wallet, synced_at, fetch_status) VALUES ('0xa', ?, 'ok')", (NOW,))
    conn.execute("INSERT INTO backtest_wallet_sync (wallet, synced_at, fetch_status) VALUES ('0xb', ?, 'error')", (NOW,))
    conn.commit()

    result = bulk_fetch_status(conn, ["0xa", "0xb", "0xnever_fetched"])
    assert result == {"0xa": "ok", "0xb": "error"}  # 0xnever_fetched absent, matching the "no row" case
    conn.close()


def test_bulk_share_timeline_matches_reconstruct_holders_at_per_checkpoint():
    conn = _make_conn()
    rng = random.Random(99)
    for _ in range(40):
        wallet = rng.choice(["0xa", "0xb", "0xc", "0xd"])
        side = rng.choice(["BUY", "SELL"])
        outcome = rng.choice(["YES", "NO"])
        _insert_trade(conn, "c1", wallet, side, outcome, rng.uniform(0.1, 0.9), rng.uniform(1, 100),
                      NOW + rng.randint(0, 20 * DAY))
    conn.commit()

    checkpoints = sorted({NOW + i * 6 * 3600 for i in range(0, 80)})
    by_checkpoint, first_seen = bulk_share_timeline(conn, "c1", checkpoints)

    for t in checkpoints[::7]:  # sample, not every checkpoint -- O(checkpoints^2) brute force otherwise
        expected = reconstruct_holders_at(conn, "c1", t)
        assert by_checkpoint[t] == expected["shares"]
        # bulk_share_timeline returns ONE first_seen dict covering the whole
        # sweep (not re-scoped per checkpoint) -- correct as long as every
        # key actually present in this checkpoint's shares snapshot maps to
        # the same first_seen value reconstruct_holders_at would give when
        # asked as-of exactly this t (a key can only enter `shares` on the
        # same trade row that sets its first_seen, so the value can never
        # change afterward -- see bulk_share_timeline's docstring reasoning).
        for key, expected_ts in expected["first_seen"].items():
            assert first_seen[key] == expected_ts
    conn.close()


def test_bulk_share_timeline_snapshots_are_independent_across_checkpoints():
    """A later checkpoint's snapshot must not be the SAME mutable dict as an
    earlier one's -- silently aliasing them would make one checkpoint's
    consumer see another's holdings if either ever mutated its copy."""
    conn = _make_conn()
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 100, NOW)
    _insert_trade(conn, "c1", "0xa", "BUY", "YES", 0.5, 50, NOW + 100)
    conn.commit()

    by_checkpoint, _ = bulk_share_timeline(conn, "c1", [NOW, NOW + 50, NOW + 200])
    assert by_checkpoint[NOW][("0xa", "YES")] == 100
    assert by_checkpoint[NOW + 50][("0xa", "YES")] == 100  # second buy hasn't landed yet
    assert by_checkpoint[NOW + 200][("0xa", "YES")] == 150
    by_checkpoint[NOW][("0xa", "YES")] = 999999  # mutate the earliest snapshot
    assert by_checkpoint[NOW + 200][("0xa", "YES")] == 150  # unaffected -- proves independence
    conn.close()


# ---------------------------------------------------------------------------
# _holders_at_fast -- must produce EXACTLY _holders_at()'s output (same
# Holder fields, same order-independent set), just from precomputed bulk
# structures instead of per-checkpoint queries. This is the direct,
# surgical safety net for the speed optimization; test_backtest_golden.py
# is the end-to-end one (verifies actual trades/pnl don't change either).
# ---------------------------------------------------------------------------

def _holders_key(h):
    return (h.wallet, h.side, round(h.position_usd, 6), h.score_percentile, h.tier,
            h.n_trades, h.first_active_ts, h.fetch_status,
            round(h.avg_bet_usd, 6) if h.avg_bet_usd is not None else None,
            h.win_rate_weighted, h.first_seen_at)


def test_holders_at_fast_matches_holders_at_on_random_data():
    conn = _make_conn()
    rng = random.Random(2024)
    cid = "c1"
    wallets = [f"0x{i}" for i in range(6)]

    for _ in range(60):
        wallet = rng.choice(wallets)
        side = rng.choice(["BUY", "SELL"])
        outcome = rng.choice(["YES", "NO"])
        _insert_trade(conn, cid, wallet, side, outcome, rng.uniform(0.1, 0.9), rng.uniform(1, 200),
                      NOW + rng.randint(0, 30 * DAY))
    for tok in ("tokY", "tokN"):
        for _ in range(20):
            _insert_price(conn, tok, cid, NOW + rng.randint(-1 * DAY, 31 * DAY), rng.uniform(0.02, 0.98))
    for wallet in wallets:
        for _ in range(rng.randint(0, 6)):
            conn.execute(
                "INSERT INTO backtest_wallet_positions (wallet, condition_id, avg_price, total_bought, "
                "realized_pnl, cur_price, timestamp) VALUES (?,?,?,?,?,?,?)",
                (wallet, f"other{rng.randint(0,3)}", rng.uniform(0, 1), rng.uniform(1, 500),
                 rng.uniform(-200, 200), rng.choice([0.0, 1.0]), NOW + rng.randint(-5 * DAY, 30 * DAY)),
            )
        if rng.random() < 0.7:
            conn.execute(
                "INSERT INTO backtest_wallet_sync (wallet, synced_at, fetch_status) VALUES (?, ?, ?)",
                (wallet, NOW, rng.choice(["ok", "error"])),
            )
    conn.execute(
        "INSERT INTO backtest_market_meta (condition_id, clob_token_ids, synced_at) VALUES (?, ?, ?)",
        (cid, json.dumps(["tokY", "tokN"]), NOW),
    )
    conn.commit()

    checkpoints = sorted({NOW + i * 6 * 3600 for i in range(0, 100)})
    token_for_side = {"YES": "tokY", "NO": "tokN"}
    price_series_by_token = {"tokY": bulk_price_series(conn, "tokY"), "tokN": bulk_price_series(conn, "tokN")}
    price_timestamps_by_token = {tok: [t for t, _ in s] for tok, s in price_series_by_token.items()}
    by_checkpoint, first_seen = bulk_share_timeline(conn, cid, checkpoints)
    all_wallets = list({w for (w, _o) in first_seen.keys()})
    positions_by_wallet = bulk_wallet_positions(conn, all_wallets)
    positions_ts_by_wallet = {w: [p.timestamp for p in plist] for w, plist in positions_by_wallet.items()}
    fetch_status_by_wallet = bulk_fetch_status(conn, all_wallets)
    raw_stats_cache: dict = {}

    for t in checkpoints[::5]:  # sample -- _holders_at's per-call cost makes every point too slow
        expected = sorted((_holders_key(h) for h in _holders_at(conn, cid, t)))
        actual = sorted((_holders_key(h) for h in _holders_at_fast(
            t, by_checkpoint.get(t, {}), first_seen, token_for_side, price_series_by_token,
            price_timestamps_by_token, positions_by_wallet, positions_ts_by_wallet,
            fetch_status_by_wallet, raw_stats_cache,
        )))
        assert actual == expected, f"mismatch at t={t}"
    conn.close()


# ---------------------------------------------------------------------------
# _build_checkpoints
# ---------------------------------------------------------------------------

def test_checkpoints_span_start_to_resolution_at_configured_interval():
    market = BacktestMarket(
        condition_id="c1", question=None, slug=None, category=None, clob_token_ids=["t1", "t2"],
        volume=0.0, start_ts=NOW, end_ts=NOW + 10 * DAY, resolved_ts=NOW + 10 * DAY, resolved_yes=True,
    )
    points = _build_checkpoints(market)
    assert points[0] == NOW
    assert points[-1] < NOW + 10 * DAY
    assert all(points[i + 1] > points[i] for i in range(len(points) - 1))


def test_checkpoints_empty_without_a_resolvable_window():
    market = BacktestMarket(
        condition_id="c1", question=None, slug=None, category=None, clob_token_ids=["t1", "t2"],
        volume=0.0, start_ts=None, end_ts=None, resolved_ts=None, resolved_yes=True,
    )
    assert _build_checkpoints(market) == []


def test_checkpoints_downsample_instead_of_truncate_for_very_long_markets():
    from app import config as cfg
    market = BacktestMarket(
        condition_id="c1", question=None, slug=None, category=None, clob_token_ids=["t1", "t2"],
        volume=0.0, start_ts=NOW, end_ts=NOW + 400 * DAY, resolved_ts=NOW + 400 * DAY, resolved_yes=True,
    )
    points = _build_checkpoints(market)
    assert len(points) <= cfg.BACKTEST.max_checkpoints_per_market
    # Downsampled, not just the first N -- the last checkpoint should still be
    # well into the market's life, not clustered near the start.
    assert points[-1] > NOW + 300 * DAY


# ---------------------------------------------------------------------------
# _parse_verdict_side -- MarketVerdict label -> (side, strength)
# ---------------------------------------------------------------------------

def test_parse_strong_buy():
    assert _parse_verdict_side("Strong Buy YES") == ("YES", "strong_buy")
    assert _parse_verdict_side("Strong Buy NO") == ("NO", "strong_buy")


def test_parse_lean():
    assert _parse_verdict_side("Lean YES") == ("YES", "lean")


def test_parse_fade_recommends_the_opposite_of_the_faded_side():
    assert _parse_verdict_side("Fade YES (lean NO)") == ("NO", "fade")
    assert _parse_verdict_side("Fade NO (lean YES)") == ("YES", "fade")


def test_parse_non_directional_labels_return_none():
    assert _parse_verdict_side("Mixed / Conflicting") is None
    assert _parse_verdict_side("Insufficient data") is None


# ---------------------------------------------------------------------------
# _summarize_run -- pnl aggregation
# ---------------------------------------------------------------------------

def _insert_sim_trade(conn, run_id, cid, side, resolved_yes, won, pnl_usd, stake_usd=100.0, category="Politics", signal_type=None):
    conn.execute(
        """
        INSERT INTO backtest_trades_sim (run_id, condition_id, question, category, side, signal_type,
            signal_ts, signal_value, entry_price, stake_usd, shares, resolved_yes, won, pnl_usd, pnl_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (run_id, cid, "Q?", category, side, signal_type, NOW, 80.0, 0.5, stake_usd, stake_usd / 0.5,
         int(resolved_yes), int(won), pnl_usd, pnl_usd / stake_usd),
    )


def test_summarize_run_computes_win_rate_and_total_pnl():
    conn = _make_conn()
    _insert_sim_trade(conn, "r1", "c1", "YES", True, True, 100.0)
    _insert_sim_trade(conn, "r1", "c2", "NO", True, False, -100.0)
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        summary = _summarize_run("r1")
    finally:
        engine.db.db_session = orig

    assert summary["n_trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl_usd"] == 0.0
    conn.close()


def test_summarize_run_empty_run_has_no_trades():
    conn = _make_conn()
    conn.commit()
    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        summary = _summarize_run("empty-run")
    finally:
        engine.db.db_session = orig
    assert summary == {
        "n_trades": 0, "win_rate": None, "total_pnl_usd": 0.0, "total_staked_usd": 0.0,
        "roi_pct": None, "avg_pnl_usd": None, "by_category": {}, "by_signal_type": {},
    }
    conn.close()


class _FakeSession:
    """Minimal stand-in for db.db_session() that reuses one already-open test connection."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# reconcile_orphaned_runs -- cleans up a run stuck 'running' by a server
# restart (the single-flight lock is in-memory only, so a restart mid-run
# would otherwise leave that row "running" forever with nothing advancing it)
# ---------------------------------------------------------------------------

def test_reconcile_marks_stuck_running_and_pending_runs_as_error():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_runs (id, created_at, status, params) VALUES (?,?,?,?)",
        ("stuck-running", NOW, "running", "{}"),
    )
    conn.execute(
        "INSERT INTO backtest_runs (id, created_at, status, params) VALUES (?,?,?,?)",
        ("stuck-pending", NOW, "pending", "{}"),
    )
    conn.execute(
        "INSERT INTO backtest_runs (id, created_at, status, params) VALUES (?,?,?,?)",
        ("genuinely-done", NOW, "done", "{}"),
    )
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        reconcile_orphaned_runs()
    finally:
        engine.db.db_session = orig

    rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM backtest_runs").fetchall()}
    assert rows["stuck-running"] == "error"
    assert rows["stuck-pending"] == "error"
    assert rows["genuinely-done"] == "done"  # untouched
    conn.close()


# ---------------------------------------------------------------------------
# get_run -- trades stream in live now that markets run concurrently
# (market_concurrency), instead of only appearing once the whole run is done
# ---------------------------------------------------------------------------

def test_get_run_includes_trades_recorded_so_far_while_still_running():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_runs (id, created_at, status, params, progress_done, progress_total) "
        "VALUES (?,?,?,?,?,?)",
        ("r1", NOW, "running", "{}", 1, 5),
    )
    _insert_sim_trade(conn, "r1", "c1", "YES", True, True, 50.0)
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        run = get_run("r1")
    finally:
        engine.db.db_session = orig

    assert run["status"] == "running"
    assert len(run["trades"]) == 1  # not gated behind status == "done"
    conn.close()


# ---------------------------------------------------------------------------
# build_verdict_cfg / build_insider_cfg -- per-run config overrides
# (architecture for tuning strategy weights from the backtest page, without
# ever touching the live production weights every other tab uses)
# ---------------------------------------------------------------------------

def test_build_verdict_cfg_with_no_overrides_returns_the_live_default_unchanged():
    result = build_verdict_cfg(None)
    assert result is cfg.VERDICT
    assert result.smart_threshold == 60.0


def test_build_verdict_cfg_applies_overrides_without_mutating_the_global():
    original = cfg.VERDICT.smart_threshold
    result = build_verdict_cfg({"smart_threshold": 75.0})
    assert result.smart_threshold == 75.0
    assert result.dumb_threshold == cfg.VERDICT.dumb_threshold  # untouched fields carry over
    assert cfg.VERDICT.smart_threshold == original  # the live global is untouched


def test_build_insider_cfg_applies_overrides_without_mutating_the_global():
    original_w_age = cfg.INSIDER.w_age
    result = build_insider_cfg({"w_age": 0.5, "w_size": 0.5})
    assert result.w_age == 0.5
    assert result.w_size == 0.5
    assert result.w_thin == cfg.INSIDER.w_thin  # untouched
    assert cfg.INSIDER.w_age == original_w_age


def test_insider_weight_override_actually_changes_the_computed_score():
    """Confirms the override isn't just accepted and silently ignored -- it
    has to flow all the way through compute_insider_score's weighted average."""
    holder = Holder(
        wallet="0xa", side="YES", position_usd=50_000, score_percentile=None, tier="Unrated",
        n_trades=0, first_active_ts=None, fetch_status="ok", avg_bet_usd=None,
        win_rate_weighted=None, first_seen_at=None,
    )
    default_flag = compute_insider_score(holder, NOW)
    zero_age_cfg = build_insider_cfg({"w_age": 0.0, "w_size": 0.5, "w_thin": 0.5})
    overridden_flag = compute_insider_score(holder, NOW, insider_cfg=zero_age_cfg)

    assert default_flag is not None and overridden_flag is not None
    assert overridden_flag.insider_score != default_flag.insider_score


# ---------------------------------------------------------------------------
# _has_no_edge -- excludes trades where the market already prices the
# recommended side as a near-certainty (real bug report: a win there pays
# pennies on a $100 stake while a loss costs the full stake, so a handful of
# these can turn a plausible-looking win rate into a negative overall ROI)
# ---------------------------------------------------------------------------

def test_no_edge_excludes_near_certain_high_side():
    assert _has_no_edge(0.998) is True
    assert _has_no_edge(0.986) is True   # from the real bug report screenshot
    assert _has_no_edge(cfg.BACKTEST.max_no_edge_entry_price) is True  # boundary is inclusive


def test_no_edge_keeps_genuine_directional_entries():
    assert _has_no_edge(0.625) is False
    assert _has_no_edge(0.535) is False
    assert _has_no_edge(0.94) is False


def test_no_edge_does_not_exclude_a_near_zero_entry():
    """A low entry price is the OPPOSITE case: a correct contrarian call
    there pays out a large multiple -- real signal value, not noise."""
    assert _has_no_edge(0.002) is False
    assert _has_no_edge(0.01) is False


# ---------------------------------------------------------------------------
# _summarize_run's by_signal_type breakdown -- so a run that allows Strong
# Buy + Lean + Fade can still show whether the strict case (strong_buy)
# performs differently from the loosest fallback (fade) lumped in with it
# ---------------------------------------------------------------------------

def test_summarize_run_breaks_down_by_signal_type():
    conn = _make_conn()
    _insert_sim_trade(conn, "r1", "c1", "YES", True, True, 50.0, signal_type="strong_buy")
    _insert_sim_trade(conn, "r1", "c2", "YES", True, True, 30.0, signal_type="strong_buy")
    _insert_sim_trade(conn, "r1", "c3", "NO", True, False, -100.0, signal_type="fade")
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        summary = _summarize_run("r1")
    finally:
        engine.db.db_session = orig

    by_type = summary["by_signal_type"]
    assert by_type["strong_buy"]["n"] == 2
    assert by_type["strong_buy"]["win_rate"] == 1.0
    assert by_type["strong_buy"]["pnl_usd"] == 80.0
    assert by_type["fade"]["n"] == 1
    assert by_type["fade"]["win_rate"] == 0.0
    assert by_type["fade"]["pnl_usd"] == -100.0
    # The strict case is profitable even though the run overall (2 wins totaling
    # +$80, one loss of -$100) is not -- exactly the distinction this exists to surface.
    assert summary["total_pnl_usd"] == -20.0
    conn.close()


# ---------------------------------------------------------------------------
# _wallets_worth_scoring -- the wallet-discovery floor must respect a per-run
# insider_overrides.min_position_usd override, not always the global default
# (real bug: found while auditing before running large-scale backtests --
# lowering the floor to catch smaller positions had silently no effect,
# since discovery kept using the hardcoded default underneath it)
# ---------------------------------------------------------------------------

def test_wallets_worth_scoring_respects_overridden_insider_floor():
    conn = _make_conn()
    cid = "c1"
    _insert_trade(conn, cid, "0xbig", "BUY", "YES", 0.5, 100_000, NOW)   # $50,000 -- always top-ranked
    _insert_trade(conn, cid, "0xsmall", "BUY", "NO", 0.5, 4_000, NOW)    # $2,000 -- below the $5,000 default floor
    _insert_price(conn, "tokY", cid, NOW, 0.5)
    _insert_price(conn, "tokN", cid, NOW, 0.5)
    conn.commit()

    market = BacktestMarket(
        condition_id=cid, question=None, slug=None, category=None, clob_token_ids=["tokY", "tokN"],
        volume=0.0, start_ts=NOW, end_ts=NOW + DAY, resolved_ts=NOW + DAY, resolved_yes=True,
    )
    checkpoints = [NOW]

    # Force top_n so small holds only qualify via the insider floor, not rank.
    orig_backtest_cfg = cfg.BACKTEST
    cfg.BACKTEST = dataclasses.replace(cfg.BACKTEST, smart_score_top_n_holders=1)
    try:
        default_floor = _wallets_worth_scoring(conn, cid, checkpoints, market, insider_floor=5_000.0)
        lowered_floor = _wallets_worth_scoring(conn, cid, checkpoints, market, insider_floor=1_000.0)
    finally:
        cfg.BACKTEST = orig_backtest_cfg

    assert "0xsmall" not in default_floor  # $2,000 clears neither the default $5,000 floor nor top-1 rank
    assert "0xsmall" in lowered_floor      # ...but does clear a lowered $1,000 override
    conn.close()


# ---------------------------------------------------------------------------
# train_test_split_markets -- the held-out set a weight search must never
# tune against, so "optimal" means "generalizes", not just "fits history"
# ---------------------------------------------------------------------------

def test_train_test_split_markets_is_disjoint_and_covers_everything():
    ids = [f"c{i}" for i in range(20)]
    train, test = train_test_split_markets(ids, test_fraction=0.3, seed=42)
    assert set(train) & set(test) == set()
    assert set(train) | set(test) == set(ids)
    assert len(test) == 6  # round(20 * 0.3)


def test_train_test_split_markets_is_deterministic_given_a_seed():
    ids = [f"c{i}" for i in range(20)]
    train1, test1 = train_test_split_markets(ids, test_fraction=0.3, seed=7)
    train2, test2 = train_test_split_markets(ids, test_fraction=0.3, seed=7)
    assert train1 == train2
    assert test1 == test2


def test_train_test_split_markets_dedupes_input():
    ids = ["a", "b", "a", "c"]
    train, test = train_test_split_markets(ids, test_fraction=0.5, seed=1)
    assert set(train) | set(test) == {"a", "b", "c"}


def test_train_test_split_markets_always_reserves_at_least_one_test_market():
    ids = [f"c{i}" for i in range(5)]
    train, test = train_test_split_markets(ids, test_fraction=0.05, seed=1)
    assert len(test) >= 1  # a near-zero fraction must still hold something out


# ---------------------------------------------------------------------------
# _search_objective -- ROI, but a 0-trade config must score strictly worse
# than any real (even losing) config, or the search "wins" by picking
# thresholds so strict nothing ever fires
# ---------------------------------------------------------------------------

def test_search_objective_prefers_a_real_loss_over_no_trades():
    no_trades = {"n_trades": 0, "roi_pct": None}
    a_real_loss = {"n_trades": 10, "roi_pct": -25.0}
    assert _search_objective(a_real_loss) > _search_objective(no_trades)


def test_search_objective_is_roi_when_trades_exist():
    assert _search_objective({"n_trades": 5, "roi_pct": 12.5}) == 12.5


# ---------------------------------------------------------------------------
# start_search / get_search / list_searches -- persistence + single-flight
# ---------------------------------------------------------------------------

def test_start_search_returns_existing_id_when_one_already_running():
    import app.backtest_engine as engine
    orig_current = engine._current_run_id
    engine._current_run_id = "already-running-id"
    try:
        result = start_search({"n_iterations": 5})
    finally:
        engine._current_run_id = orig_current
    assert result == {"search_id": "already-running-id", "started": False}


def test_get_search_round_trips_json_fields():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_searches (id, created_at, status, params, progress_done, progress_total, "
        "history, best_overrides, best_train_summary, test_summary) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s1", NOW, "done", '{"n_iterations": 3}', 4, 4,
         '[{"overrides": {"w_age": 0.1}, "run_id": "s1-train0", "summary": {"n_trades": 2}}]',
         '{"w_age": 0.1}', '{"n_trades": 2, "roi_pct": 10.0}', '{"n_trades": 1, "roi_pct": 5.0}'),
    )
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        result = get_search("s1")
    finally:
        engine.db.db_session = orig

    assert result["status"] == "done"
    assert result["params"] == {"n_iterations": 3}
    assert result["best_overrides"] == {"w_age": 0.1}
    assert result["test_summary"]["roi_pct"] == 5.0
    assert len(result["history"]) == 1
    conn.close()


def test_get_search_returns_none_for_unknown_id():
    conn = _make_conn()
    conn.commit()
    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        result = get_search("does-not-exist")
    finally:
        engine.db.db_session = orig
    assert result is None
    conn.close()


def test_list_searches_orders_newest_first():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backtest_searches (id, created_at, status, params) VALUES (?,?,?,?)",
        ("older", NOW, "done", "{}"),
    )
    conn.execute(
        "INSERT INTO backtest_searches (id, created_at, status, params) VALUES (?,?,?,?)",
        ("newer", NOW + 100, "running", "{}"),
    )
    conn.commit()

    import app.backtest_engine as engine
    orig = engine.db.db_session
    engine.db.db_session = lambda: _FakeSession(conn)
    try:
        results = list_searches(limit=10)
    finally:
        engine.db.db_session = orig

    assert [r["id"] for r in results] == ["newer", "older"]
    conn.close()
