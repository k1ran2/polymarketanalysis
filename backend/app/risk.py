"""
Pure risk/cap-enforcement logic for live trading -- no I/O, no database, no
network. Every hard limit the live trading bot respects lives HERE and only
here, so there's exactly one place to audit before ever connecting real
money: app.trade_watcher calls check_caps() before every order and refuses
to submit unless it returns allow=True.

Deliberately more caps than just "max daily loss": a realized-loss cap alone
doesn't bound exposure, since prediction-market positions mostly don't
resolve same-day -- you could open a lot of exposure today, realize $0 loss
today (the cap never trips), and lose it all next week. max_daily_spend_usd
(dollars that actually left the wallet today) and max_open_exposure_usd
(dollars currently at risk across every open position) are the real bounds
on that; max_daily_loss_usd is the right auto-halt trigger on top, not a
substitute for the other two.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RiskDecision:
    allow: bool
    reason: Optional[str] = None  # None iff allow is True; see check_caps' docstring for the reason vocabulary


def check_caps(settings: dict, ledger: dict, open_positions: list, candidate: dict) -> RiskDecision:
    """
    settings: a trading_settings row as a dict -- enabled, max_stake_usd,
        max_open_positions, max_open_exposure_usd, max_position_per_market_usd,
        max_daily_loss_usd, max_daily_spend_usd, max_trades_per_day.
    ledger: today's trading_day_ledger row as a dict -- realized_pnl_usd,
        spent_usd, n_trades, halted (the auto-halt flag; a SEPARATE concept
        from settings['enabled'], the user's own sticky kill switch -- see
        db.py's trading_day_ledger docstring for why they're kept apart).
    open_positions: currently-open trades (status not yet resolved/closed),
        each a dict with at least 'condition_id' and 'cost_usd' (what
        actually left the wallet for it, or requested_stake_usd if a pending
        order's true cost isn't known yet -- a pending order still commits
        the money for exposure/cap purposes).
    candidate: the proposed trade -- a dict with 'condition_id' and
        'stake_usd'.

    Returns the FIRST cap that would be breached, most fundamental first
    (paused/halted before anything money-shaped, dedupe before capacity,
    capacity before exposure/spend, per-trade size before aggregate size --
    each check is independent, so which one fires first is just which is
    checked first, not a priority ranking of "worse"). Reasons:
        'paused'                -- settings['enabled'] is False
        'halted'                -- today's auto-halt (max_daily_loss_usd already breached)
        'already_open'          -- an open position already exists for this condition_id
        'cap_open_positions'    -- max_open_positions already reached
        'stake_exceeds_max'     -- candidate stake alone exceeds max_stake_usd
        'stake_exceeds_market_cap' -- candidate stake exceeds max_position_per_market_usd
        'cap_open_exposure'     -- open exposure + candidate stake would exceed max_open_exposure_usd
        'cap_daily_spend'       -- today's spend + candidate stake would exceed max_daily_spend_usd
        'cap_daily_trades'      -- today's trade count already at max_trades_per_day
    """
    if not settings.get("enabled", False):
        return RiskDecision(False, "paused")
    if ledger.get("halted", False):
        return RiskDecision(False, "halted")

    condition_id = candidate["condition_id"]
    stake = float(candidate["stake_usd"])

    if any(p["condition_id"] == condition_id for p in open_positions):
        return RiskDecision(False, "already_open")

    if len(open_positions) >= settings["max_open_positions"]:
        return RiskDecision(False, "cap_open_positions")

    if stake > settings["max_stake_usd"]:
        return RiskDecision(False, "stake_exceeds_max")

    if stake > settings["max_position_per_market_usd"]:
        return RiskDecision(False, "stake_exceeds_market_cap")

    open_exposure = sum(float(p.get("cost_usd") or p.get("requested_stake_usd") or 0.0) for p in open_positions)
    if open_exposure + stake > settings["max_open_exposure_usd"]:
        return RiskDecision(False, "cap_open_exposure")

    if float(ledger.get("spent_usd", 0.0)) + stake > settings["max_daily_spend_usd"]:
        return RiskDecision(False, "cap_daily_spend")

    if int(ledger.get("n_trades", 0)) >= settings["max_trades_per_day"]:
        return RiskDecision(False, "cap_daily_trades")

    return RiskDecision(True, None)


def should_halt_for_daily_loss(ledger: dict, settings: dict) -> bool:
    """True if today's realized loss has crossed max_daily_loss_usd -- the
    trade_watcher calls this after each reconciliation pass and, if True,
    sets trading_day_ledger.halted (an auto-halt distinct from the user's
    own settings['enabled'] kill switch -- see check_caps' docstring)."""
    return float(ledger.get("realized_pnl_usd", 0.0)) <= -abs(float(settings["max_daily_loss_usd"]))
