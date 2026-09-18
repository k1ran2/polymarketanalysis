"""
Unit tests for app.risk -- the single place every live-trading hard limit
lives. Pure functions, no I/O -- every cap and every boundary value is
checked directly, no mocking needed. This is the highest-value test file in
the trading feature: a bug here means real money either gets over-exposed
or a legitimate trade gets wrongly blocked.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.risk import RiskDecision, check_caps, should_halt_for_daily_loss

DEFAULT_SETTINGS = {
    "enabled": True,
    "max_stake_usd": 1.0,
    "max_open_positions": 3,
    "max_open_exposure_usd": 5.0,
    "max_position_per_market_usd": 1.0,
    "max_daily_loss_usd": 10.0,
    "max_daily_spend_usd": 25.0,
    "max_trades_per_day": 5,
}

DEFAULT_LEDGER = {"realized_pnl_usd": 0.0, "spent_usd": 0.0, "n_trades": 0, "halted": False}

CANDIDATE = {"condition_id": "c-new", "stake_usd": 1.0}


def test_allows_a_trade_within_every_cap():
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, [], CANDIDATE)
    assert d == RiskDecision(True, None)


def test_blocks_when_paused():
    settings = {**DEFAULT_SETTINGS, "enabled": False}
    d = check_caps(settings, DEFAULT_LEDGER, [], CANDIDATE)
    assert d == RiskDecision(False, "paused")


def test_blocks_when_halted_even_if_enabled():
    ledger = {**DEFAULT_LEDGER, "halted": True}
    d = check_caps(DEFAULT_SETTINGS, ledger, [], CANDIDATE)
    assert d == RiskDecision(False, "halted")


def test_blocks_a_second_position_in_the_same_market():
    open_positions = [{"condition_id": "c-new", "cost_usd": 1.0}]
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, open_positions, CANDIDATE)
    assert d == RiskDecision(False, "already_open")


def test_allows_a_different_market_when_under_position_cap():
    open_positions = [{"condition_id": "c-other", "cost_usd": 1.0}]
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, open_positions, CANDIDATE)
    assert d.allow is True


def test_blocks_at_max_open_positions():
    open_positions = [{"condition_id": f"c{i}", "cost_usd": 1.0} for i in range(3)]  # == max_open_positions
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, open_positions, CANDIDATE)
    assert d == RiskDecision(False, "cap_open_positions")


def test_allows_just_under_max_open_positions():
    open_positions = [{"condition_id": f"c{i}", "cost_usd": 1.0} for i in range(2)]
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, open_positions, CANDIDATE)
    assert d.allow is True


def test_blocks_stake_exceeding_max_stake_usd():
    candidate = {"condition_id": "c-new", "stake_usd": 1.01}
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, [], candidate)
    assert d == RiskDecision(False, "stake_exceeds_max")


def test_allows_stake_exactly_at_max_stake_usd():
    candidate = {"condition_id": "c-new", "stake_usd": 1.0}  # boundary is inclusive
    d = check_caps(DEFAULT_SETTINGS, DEFAULT_LEDGER, [], candidate)
    assert d.allow is True


def test_blocks_stake_exceeding_per_market_cap_even_if_under_max_stake():
    settings = {**DEFAULT_SETTINGS, "max_stake_usd": 10.0, "max_position_per_market_usd": 2.0}
    candidate = {"condition_id": "c-new", "stake_usd": 3.0}
    d = check_caps(settings, DEFAULT_LEDGER, [], candidate)
    assert d == RiskDecision(False, "stake_exceeds_market_cap")


def test_blocks_when_open_exposure_plus_candidate_exceeds_cap():
    settings = {**DEFAULT_SETTINGS, "max_stake_usd": 10.0, "max_position_per_market_usd": 10.0, "max_open_exposure_usd": 5.0}
    open_positions = [{"condition_id": "c-a", "cost_usd": 4.5}]
    candidate = {"condition_id": "c-new", "stake_usd": 1.0}  # 4.5 + 1.0 > 5.0
    d = check_caps(settings, DEFAULT_LEDGER, open_positions, candidate)
    assert d == RiskDecision(False, "cap_open_exposure")


def test_open_exposure_falls_back_to_requested_stake_when_cost_unknown():
    """A pending order (not yet filled) has no cost_usd yet, but its stake is
    already committed money for exposure purposes -- must still count."""
    settings = {**DEFAULT_SETTINGS, "max_stake_usd": 10.0, "max_position_per_market_usd": 10.0, "max_open_exposure_usd": 5.0}
    open_positions = [{"condition_id": "c-a", "cost_usd": None, "requested_stake_usd": 4.5}]
    candidate = {"condition_id": "c-new", "stake_usd": 1.0}
    d = check_caps(settings, DEFAULT_LEDGER, open_positions, candidate)
    assert d == RiskDecision(False, "cap_open_exposure")


def test_allows_open_exposure_exactly_at_cap_boundary():
    settings = {**DEFAULT_SETTINGS, "max_stake_usd": 10.0, "max_position_per_market_usd": 10.0, "max_open_exposure_usd": 5.0}
    open_positions = [{"condition_id": "c-a", "cost_usd": 4.0}]
    candidate = {"condition_id": "c-new", "stake_usd": 1.0}  # 4.0 + 1.0 == 5.0, inclusive
    d = check_caps(settings, DEFAULT_LEDGER, open_positions, candidate)
    assert d.allow is True


def test_blocks_when_daily_spend_cap_would_be_exceeded():
    settings = {**DEFAULT_SETTINGS, "max_stake_usd": 10.0, "max_position_per_market_usd": 10.0, "max_open_exposure_usd": 10.0, "max_daily_spend_usd": 5.0}
    ledger = {**DEFAULT_LEDGER, "spent_usd": 4.5}
    candidate = {"condition_id": "c-new", "stake_usd": 1.0}
    d = check_caps(settings, ledger, [], candidate)
    assert d == RiskDecision(False, "cap_daily_spend")


def test_blocks_at_max_daily_trade_count():
    settings = {**DEFAULT_SETTINGS, "max_trades_per_day": 5}
    ledger = {**DEFAULT_LEDGER, "n_trades": 5}
    d = check_caps(settings, ledger, [], CANDIDATE)
    assert d == RiskDecision(False, "cap_daily_trades")


def test_allows_just_under_max_daily_trade_count():
    settings = {**DEFAULT_SETTINGS, "max_trades_per_day": 5}
    ledger = {**DEFAULT_LEDGER, "n_trades": 4}
    d = check_caps(settings, ledger, [], CANDIDATE)
    assert d.allow is True


def test_paused_takes_priority_over_every_other_breach():
    """Even if every other cap is ALSO breached, 'paused' must be the
    reported reason -- it's checked first and is the most fundamental."""
    settings = {**DEFAULT_SETTINGS, "enabled": False, "max_stake_usd": 0.0, "max_open_positions": 0}
    d = check_caps(settings, DEFAULT_LEDGER, [], CANDIDATE)
    assert d == RiskDecision(False, "paused")


# ---------------------------------------------------------------------------
# should_halt_for_daily_loss -- the auto-halt trigger
# ---------------------------------------------------------------------------

def test_should_halt_when_loss_exceeds_cap():
    ledger = {"realized_pnl_usd": -10.01}
    assert should_halt_for_daily_loss(ledger, DEFAULT_SETTINGS) is True


def test_should_halt_at_exact_loss_boundary():
    ledger = {"realized_pnl_usd": -10.0}  # == max_daily_loss_usd, inclusive
    assert should_halt_for_daily_loss(ledger, DEFAULT_SETTINGS) is True


def test_should_not_halt_when_loss_under_cap():
    ledger = {"realized_pnl_usd": -9.99}
    assert should_halt_for_daily_loss(ledger, DEFAULT_SETTINGS) is False


def test_should_not_halt_on_a_profitable_day():
    ledger = {"realized_pnl_usd": 50.0}
    assert should_halt_for_daily_loss(ledger, DEFAULT_SETTINGS) is False
