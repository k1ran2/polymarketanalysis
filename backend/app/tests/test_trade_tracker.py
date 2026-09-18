"""
Unit tests for app.trade_tracker -- pure P&L math, deliberately shaped like
backtest_engine._summarize_run's output (see that function's tests in
test_backtest.py for the shape this mirrors).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.trade_tracker import compute_realized_pnl, mark_to_market_pnl, roll_day_totals, summarize_trades


# ---------------------------------------------------------------------------
# compute_realized_pnl
# ---------------------------------------------------------------------------

def test_realized_pnl_full_win():
    pnl = compute_realized_pnl("YES", filled_shares=200.0, avg_fill_price=0.5, fees_usd=0.0, resolved_yes=True)
    assert pnl == 100.0  # 200 * (1.0 - 0.5)


def test_realized_pnl_full_loss():
    pnl = compute_realized_pnl("YES", filled_shares=200.0, avg_fill_price=0.5, fees_usd=0.0, resolved_yes=False)
    assert pnl == -100.0  # 200 * (0.0 - 0.5)


def test_realized_pnl_no_side_win_when_market_resolves_no():
    pnl = compute_realized_pnl("NO", filled_shares=100.0, avg_fill_price=0.4, fees_usd=0.0, resolved_yes=False)
    assert pnl == 60.0  # 100 * (1.0 - 0.4)


def test_realized_pnl_subtracts_fees():
    pnl = compute_realized_pnl("YES", filled_shares=200.0, avg_fill_price=0.5, fees_usd=2.5, resolved_yes=True)
    assert pnl == 97.5


# ---------------------------------------------------------------------------
# mark_to_market_pnl
# ---------------------------------------------------------------------------

def test_mark_to_market_yes_holder_gain():
    pnl = mark_to_market_pnl("YES", filled_shares=100.0, avg_fill_price=0.4, current_price_yes=0.6)
    assert abs(pnl - 20.0) < 1e-9


def test_mark_to_market_no_holder_uses_inverse_price():
    # Bought NO at 0.4 (i.e. paid 0.4 for NO shares); YES price has since
    # dropped to 0.3, so NO's implied price rose to 0.7 -- a gain.
    pnl = mark_to_market_pnl("NO", filled_shares=100.0, avg_fill_price=0.4, current_price_yes=0.3)
    assert abs(pnl - 30.0) < 1e-9


# ---------------------------------------------------------------------------
# summarize_trades -- same shape as backtest_engine._summarize_run
# ---------------------------------------------------------------------------

def test_summarize_trades_empty_matches_backtest_empty_shape():
    assert summarize_trades([]) == {
        "n_trades": 0, "win_rate": None, "total_pnl_usd": 0.0, "total_staked_usd": 0.0,
        "roi_pct": None, "avg_pnl_usd": None, "by_category": {}, "by_signal_type": {},
    }


def test_summarize_trades_excludes_still_open_positions():
    trades = [
        {"cost_usd": 100.0, "realized_pnl_usd": 50.0, "won": True, "category": "Politics", "signal_type": "strong_buy"},
        {"cost_usd": 100.0, "realized_pnl_usd": None, "won": None, "category": "Politics", "signal_type": "strong_buy"},  # still open
    ]
    summary = summarize_trades(trades)
    assert summary["n_trades"] == 1  # the open one isn't counted
    assert summary["total_pnl_usd"] == 50.0


def test_summarize_trades_computes_win_rate_and_roi():
    trades = [
        {"cost_usd": 100.0, "realized_pnl_usd": 100.0, "won": True, "category": "Sports", "signal_type": "insider"},
        {"cost_usd": 100.0, "realized_pnl_usd": -100.0, "won": False, "category": "Sports", "signal_type": "insider"},
    ]
    summary = summarize_trades(trades)
    assert summary["n_trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl_usd"] == 0.0
    assert summary["total_staked_usd"] == 200.0
    assert summary["roi_pct"] == 0.0


def test_summarize_trades_by_category_and_signal_type_breakdown():
    trades = [
        {"cost_usd": 100.0, "realized_pnl_usd": 50.0, "won": True, "category": "Politics", "signal_type": "strong_buy"},
        {"cost_usd": 100.0, "realized_pnl_usd": 30.0, "won": True, "category": "Politics", "signal_type": "lean"},
        {"cost_usd": 100.0, "realized_pnl_usd": -100.0, "won": False, "category": "Sports", "signal_type": "insider"},
    ]
    summary = summarize_trades(trades)
    assert summary["by_category"]["Politics"]["n"] == 2
    assert summary["by_category"]["Politics"]["pnl_usd"] == 80.0
    assert summary["by_category"]["Sports"]["win_rate"] == 0.0
    assert summary["by_signal_type"]["strong_buy"]["roi_pct"] == 50.0
    assert summary["by_signal_type"]["insider"]["roi_pct"] == -100.0


def test_summarize_trades_uncategorized_fallback():
    trades = [{"cost_usd": 100.0, "realized_pnl_usd": 10.0, "won": True, "category": None, "signal_type": None}]
    summary = summarize_trades(trades)
    assert "Uncategorized" in summary["by_category"]
    assert "unknown" in summary["by_signal_type"]


def test_summarize_trades_falls_back_to_requested_stake_when_cost_usd_missing():
    trades = [{"requested_stake_usd": 50.0, "realized_pnl_usd": 25.0, "won": True, "category": "X", "signal_type": "y"}]
    summary = summarize_trades(trades)
    assert summary["total_staked_usd"] == 50.0
    assert summary["roi_pct"] == 50.0


# ---------------------------------------------------------------------------
# roll_day_totals
# ---------------------------------------------------------------------------

def test_roll_day_totals_counts_spend_regardless_of_resolution():
    trades = [
        {"cost_usd": 1.0, "realized_pnl_usd": None},  # still open -- spent, but not realized
        {"cost_usd": 1.0, "realized_pnl_usd": -0.5},  # resolved, a loss
    ]
    totals = roll_day_totals(trades)
    assert totals["spent_usd"] == 2.0
    assert totals["realized_pnl_usd"] == -0.5
    assert totals["n_trades"] == 2
