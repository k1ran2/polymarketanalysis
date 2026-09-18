"""
Unit/integration tests for app.trade_watcher, against a real in-memory
SQLite DB (the actual SCHEMA, the actual scoring.overall_market_verdict via
get_holders_for_market -- not hand-built Holder lists) and MockTrader (the
only trader implementation that exists -- see that class's docstring for
why every trade this module can ever create is, by construction, fake).
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SCHEMA, MIGRATIONS
from app.trade_watcher import (
    MockTrader, evaluate_market, get_or_create_day_ledger, get_settings, poll_once,
)

NOW = int(time.time())
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


def _insert_default_settings(conn, **overrides):
    defaults = dict(
        enabled=1, strategy="smart_money", max_stake_usd=1.0, max_open_positions=3,
        max_open_exposure_usd=5.0, max_position_per_market_usd=1.0, max_daily_loss_usd=10.0,
        max_daily_spend_usd=25.0, max_trades_per_day=5, min_conviction=50.0,
        min_signal_strength="lean", max_entry_price=0.90, min_entry_price=0.05,
        max_slippage_pct=2.0, min_market_liquidity_usd=1000.0, min_hours_to_resolution=1.0,
        order_type="FOK", poll_interval_seconds=300, universe_size=40, resignal_cooldown_hours=24.0,
        updated_at=NOW,
    )
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    qs = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO trading_settings (id, {cols}) VALUES (1, {qs})", tuple(defaults.values()))


def _insert_market(conn, cid, price_yes=0.3, liquidity=50_000.0, end_offset_days=10, closed=0):
    conn.execute(
        "INSERT INTO markets (condition_id, question, category, outcomes, outcome_prices, clob_token_ids, "
        "volume, liquidity, end_date, active, closed, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid, f"Will {cid} happen?", "Politics", json.dumps(["Yes", "No"]),
            json.dumps([str(price_yes), str(round(1 - price_yes, 4))]), json.dumps(["tokY", "tokN"]),
            100_000.0, liquidity, f"2099-01-{1 + end_offset_days:02d}T00:00:00Z" if end_offset_days < 28 else "2099-02-01T00:00:00Z",
            1, closed, NOW,
        ),
    )


def _insert_strong_buy_yes_holders(conn, cid):
    """5 Gold traders long YES, 3 Bronze traders long NO -- clears
    overall_market_verdict's Strong Buy gate the same way test_scoring.py's
    fixtures do."""
    for i in range(5):
        w = f"0xgold{i}"
        conn.execute(
            "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
            "VALUES (?, 80, 50000, 'Gold', 90.0, 2.0, ?)", (w, NOW),
        )
        conn.execute(
            "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
            "VALUES (?, ?, 'YES', 2000.0, 'holders_fast_path', ?)", (cid, w, NOW),
        )
    for i in range(3):
        w = f"0xbronze{i}"
        conn.execute(
            "INSERT INTO traders (wallet, n, volume, tier, percentile, raw_composite, updated_at) "
            "VALUES (?, 10, 1000, 'Bronze', 20.0, -1.0, ?)", (w, NOW),
        )
        conn.execute(
            "INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at) "
            "VALUES (?, ?, 'NO', 300.0, 'holders_fast_path', ?)", (cid, w, NOW),
        )


class _FakeSession:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _patch_db(monkeypatch, conn):
    import app.trade_watcher as watcher_module
    monkeypatch.setattr(watcher_module.db, "db_session", lambda: _FakeSession(conn))


# ---------------------------------------------------------------------------
# MockTrader
# ---------------------------------------------------------------------------

def test_mock_trader_fabricates_a_full_fill_and_records_the_call():
    trader = MockTrader()
    fill = trader.submit_order("c1", "tokY", "YES", 0.4, 1.0)
    assert fill["status"] == "filled"
    assert fill["cost_usd"] == 1.0
    assert abs(fill["filled_shares"] - 2.5) < 1e-9  # 1.0 / 0.4
    assert len(trader.calls) == 1


# ---------------------------------------------------------------------------
# evaluate_market -- direct unit tests against the real DB-join path
# ---------------------------------------------------------------------------

def test_evaluate_market_trades_on_a_clear_strong_buy_signal():
    conn = _make_conn()
    _insert_default_settings(conn)
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()

    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    trader = MockTrader()
    outcome = evaluate_market(conn, "c1", settings, ledger, trader, NOW)

    assert outcome["action"] == "traded"
    assert outcome["side"] == "YES"
    trade = conn.execute("SELECT * FROM trades WHERE id=?", (outcome["trade_id"],)).fetchone()
    assert trade["status"] == "filled"
    assert trade["source"] == "bot"
    assert trade["cost_usd"] == settings["max_stake_usd"]
    signal_row = conn.execute("SELECT * FROM trade_signals WHERE trade_id=?", (outcome["trade_id"],)).fetchone()
    assert signal_row["action"] == "traded"
    conn.close()


def test_evaluate_market_skips_below_liquidity_floor():
    conn = _make_conn()
    _insert_default_settings(conn, min_market_liquidity_usd=100_000.0)
    _insert_market(conn, "c1", liquidity=1000.0)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()

    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "liquidity"}
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0
    conn.close()


def test_evaluate_market_skips_when_entry_price_out_of_band():
    conn = _make_conn()
    # price_yes=0.3 -> entry on the winning YES side is 0.3, fine; but cap
    # max_entry_price below that to force the skip.
    _insert_default_settings(conn, max_entry_price=0.1)
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()

    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "price_out_of_band"}
    conn.close()


def test_evaluate_market_skips_a_closed_market():
    conn = _make_conn()
    _insert_default_settings(conn)
    _insert_market(conn, "c1", closed=1)
    conn.commit()
    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "not_found_or_closed"}
    conn.close()


def test_evaluate_market_skips_when_too_close_to_resolution():
    conn = _make_conn()
    _insert_default_settings(conn, min_hours_to_resolution=999999.0)
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()
    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "too_close_to_resolution"}
    conn.close()


def test_evaluate_market_respects_cooldown_after_a_recent_trade():
    conn = _make_conn()
    _insert_default_settings(conn, resignal_cooldown_hours=24.0)
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()
    conn.execute(
        "INSERT INTO trade_signals (condition_id, side, strategy, conviction, detected_at, action) "
        "VALUES ('c1', 'YES', 'smart_money', 80.0, ?, 'traded')", (NOW - 3600,),  # 1 hour ago
    )
    conn.commit()

    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "cooldown"}
    conn.close()


def test_evaluate_market_respects_risk_caps():
    conn = _make_conn()
    _insert_default_settings(conn, max_open_positions=0)  # nothing can ever be allowed
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.commit()
    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "cap_open_positions"}
    conn.close()


def test_evaluate_market_no_trade_when_no_holders_qualify():
    conn = _make_conn()
    _insert_default_settings(conn)
    _insert_market(conn, "c1", price_yes=0.5)
    # no holders inserted at all
    conn.commit()
    settings = get_settings(conn)
    ledger = get_or_create_day_ledger(conn, "2099-01-01")
    outcome = evaluate_market(conn, "c1", settings, ledger, MockTrader(), NOW)
    assert outcome == {"action": "skipped", "reason": "no_holders"}
    conn.close()


# ---------------------------------------------------------------------------
# poll_once -- the enabled/halted gates
# ---------------------------------------------------------------------------

def test_poll_once_noop_when_disabled(monkeypatch):
    conn = _make_conn()
    _insert_default_settings(conn, enabled=0)
    conn.commit()
    _patch_db(monkeypatch, conn)

    counts = poll_once(MockTrader())
    assert counts == {"evaluated": 0, "traded": 0, "skipped": 0}
    conn.close()


def test_poll_once_noop_when_halted(monkeypatch):
    conn = _make_conn()
    _insert_default_settings(conn)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    conn.execute(
        "INSERT INTO trading_day_ledger (day, halted, halted_reason, updated_at) VALUES (?, 1, 'cap_daily_loss', ?)",
        (day, NOW),
    )
    conn.commit()
    _patch_db(monkeypatch, conn)

    counts = poll_once(MockTrader())
    assert counts == {"evaluated": 0, "traded": 0, "skipped": 0}
    conn.close()


def test_poll_once_evaluates_tracked_markets_and_trades(monkeypatch):
    conn = _make_conn()
    _insert_default_settings(conn)
    _insert_market(conn, "c1", price_yes=0.3)
    _insert_strong_buy_yes_holders(conn, "c1")
    conn.execute("UPDATE markets SET list_rank=1 WHERE condition_id='c1'")
    conn.commit()
    _patch_db(monkeypatch, conn)

    import app.trade_watcher as watcher_module
    monkeypatch.setattr(watcher_module, "analyze_market", lambda cid: None)  # no live network call

    trader = MockTrader()
    counts = poll_once(trader)
    assert counts["evaluated"] == 1
    assert counts["traded"] == 1
    assert len(trader.calls) == 1
    conn.close()
