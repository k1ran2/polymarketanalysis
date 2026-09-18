"""
P&L math for live/manual/imported trades -- deliberately the SAME shape as
app.backtest_engine._summarize_run's output, so the frontend's existing
results components can render a live trading summary with no new code.

This is also "the tracker": there is no separate tracker data model. A
bot-only P&L view and a hand-logged one disagreeing with each other would be
the worst possible failure for a tool whose whole job is telling you whether
a strategy works, so `trades` (source='bot'|'manual'|'imported') is the one
table everything reads from -- see db.py's trades table docstring.

Pure functions only here -- no I/O. Reconciliation (checking which open
trades' markets have since resolved, via Gamma or the live `markets` table)
is a separate, DB/network-touching piece that calls into these once it has
the resolution outcome; that piece doesn't exist yet, since there are no
live trades to reconcile until credentials are connected -- see the trading
build order.
"""
from __future__ import annotations

from typing import Optional


def compute_realized_pnl(
    side: str, filled_shares: float, avg_fill_price: float, fees_usd: float, resolved_yes: bool,
) -> float:
    """A resolved position's realized P&L: shares * (payout - entry) - fees.
    payout is 1.0 if this trade's side matches how the market resolved, else 0.0
    -- exactly backtest_engine's pnl_usd formula, so a live trade and a
    backtested one are graded identically."""
    payout = 1.0 if (side == "YES") == resolved_yes else 0.0
    return filled_shares * (payout - avg_fill_price) - fees_usd


def mark_to_market_pnl(side: str, filled_shares: float, avg_fill_price: float, current_price_yes: float) -> float:
    """Unrealized P&L for a still-open position, marked at the current YES
    price (current_price_yes for a YES holder, 1-current_price_yes for a NO
    holder) -- for display only, never used to trigger anything."""
    current_price = current_price_yes if side == "YES" else (1.0 - current_price_yes)
    return filled_shares * (current_price - avg_fill_price)


def summarize_trades(trades: list) -> dict:
    """
    Same shape as backtest_engine._summarize_run: n_trades, win_rate,
    total_pnl_usd, total_staked_usd, roi_pct, avg_pnl_usd, by_category,
    by_signal_type -- built from `trades` rows (dicts) with at least
    cost_usd, realized_pnl_usd, won (bool), category, signal_type.

    Only RESOLVED trades (realized_pnl_usd is not None) count toward these
    numbers -- an open position's eventual P&L is unknown, so including it
    as a live "0" would understate risk and overstate the win rate. Use
    mark_to_market_pnl separately for an "unrealized" display figure.
    """
    resolved = [t for t in trades if t.get("realized_pnl_usd") is not None]
    n = len(resolved)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": None, "total_pnl_usd": 0.0, "total_staked_usd": 0.0,
            "roi_pct": None, "avg_pnl_usd": None, "by_category": {}, "by_signal_type": {},
        }

    total_pnl = sum(t["realized_pnl_usd"] for t in resolved)
    total_staked = sum(t.get("cost_usd") or t.get("requested_stake_usd") or 0.0 for t in resolved)
    wins = sum(1 for t in resolved if t.get("won"))

    by_category: dict = {}
    for t in resolved:
        cat = t.get("category") or "Uncategorized"
        bucket = by_category.setdefault(cat, {"n": 0, "wins": 0, "pnl_usd": 0.0})
        bucket["n"] += 1
        bucket["wins"] += 1 if t.get("won") else 0
        bucket["pnl_usd"] += t["realized_pnl_usd"]
    for bucket in by_category.values():
        bucket["win_rate"] = bucket["wins"] / bucket["n"] if bucket["n"] else None

    by_signal_type: dict = {}
    for t in resolved:
        sig = t.get("signal_type") or "unknown"
        bucket = by_signal_type.setdefault(sig, {"n": 0, "wins": 0, "pnl_usd": 0.0, "staked_usd": 0.0})
        bucket["n"] += 1
        bucket["wins"] += 1 if t.get("won") else 0
        bucket["pnl_usd"] += t["realized_pnl_usd"]
        bucket["staked_usd"] += t.get("cost_usd") or t.get("requested_stake_usd") or 0.0
    for bucket in by_signal_type.values():
        bucket["win_rate"] = bucket["wins"] / bucket["n"] if bucket["n"] else None
        bucket["roi_pct"] = (bucket["pnl_usd"] / bucket["staked_usd"] * 100.0) if bucket["staked_usd"] > 0 else None

    return {
        "n_trades": n,
        "win_rate": wins / n,
        "total_pnl_usd": total_pnl,
        "total_staked_usd": total_staked,
        "roi_pct": (total_pnl / total_staked * 100.0) if total_staked > 0 else None,
        "avg_pnl_usd": total_pnl / n,
        "by_category": by_category,
        "by_signal_type": by_signal_type,
    }


def roll_day_totals(trades: list) -> dict:
    """Today's realized_pnl_usd / spent_usd / n_trades for trading_day_ledger
    -- `trades` should already be filtered to today's UTC day and to
    source='bot' (a hand-logged manual trade doesn't count against the
    bot's own daily caps). spent_usd counts every trade that actually cost
    money regardless of resolution status (an open position already spent
    the cash); realized_pnl_usd only counts resolved ones (open positions
    haven't realized anything yet)."""
    spent = sum(t.get("cost_usd") or t.get("requested_stake_usd") or 0.0 for t in trades)
    realized = sum(t["realized_pnl_usd"] for t in trades if t.get("realized_pnl_usd") is not None)
    return {"spent_usd": spent, "realized_pnl_usd": realized, "n_trades": len(trades)}
