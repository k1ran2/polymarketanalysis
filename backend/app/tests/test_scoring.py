"""
Unit tests for app/scoring.py against hand-computed fixtures (§10 guardrail:
"Unit-test the §3 formulas against hand-computed fixtures before wiring up
any UI.")
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.scoring import (
    ClosedPosition, parse_closed_position, is_resolved_outcome, outcome_value,
    trader_raw_stats, confidence_tier, bronze_weight,
    composite_and_percentile, compute_trend, split_positions_by_lookback,
    fade_pool_eligible, smart_pool_eligible, holder_dumb_pool_eligible, TrendResult, ScoreResult,
    Holder, usd_weighted_score, net_smart_flow, smart_implied_probability,
    undervalued_gap, signed_distribution_stats, whale_concentration,
    gini_coefficient, overall_market_verdict, tenure_days, usd_weighted_tenure_days,
)
from app import config as cfg

NOW = 1_700_000_000  # fixed "now" for deterministic tests


def cp(wallet="0xabc", condition_id="c1", avg_price=0.5, total_bought=100.0,
       realized_pnl=0.0, cur_price=1.0, timestamp=NOW):
    return ClosedPosition(wallet, condition_id, avg_price, total_bought,
                           realized_pnl, cur_price, timestamp)


# ---------------------------------------------------------------------------
# §3.1 parsing / resolution / return
# ---------------------------------------------------------------------------

def test_parse_closed_position_valid():
    raw = {
        "proxyWallet": "0xabc", "conditionId": "0xdef", "avgPrice": 0.4,
        "totalBought": 1000.0, "realizedPnl": 200.0, "curPrice": 1.0,
        "timestamp": 1700000000,
    }
    pos = parse_closed_position(raw)
    assert pos is not None
    assert pos.wallet == "0xabc"
    assert math.isclose(pos.r, 0.2)


def test_parse_closed_position_malformed_missing_field():
    raw = {"proxyWallet": "0xabc", "conditionId": "0xdef"}  # missing required fields
    assert parse_closed_position(raw) is None


def test_parse_closed_position_zero_total_bought_rejected():
    raw = {
        "proxyWallet": "0xabc", "conditionId": "0xdef", "avgPrice": 0.4,
        "totalBought": 0.0, "realizedPnl": 0.0, "curPrice": 1.0, "timestamp": 1,
    }
    assert parse_closed_position(raw) is None


def test_is_resolved_outcome():
    assert is_resolved_outcome(cp(cur_price=1.0)) is True
    assert is_resolved_outcome(cp(cur_price=0.0)) is True
    assert is_resolved_outcome(cp(cur_price=0.995)) is True
    assert is_resolved_outcome(cp(cur_price=0.63)) is False  # early-sale exit
    assert is_resolved_outcome(cp(cur_price=0.5)) is False   # cancelled/disputed refund


def test_outcome_value():
    assert outcome_value(cp(cur_price=1.0)) == 1.0
    assert outcome_value(cp(cur_price=0.0)) == 0.0


# ---------------------------------------------------------------------------
# §3.1 trader_raw_stats — hand-computed fixture
# ---------------------------------------------------------------------------

def test_trader_raw_stats_hand_computed():
    # 5 positions, hand-computed by hand below.
    positions = [
        cp(total_bought=100, realized_pnl=50, cur_price=1.0, avg_price=0.4),   # r=0.5,  win, resolved, calib err=(0.4-1)^2=0.36
        cp(total_bought=200, realized_pnl=-100, cur_price=0.0, avg_price=0.6), # r=-0.5, loss, resolved, calib err=(0.6-0)^2=0.36
        cp(total_bought=100, realized_pnl=20, cur_price=1.0, avg_price=0.7),   # r=0.2,  win, resolved, calib err=(0.7-1)^2=0.09
        cp(total_bought=50,  realized_pnl=-25, cur_price=0.63, avg_price=0.5), # r=-0.5, loss, EARLY EXIT (not resolved) -> excluded from brier
        cp(total_bought=150, realized_pnl=15, cur_price=1.0, avg_price=0.9),   # r=0.1,  win, resolved, calib err=(0.9-1)^2=0.01
    ]
    stats = trader_raw_stats("0xabc", positions, now_ts=NOW)

    assert stats.n == 5
    assert math.isclose(stats.volume, 100 + 200 + 100 + 50 + 150)  # 600

    # win_rate_weighted = (sum totalBought where r>0) / volume
    # winners: 100 (r=0.5), 100 (r=0.2), 150 (r=0.1) => 350 / 600
    assert math.isclose(stats.win_rate_weighted, 350 / 600)

    # mean_return over the 5 r values: 0.5, -0.5, 0.2, -0.5, 0.1 -> mean = -0.2/5 = -0.04
    returns = [0.5, -0.5, 0.2, -0.5, 0.1]
    expected_mean = sum(returns) / 5
    assert math.isclose(stats.mean_return, expected_mean)

    # population stdev of returns
    var = sum((r - expected_mean) ** 2 for r in returns) / 5
    expected_std = math.sqrt(var)
    assert math.isclose(stats.stdev_return, expected_std)
    assert math.isclose(stats.sharpe_like, expected_mean / expected_std)

    # brier over the 4 resolved-outcome positions only (excludes the 0.63 early exit)
    calib_errs = [0.36, 0.36, 0.09, 0.01]
    assert stats.n_calibration == 4
    assert math.isclose(stats.brier_score, sum(calib_errs) / 4)

    # n=5, volume=600 >= silver_min_volume(500) and n>=silver_min_n(15)? No (n=5<15) -> Bronze
    assert stats.tier == "Bronze"


def test_trader_raw_stats_unrated_below_n():
    positions = [cp() for _ in range(4)]
    stats = trader_raw_stats("0xabc", positions, now_ts=NOW)
    assert stats.tier == "Unrated"
    assert stats.sharpe_like is None  # n<5 gate


def test_confidence_tiers():
    # Bronze: n<15 or volume<500
    assert confidence_tier(n=10, volume=1000, last_active_ts=NOW, now_ts=NOW) == "Bronze"
    assert confidence_tier(n=20, volume=100, last_active_ts=NOW, now_ts=NOW) == "Bronze"
    # Silver: n>=15 and volume>=500, but not meeting Gold
    assert confidence_tier(n=15, volume=500, last_active_ts=NOW, now_ts=NOW) == "Silver"
    # Gold: n>=50, volume>=5000, active in last 90 days
    assert confidence_tier(n=50, volume=5000, last_active_ts=NOW, now_ts=NOW) == "Gold"
    # Gold-eligible by n/volume but stale (>90 days) -> Silver
    stale_ts = NOW - 91 * 86400
    assert confidence_tier(n=50, volume=5000, last_active_ts=stale_ts, now_ts=NOW) == "Silver"
    # Unrated
    assert confidence_tier(n=3, volume=10000, last_active_ts=NOW, now_ts=NOW) == "Unrated"


def test_bronze_weight():
    assert math.isclose(bronze_weight(15), 0.5)
    assert math.isclose(bronze_weight(30), 1.0)
    assert math.isclose(bronze_weight(60), 1.0)  # capped at 1


# ---------------------------------------------------------------------------
# §3.2 Smart Score — per-wallet fixed curve (no population percentile)
# ---------------------------------------------------------------------------

def _stats(wallet, sharpe, mean_ret, winrate, brier, n=40, tier="Silver"):
    from app.scoring import TraderRawStats
    return TraderRawStats(
        wallet=wallet, n=n, volume=5000, win_rate_weighted=winrate,
        mean_return=mean_ret, stdev_return=0.1, sharpe_like=sharpe,
        brier_score=brier, n_calibration=n, last_active_ts=NOW, tier=tier,
    )


def test_smart_score_ranks_better_trader_higher():
    from app.scoring import smart_score_from_raw
    best = _stats("best", sharpe=2.0, mean_ret=0.5, winrate=0.9, brier=0.05)
    mid = _stats("mid", sharpe=0.3, mean_ret=0.05, winrate=0.55, brier=0.22)
    worst = _stats("worst", sharpe=-1.0, mean_ret=-0.3, winrate=0.25, brier=0.4)
    b, m, w = (smart_score_from_raw(x) for x in (best, mid, worst))
    assert 0 <= w < m < b <= 100
    assert b > 70          # a strong, consistent, well-calibrated record scores high
    assert w < 30          # a losing, poorly-calibrated record scores low


def test_smart_score_is_computed_per_wallet_independently():
    # A wallet's score depends only on its own stats -- adding/removing other
    # wallets from the batch must not change it.
    from app.scoring import composite_and_percentile
    a = _stats("a", sharpe=1.0, mean_ret=0.2, winrate=0.6, brier=0.12)
    solo = composite_and_percentile({"a": a})["a"].percentile
    with_others = composite_and_percentile({
        "a": a,
        "b": _stats("b", sharpe=5.0, mean_ret=0.9, winrate=0.99, brier=0.01),
        "c": _stats("c", sharpe=-3.0, mean_ret=-0.8, winrate=0.1, brier=0.6),
    })["a"].percentile
    assert solo == with_others


def test_smart_score_small_sample_shrinks_toward_50():
    from app.scoring import smart_score_from_raw
    great = dict(sharpe=6.0, mean_ret=0.6, winrate=0.95, brier=0.01)
    thin = smart_score_from_raw(_stats("thin", n=6, **great))
    thick = smart_score_from_raw(_stats("thick", n=400, **great))
    assert thin is not None and thick is not None
    assert 50 < thin < 65        # 6 bets: barely above neutral even when perfect
    assert thick > 90            # 400 bets: the record is trusted
    assert thick > thin


def test_smart_score_none_below_min_n():
    from app.scoring import smart_score_from_raw, TraderRawStats
    unrated = TraderRawStats(
        wallet="u", n=3, volume=10, win_rate_weighted=1.0, mean_return=0.5,
        stdev_return=0.1, sharpe_like=5.0, brier_score=0.01, n_calibration=3,
        last_active_ts=NOW, tier="Unrated",
    )
    assert smart_score_from_raw(unrated) is None


def test_composite_and_percentile_unrated_excluded():
    from app.scoring import TraderRawStats
    pop = {
        "rated": _stats("rated", sharpe=1.0, mean_ret=0.2, winrate=0.6, brier=0.1),
        "unrated": TraderRawStats(
            wallet="unrated", n=2, volume=10, win_rate_weighted=None,
            mean_return=None, stdev_return=None, sharpe_like=None,
            brier_score=None, n_calibration=0, last_active_ts=NOW, tier="Unrated",
        ),
    }
    results = composite_and_percentile(pop)
    assert results["unrated"].percentile is None
    assert results["unrated"].raw_composite is None
    assert results["rated"].percentile is not None


# ---------------------------------------------------------------------------
# §3.3 trend / fade pool gate
# ---------------------------------------------------------------------------

def test_trend_improving_excluded_from_fade_even_if_lifetime_bad():
    life = ScoreResult("w1", raw_composite=-1.0, percentile=20.0, tier="Silver")  # bad lifetime
    recent = ScoreResult("w1", raw_composite=1.0, percentile=70.0, tier="Silver")  # good recently
    trend = TrendResult("w1", life, recent, "Improving", 50.0)
    assert fade_pool_eligible(trend, dumb_threshold=40.0) is False  # spec: must NOT be included


def test_trend_declining_bad_trader_included_in_fade():
    life = ScoreResult("w2", raw_composite=-1.0, percentile=15.0, tier="Silver")
    recent = ScoreResult("w2", raw_composite=-1.2, percentile=10.0, tier="Silver")
    trend = TrendResult("w2", life, recent, "Declining", -5.0)
    assert fade_pool_eligible(trend, dumb_threshold=40.0) is True


def test_trend_stable_bad_trader_included_in_fade():
    life = ScoreResult("w3", raw_composite=-1.0, percentile=25.0, tier="Silver")
    trend = TrendResult("w3", life, None, "Insufficient recent data", None)
    assert fade_pool_eligible(trend, dumb_threshold=40.0) is True  # recent unavailable -> falls back to lifetime


def test_good_lifetime_trader_never_in_fade_pool():
    life = ScoreResult("w4", raw_composite=1.0, percentile=80.0, tier="Gold")
    trend = TrendResult("w4", life, None, "Insufficient recent data", None)
    assert fade_pool_eligible(trend, dumb_threshold=40.0) is False


def test_smart_pool_excludes_declining_when_flag_on():
    life = ScoreResult("w5", raw_composite=1.0, percentile=70.0, tier="Gold")
    trend = TrendResult("w5", life, None, "Declining", -20.0)
    fade_cfg_on = cfg.FadePoolConfig(exclude_improving_from_fade=True, exclude_declining_from_smart=True)
    assert smart_pool_eligible(trend, smart_threshold=60.0, fade_cfg=fade_cfg_on) is False
    fade_cfg_off = cfg.FadePoolConfig(exclude_improving_from_fade=True, exclude_declining_from_smart=False)
    assert smart_pool_eligible(trend, smart_threshold=60.0, fade_cfg=fade_cfg_off) is True


def test_split_positions_by_lookback():
    recent_pos = cp(timestamp=NOW - 10 * 86400)
    prior_pos = cp(timestamp=NOW - 200 * 86400)
    recent, prior = split_positions_by_lookback([recent_pos, prior_pos], NOW, lookback_days=90)
    assert recent == [recent_pos]
    assert prior == [prior_pos]


# ---------------------------------------------------------------------------
# §3.5 market-side aggregation — hand-computed fixture
# ---------------------------------------------------------------------------

def test_market_side_aggregation_hand_computed():
    holders = [
        Holder("w1", "YES", 1000.0, 80.0, "Gold"),   # smart
        Holder("w2", "YES", 500.0, 30.0, "Bronze"),  # dumb
        Holder("w3", "NO", 2000.0, 20.0, "Silver"),  # dumb
        Holder("w4", "NO", 100.0, None, "Unrated"),  # unrated -> $0 weight
    ]
    # usd_weighted_score(YES) = (80*1000 + 30*500) / (1000+500) = (80000+15000)/1500 = 63.333...
    yes_holders = [h for h in holders if h.side == "YES"]
    assert math.isclose(usd_weighted_score(yes_holders), (80 * 1000 + 30 * 500) / 1500)

    # net_smart_flow = yes_sum - no_sum
    # yes_sum = 1000*(80-50)/50 + 500*(30-50)/50 = 1000*0.6 + 500*(-0.4) = 600 - 200 = 400
    # no_sum  = 2000*(20-50)/50 = 2000*(-0.6) = -1200   (unrated w4 contributes 0)
    # net = 400 - (-1200) = 1600
    nsf = net_smart_flow(holders)
    assert math.isclose(nsf, 1600.0)

    # smart_implied_probability = yes_w / (yes_w+no_w), using max(score,1)
    # yes_w = 1000*80 + 500*30 = 80000+15000=95000
    # no_w  = 2000*20 = 40000  (unrated excluded)
    sip = smart_implied_probability(holders)
    assert math.isclose(sip, 95000 / (95000 + 40000))

    gap = undervalued_gap(sip, current_market_price=0.5)
    assert math.isclose(gap, sip - 0.5)


def test_bronze_holder_down_weighted_in_score_aggregates():
    # §3.4: a Bronze trader is "down-weighted by min(1, n/30) in aggregates".
    # Same $ position, same score; only the tier + trade count differ.
    from app.scoring import score_weight
    silver = Holder("s", "YES", 1000.0, 80.0, "Silver", n_trades=40)
    bronze = Holder("b", "YES", 1000.0, 80.0, "Bronze", n_trades=6)  # weight = 6/30 = 0.2
    assert math.isclose(score_weight(silver), 1000.0)
    assert math.isclose(score_weight(bronze), 200.0)

    # usd_weighted_score is score-invariant here (both score 80) but the Bronze
    # holder's pull on net_smart_flow should be 1/5 of the Silver's.
    nsf_silver_only = net_smart_flow([silver])
    nsf_bronze_only = net_smart_flow([bronze])
    assert math.isclose(nsf_bronze_only, nsf_silver_only * 0.2)

    # A Bronze holder with no recorded trade count is left at full weight
    # (nothing to down-weight by) rather than being silently zeroed.
    bronze_unknown_n = Holder("b2", "YES", 1000.0, 80.0, "Bronze")
    assert math.isclose(score_weight(bronze_unknown_n), 1000.0)


# ---------------------------------------------------------------------------
# §3.6 signed distribution stats
# ---------------------------------------------------------------------------

def test_signed_distribution_stats_symmetric_zero_mean():
    holders = [
        Holder("a", "YES", 100.0, 70.0, "Gold"),   # signed = +20
        Holder("b", "NO", 100.0, 70.0, "Gold"),    # signed = -20
    ]
    stats = signed_distribution_stats(holders)
    assert math.isclose(stats.weighted_mean, 0.0, abs_tol=1e-9)
    assert stats.n == 2


def test_signed_distribution_stats_skew_positive_lean_yes():
    holders = [
        Holder("a", "YES", 1000.0, 90.0, "Gold"),  # signed = +40, big weight
        Holder("b", "NO", 10.0, 90.0, "Gold"),      # signed = -40, tiny weight
    ]
    stats = signed_distribution_stats(holders)
    assert stats.weighted_mean > 0  # leans YES


# ---------------------------------------------------------------------------
# §3.7 whale concentration
# ---------------------------------------------------------------------------

def test_whale_concentration_buckets_hand_computed():
    # 10 holders, position_usd = 1000,900,...,100 (descending), total=5500
    holders = [Holder(f"w{i}", "YES", float(1000 - i * 100), 50.0, "Silver") for i in range(10)]
    wc = whale_concentration(holders)
    total = sum(h.position_usd for h in holders)
    assert math.isclose(total, 5500.0)
    # top1% of 10 -> ceil(0.1)=1 holder -> the $1000 holder
    top1_bucket = wc.buckets[0]
    assert top1_bucket.holder_count == 1
    assert math.isclose(top1_bucket.usd_share, 1000 / 5500)
    # buckets' holder counts should sum to n
    assert sum(b.holder_count for b in wc.buckets) == 10
    # usd shares should sum to ~1
    assert math.isclose(sum(b.usd_share for b in wc.buckets), 1.0)


def test_gini_coefficient_perfect_equality_is_zero():
    assert math.isclose(gini_coefficient([100.0] * 10), 0.0, abs_tol=1e-9)


def test_gini_coefficient_hand_computed():
    # values [1, 1, 1, 7] -> classic textbook-style example
    vals = [1.0, 1.0, 1.0, 7.0]
    n = 4
    total = 10.0
    # cum = sum((i+1)*v_sorted) for sorted ascending [1,1,1,7] -> 1*1+2*1+3*1+4*7 = 1+2+3+28=34
    cum = 1 * 1 + 2 * 1 + 3 * 1 + 4 * 7
    expected = (2 * cum - (n + 1) * total) / (n * total)
    assert math.isclose(gini_coefficient(vals), expected)


# ---------------------------------------------------------------------------
# §3.8 overall market verdict
# ---------------------------------------------------------------------------

def test_verdict_insufficient_data_when_mostly_unrated():
    holders = [
        Holder("w1", "YES", 100.0, 80.0, "Gold"),
        Holder("w2", "YES", 900.0, None, "Unrated"),
        Holder("w3", "NO", 500.0, None, "Unrated"),
    ]
    v = overall_market_verdict(holders, current_market_price=0.5)
    assert v.label == "Insufficient data"


def test_verdict_strong_buy_when_gap_large_and_smart_dominant():
    holders = [
        Holder(f"yes{i}", "YES", 1000.0, 85.0, "Gold") for i in range(5)
    ] + [
        Holder(f"no{i}", "NO", 200.0, 45.0, "Bronze") for i in range(3)
    ]
    v = overall_market_verdict(holders, current_market_price=0.3)
    assert v.label.startswith("Strong Buy YES")
    assert v.conviction > 0


def test_verdict_fade_when_dumb_money_dominates_one_side():
    holders = [
        Holder(f"no{i}", "NO", 1000.0, 20.0, "Bronze") for i in range(6)
    ] + [
        Holder("yes1", "YES", 100.0, 55.0, "Silver"),
    ]
    v = overall_market_verdict(holders, current_market_price=0.5)
    assert "Fade NO" in v.label


# ---------------------------------------------------------------------------
# holder_dumb_pool_eligible / §3.3 fade-pool trend gate wired into
# overall_market_verdict's dumb_usd -- a low lifetime score alone used to be
# enough to count as dumb money; it no longer is if the trader is trending
# "Improving" or has already recovered on their recent-window score. A
# holder with no trend data (trend=None -- every backtest holder, since the
# backtest engine doesn't compute a recent-vs-lifetime window per checkpoint)
# must fall back to the old lifetime-only behavior exactly, unfiltered.
# ---------------------------------------------------------------------------

def test_dumb_pool_eligible_excludes_improving_trend_by_default():
    h = Holder("w", "NO", 1000.0, 20.0, "Bronze", trend="Improving")
    assert holder_dumb_pool_eligible(h, dumb_threshold=40.0) is False


def test_dumb_pool_eligible_excludes_recovered_recent_score():
    h = Holder("w", "NO", 1000.0, 20.0, "Bronze", trend="Stable", recent_percentile=50.0)
    assert holder_dumb_pool_eligible(h, dumb_threshold=40.0) is False


def test_dumb_pool_eligible_includes_declining_or_stable_low_scorer():
    h = Holder("w", "NO", 1000.0, 20.0, "Bronze", trend="Declining")
    assert holder_dumb_pool_eligible(h, dumb_threshold=40.0) is True


def test_dumb_pool_eligible_with_no_trend_data_falls_back_to_lifetime_only():
    """The backtest engine never sets trend/recent_percentile -- this must
    behave exactly as it did before the fade-pool rule existed, i.e. purely
    on score_percentile, so wiring this in doesn't silently change any past
    backtest result."""
    dumb = Holder("w1", "NO", 1000.0, 20.0, "Bronze")
    not_dumb = Holder("w2", "NO", 1000.0, 60.0, "Gold")
    assert holder_dumb_pool_eligible(dumb, dumb_threshold=40.0) is True
    assert holder_dumb_pool_eligible(not_dumb, dumb_threshold=40.0) is False


def test_verdict_no_longer_fades_an_improving_trader_even_with_low_lifetime_score():
    """Same shape as test_verdict_fade_when_dumb_money_dominates_one_side,
    but every low-scoring NO holder is trending Improving -- none of them
    should count as dumb money now, so NO's dumb_volume_share collapses and
    the market must not fire "Fade NO" anymore."""
    holders = [
        Holder(f"no{i}", "NO", 1000.0, 20.0, "Bronze", trend="Improving") for i in range(6)
    ] + [
        Holder("yes1", "YES", 100.0, 55.0, "Silver"),
    ]
    v = overall_market_verdict(holders, current_market_price=0.5)
    assert "Fade NO" not in v.label
    assert v.dumb_volume_share == 0.0


# ---------------------------------------------------------------------------
# Wallet tenure -- earliest observed trade, and its $-weighted side average
# ---------------------------------------------------------------------------

def test_trader_raw_stats_tracks_first_active_ts():
    positions = [cp(timestamp=NOW - 200 * 86400), cp(timestamp=NOW - 10 * 86400), cp(timestamp=NOW)]
    stats = trader_raw_stats("0xabc", positions, now_ts=NOW)
    assert stats.first_active_ts == NOW - 200 * 86400
    assert stats.last_active_ts == NOW


def test_tenure_days_none_when_unknown():
    h = Holder("w", "YES", 100.0, None, "Unrated", first_active_ts=None)
    assert tenure_days(h, NOW) is None


def test_tenure_days_computed_from_first_active_ts():
    h = Holder("w", "YES", 100.0, 80.0, "Gold", first_active_ts=NOW - 30 * 86400)
    assert math.isclose(tenure_days(h, NOW), 30.0)


def test_usd_weighted_tenure_days_weights_by_position_size():
    holders = [
        Holder("old", "YES", 900.0, 80.0, "Gold", first_active_ts=NOW - 300 * 86400),
        Holder("new", "YES", 100.0, 80.0, "Gold", first_active_ts=NOW - 3 * 86400),
        Holder("unknown_age", "NO", 500.0, 40.0, "Silver", first_active_ts=None),
    ]
    avg = usd_weighted_tenure_days(holders, NOW)
    expected = (900 * 300 + 100 * 3) / 1000
    assert math.isclose(avg, expected)


def test_usd_weighted_tenure_days_none_when_all_unknown():
    holders = [Holder("w", "YES", 100.0, 80.0, "Gold", first_active_ts=None)]
    assert usd_weighted_tenure_days(holders, NOW) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
