"""
Pure scoring functions implementing spec §3 (Metrics).

Design principle: every function here is a pure function of its inputs (plus
the config values passed in) — no I/O, no globals, no hidden state. This is
what makes them unit-testable against hand-computed fixtures (§10 guardrail)
and safe to call from both the backfill script and any future recompute job.

Two-stage pipeline for the composite score (§3.2):
  1. `trader_raw_stats(positions, ...)` turns one trader's closed positions
     into raw stats (n, volume, sharpe_like, mean_return, win_rate, brier).
  2. `composite_and_percentile(raw_stats_by_wallet, ...)` is a *population*
     function: it z-scores each metric across all Bronze+ traders, forms the
     weighted composite, then converts to a percentile rank among Silver+Gold
     traders. This is why it takes a dict of every trader's raw stats rather
     than being callable per-trader in isolation — the percentile is only
     meaningful relative to the population.

Trend (§3.3) re-runs this whole two-stage pipeline twice: once over each
trader's full history ("lifetime") and once over only the recent window
("recent"), each with its own population of z-scores/percentiles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Iterable, Optional

from . import config as cfg


# ---------------------------------------------------------------------------
# §3.1 Trader stats — per-position and per-trader raw stats
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClosedPosition:
    wallet: str
    condition_id: str
    avg_price: float       # entry price / implied probability
    total_bought: float    # $ volume put into this position
    realized_pnl: float
    cur_price: float       # settlement price (~0 or ~1 if resolved binary)
    timestamp: int         # unix seconds

    @property
    def r(self) -> Optional[float]:
        """Return realizedPnl / totalBought. None if totalBought is ~0 (malformed)."""
        if self.total_bought is None or abs(self.total_bought) < 1e-9:
            return None
        return self.realized_pnl / self.total_bought


def parse_closed_position(raw: dict) -> Optional[ClosedPosition]:
    """
    Parse one raw /closed-positions record into a ClosedPosition, or None if
    it's malformed (§10: "validate/skip malformed records rather than
    crashing a job").
    """
    try:
        wallet = raw["proxyWallet"]
        condition_id = raw["conditionId"]
        avg_price = float(raw["avgPrice"])
        total_bought = float(raw["totalBought"])
        realized_pnl = float(raw["realizedPnl"])
        cur_price = float(raw["curPrice"])
        timestamp = int(raw["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if total_bought <= 0:
        return None  # dust / malformed — can't compute a return
    if not (0.0 <= avg_price <= 1.0):
        return None
    return ClosedPosition(
        wallet=wallet,
        condition_id=condition_id,
        avg_price=avg_price,
        total_bought=total_bought,
        realized_pnl=realized_pnl,
        cur_price=cur_price,
        timestamp=timestamp,
    )


def is_resolved_outcome(pos: ClosedPosition, eps: float = 0.02) -> bool:
    """
    True if curPrice settled to ~0 or ~1, i.e. this was a genuine binary
    resolution (not an early-sale exit, and not a cancelled/disputed market
    that refunds at some other price like 0.5). Used to gate brier_score
    (§3.1: "counts toward calibration only if curPrice resolved to ~0 or 1").
    """
    return pos.cur_price <= eps or pos.cur_price >= (1.0 - eps)


def outcome_value(pos: ClosedPosition) -> float:
    """1.0 if the position resolved YES-side-true (curPrice~1), else 0.0."""
    return 1.0 if pos.cur_price >= 0.5 else 0.0


@dataclass(frozen=True)
class TraderRawStats:
    wallet: str
    n: int
    volume: float
    win_rate_weighted: Optional[float]
    mean_return: Optional[float]
    stdev_return: Optional[float]
    sharpe_like: Optional[float]
    brier_score: Optional[float]
    n_calibration: int          # number of positions used for brier_score
    last_active_ts: Optional[int]
    first_active_ts: Optional[int] = None  # earliest position we fetched -- see tenure_days()
    tier: str = "Unrated"


def confidence_tier(
    n: int,
    volume: float,
    last_active_ts: Optional[int],
    now_ts: int,
    tiers: cfg.ConfidenceTierConfig = cfg.TIERS,
) -> str:
    """§3.4 confidence tiers."""
    if n < tiers.unrated_max_n:
        return "Unrated"
    is_silver_eligible = n >= tiers.silver_min_n and volume >= tiers.silver_min_volume
    if is_silver_eligible:
        active_recently = (
            last_active_ts is not None
            and (now_ts - last_active_ts) <= tiers.gold_active_days * 86400
        )
        if n >= tiers.gold_min_n and volume >= tiers.gold_min_volume and active_recently:
            return "Gold"
        return "Silver"
    return "Bronze"


def bronze_weight(n: int, tiers: cfg.ConfidenceTierConfig = cfg.TIERS) -> float:
    """Down-weight factor for Bronze traders in aggregates: min(1, n/30)."""
    return min(1.0, n / tiers.bronze_weight_cap_denominator)


def trader_raw_stats(
    wallet: str,
    positions: Iterable[ClosedPosition],
    now_ts: int,
    tiers: cfg.ConfidenceTierConfig = cfg.TIERS,
) -> TraderRawStats:
    """§3.1: turn one trader's closed positions into raw stats."""
    positions = list(positions)
    n = len(positions)
    volume = sum(p.total_bought for p in positions)
    last_active_ts = max((p.timestamp for p in positions), default=None)
    # Earliest position *we fetched* -- a lower bound on how long this wallet
    # has traded on Polymarket, not a hard fact (see BACKFILL.max_closed_
    # positions_per_wallet: a very active wallet's true first trade can be
    # older than anything in our capped, newest-first page).
    first_active_ts = min((p.timestamp for p in positions), default=None)
    tier = confidence_tier(n, volume, last_active_ts, now_ts, tiers)

    if n == 0 or volume <= 0:
        return TraderRawStats(
            wallet=wallet, n=n, volume=volume, win_rate_weighted=None,
            mean_return=None, stdev_return=None, sharpe_like=None,
            brier_score=None, n_calibration=0, last_active_ts=last_active_ts,
            first_active_ts=first_active_ts, tier=tier,
        )

    returns = [p.r for p in positions if p.r is not None]
    win_volume = sum(p.total_bought for p in positions if p.r is not None and p.r > 0)
    win_rate_weighted = win_volume / volume if volume > 0 else None

    mean_return = mean(returns) if returns else None
    stdev_return = pstdev(returns) if len(returns) >= 2 else None
    sharpe_like: Optional[float] = None
    if n >= 5 and mean_return is not None and stdev_return is not None and stdev_return > 1e-9:
        sharpe_like = mean_return / stdev_return

    calibration_positions = [p for p in positions if is_resolved_outcome(p)]
    n_calibration = len(calibration_positions)
    brier_score: Optional[float] = None
    if n_calibration > 0:
        brier_score = mean(
            (p.avg_price - outcome_value(p)) ** 2 for p in calibration_positions
        )

    return TraderRawStats(
        wallet=wallet, n=n, volume=volume, win_rate_weighted=win_rate_weighted,
        mean_return=mean_return, stdev_return=stdev_return, sharpe_like=sharpe_like,
        brier_score=brier_score, n_calibration=n_calibration,
        last_active_ts=last_active_ts, first_active_ts=first_active_ts, tier=tier,
    )


# ---------------------------------------------------------------------------
# §3.2 Smart Score (0-100) — per-wallet, fixed curve (no population percentile)
# ---------------------------------------------------------------------------
#
# Deliberate simplification of the spec: instead of z-scoring across the trader
# population and taking a percentile rank, each wallet is scored in isolation
# from its own resolved history via a fixed mapping. Consequences:
#   + a wallet's score is stable and reproducible -- it doesn't drift as other
#     traders enter the pool
#   + scoring one wallet needs only that wallet's data, so the on-demand
#     "pick a market, score its holders now" flow doesn't have to load and
#     re-rank everyone on every market open
#   - "72" now means "this track record maps to 72 on our skill curve", not
#     "better than 72% of tracked traders". The curve constants live in
#     cfg.SmartScoreParams and are the thing to tune.

@dataclass(frozen=True)
class ScoreResult:
    wallet: str
    raw_composite: Optional[float]   # same value as `score` (kept for the DB column)
    percentile: Optional[float]      # the 0-100 Smart Score; None if too little history
    tier: str


def _logistic(x: float, scale: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x / scale))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def smart_score_from_raw(
    raw: TraderRawStats,
    params: cfg.SmartScoreParams = cfg.SCORE,
) -> Optional[float]:
    """
    Per-wallet Smart Score (0-100) from one trader's raw stats. None if the
    trader has too little resolved history to score (Unrated / n < min_n).

    Each available sub-metric -> 0-1 via a fixed curve, weight-averaged over
    the metrics present, scaled to 0-100, then shrunk toward a neutral 50 by
    sample size.
    """
    if raw.tier == "Unrated" or raw.n < params.min_n:
        return None

    comps: list = []  # (weight, value in 0-1)
    if raw.mean_return is not None:
        comps.append((params.w_return, _logistic(raw.mean_return, params.return_scale)))
    if raw.win_rate_weighted is not None:
        span = params.winrate_ceil - params.winrate_floor
        comps.append((params.w_winrate, _clamp01((raw.win_rate_weighted - params.winrate_floor) / span)))
    if raw.sharpe_like is not None:
        comps.append((params.w_sharpe, _logistic(raw.sharpe_like, params.sharpe_scale)))
    if raw.brier_score is not None:
        span = params.brier_worst - params.brier_best
        comps.append((params.w_brier, _clamp01((params.brier_worst - raw.brier_score) / span)))

    if not comps:
        return None

    wsum = sum(w for w, _ in comps)
    raw_0_1 = sum(w * v for w, v in comps) / wsum
    score = 100.0 * raw_0_1

    shrink = raw.n / (raw.n + params.shrink_k)
    return round(50.0 + (score - 50.0) * shrink, 2)


def composite_and_percentile(
    raw_stats_by_wallet: dict,
    params: cfg.SmartScoreParams = cfg.SCORE,
) -> dict:
    """
    Score every wallet in `raw_stats_by_wallet` independently.

    raw_stats_by_wallet: wallet -> TraderRawStats
    returns: wallet -> ScoreResult (Unrated / too-thin wallets get
             raw_composite=None, percentile=None).

    Name kept for backwards compatibility with callers; there is no longer a
    percentile or a population step.
    """
    results = {}
    for w, s in raw_stats_by_wallet.items():
        sc = smart_score_from_raw(s, params)
        results[w] = ScoreResult(wallet=w, raw_composite=sc, percentile=sc, tier=s.tier)
    return results


# ---------------------------------------------------------------------------
# §3.3 Trend (improving vs declining) — gates the fade pool
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrendResult:
    wallet: str
    lifetime_score: Optional[ScoreResult]
    recent_score: Optional[ScoreResult]  # None if insufficient recent data
    trend: str  # "Improving" | "Declining" | "Stable" | "Insufficient recent data"
    delta: Optional[float]


def split_positions_by_lookback(
    positions: Iterable[ClosedPosition], now_ts: int, lookback_days: int
) -> tuple:
    """Returns (recent_positions, prior_positions). 'recent' = within lookback of now."""
    cutoff = now_ts - lookback_days * 86400
    positions = list(positions)
    recent = [p for p in positions if p.timestamp >= cutoff]
    prior = [p for p in positions if p.timestamp < cutoff]
    return recent, prior


def compute_trend(
    lifetime_scores: dict,
    recent_scores: dict,
    trend_cfg: cfg.TrendConfig = cfg.TREND,
) -> dict:
    """
    lifetime_scores / recent_scores: wallet -> ScoreResult, as produced by two
    separate calls to composite_and_percentile() — one over each trader's full
    history, one over only their recent-window positions (recent_scores should
    only contain wallets that cleared the recent_min_n gate).

    Returns wallet -> TrendResult for every wallet in lifetime_scores.
    """
    results = {}
    for w, life in lifetime_scores.items():
        recent = recent_scores.get(w)
        if life.percentile is None:
            results[w] = TrendResult(w, life, recent, "Insufficient recent data", None)
            continue
        if recent is None or recent.percentile is None:
            results[w] = TrendResult(w, life, None, "Insufficient recent data", None)
            continue
        delta = recent.percentile - life.percentile
        if delta > trend_cfg.improving_threshold:
            label = "Improving"
        elif delta < trend_cfg.declining_threshold:
            label = "Declining"
        else:
            label = "Stable"
        results[w] = TrendResult(w, life, recent, label, delta)
    return results


def fade_pool_eligible(
    trend_result: TrendResult,
    dumb_threshold: float = cfg.VERDICT.dumb_threshold,
    fade_cfg: cfg.FadePoolConfig = cfg.FADE,
) -> bool:
    """
    §3.3 rule: a trader may enter the dumb-money/fade pool only if
      lifetime_score < dumb_threshold
      AND trend is NOT "Improving" (when exclude_improving_from_fade is on)
      AND (recent_score unavailable OR recent_score also < dumb_threshold)
    """
    life = trend_result.lifetime_score
    if life is None or life.percentile is None:
        return False
    if life.percentile >= dumb_threshold:
        return False
    if fade_cfg.exclude_improving_from_fade and trend_result.trend == "Improving":
        return False
    recent = trend_result.recent_score
    if recent is not None and recent.percentile is not None:
        if recent.percentile >= dumb_threshold:
            return False
    return True


def holder_dumb_pool_eligible(
    h: Holder,
    dumb_threshold: float = cfg.VERDICT.dumb_threshold,
    fade_cfg: cfg.FadePoolConfig = cfg.FADE,
) -> bool:
    """
    Same §3.3 rule as fade_pool_eligible, adapted to Holder's own flat
    trend/recent_percentile fields instead of a separately-built TrendResult
    -- overall_market_verdict works from Holder objects directly, and this
    avoids round-tripping through ScoreResult/TrendResult just to ask the
    same question. A holder with trend=None (unknown -- e.g. the backtest
    engine doesn't compute a recent-vs-lifetime window at each historical
    checkpoint, so it never sets this) falls through both trend checks
    unfiltered, which is the correct, conservative default: no basis to
    exclude them, so they're judged on lifetime score alone, exactly as
    before this function existed.
    """
    if h.score_percentile is None or h.score_percentile >= dumb_threshold:
        return False
    if fade_cfg.exclude_improving_from_fade and h.trend == "Improving":
        return False
    if h.recent_percentile is not None and h.recent_percentile >= dumb_threshold:
        return False
    return True


def smart_pool_eligible(
    trend_result: TrendResult,
    smart_threshold: float = cfg.VERDICT.smart_threshold,
    fade_cfg: cfg.FadePoolConfig = cfg.FADE,
) -> bool:
    """Symmetric optional flag (§3.3): exclude 'Declining' traders from the smart pool. Off by default."""
    life = trend_result.lifetime_score
    if life is None or life.percentile is None:
        return False
    if life.percentile < smart_threshold:
        return False
    if fade_cfg.exclude_declining_from_smart and trend_result.trend == "Declining":
        return False
    return True


# ---------------------------------------------------------------------------
# §3.5 Market-side aggregation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Holder:
    wallet: str
    side: str            # "YES" | "NO"
    position_usd: float
    score_percentile: Optional[float]  # None if Unrated
    tier: str = "Unrated"
    n_trades: Optional[int] = None  # trader's resolved-position count, for §3.4 Bronze down-weighting
    first_active_ts: Optional[int] = None  # earliest observed trade; None = unknown (e.g. no resolved history)
    fetch_status: str = "ok"  # 'ok' | 'error' | 'never_fetched' -- see pipeline.record_fetch_failures
    avg_bet_usd: Optional[float] = None  # trader's lifetime volume/n; used by app.insiders' size check
    win_rate_weighted: Optional[float] = None  # trader's lifetime win rate; used by app.insiders' win-rate-anomaly check
    first_seen_at: Optional[int] = None  # when we first saw this wallet holding this market+side; used by app.insiders' clustering check
    trend: Optional[str] = None  # "Improving" | "Declining" | "Stable" | "Insufficient recent data" | None (unknown); see §3.3/fade_pool_eligible
    recent_percentile: Optional[float] = None  # Smart Score over just the trader's recent-window positions; None if unavailable


def tenure_days(holder: Holder, now_ts: int) -> Optional[float]:
    """Days since a holder's earliest observed trade, or None if unknown."""
    if holder.first_active_ts is None:
        return None
    return max(0.0, (now_ts - holder.first_active_ts) / 86400.0)


def usd_weighted_tenure_days(holders: Iterable[Holder], now_ts: int) -> Optional[float]:
    """$-weighted average tenure (days) across holders with a known first trade. None if none known."""
    points = [(tenure_days(h, now_ts), h.position_usd) for h in holders]
    points = [(t, w) for t, w in points if t is not None and w > 0]
    total = sum(w for _, w in points)
    if total <= 0:
        return None
    return sum(t * w for t, w in points) / total


def score_weight(h: Holder, tiers: cfg.ConfidenceTierConfig = cfg.TIERS) -> float:
    """
    Effective $ weight of a holder in the *score-weighted* aggregates (§3.5
    usd_weighted_score / net_smart_flow / smart_implied_probability, and the
    §3.6 signed distribution).

    §3.4: a Bronze trader is "down-weighted by min(1, n/30) in aggregates" --
    their conviction counts for less until they have a longer track record.
    Silver/Gold/Unrated are unaffected (Unrated already contributes $0 by
    virtue of having no score). Raw-dollar views -- the holder table and the
    §3.7 whale-concentration pies -- deliberately keep the true position_usd
    and do not call this.
    """
    if h.tier == "Bronze" and h.n_trades is not None:
        return h.position_usd * bronze_weight(h.n_trades, tiers)
    return h.position_usd


def usd_weighted_score(holders: Iterable[Holder]) -> Optional[float]:
    """§3.5: Σ(score·usd)/Σ(usd) over RATED holders on one side. None if no rated $."""
    rated = [h for h in holders if h.score_percentile is not None]
    total = sum(score_weight(h) for h in rated)
    if total <= 0:
        return None
    return sum(h.score_percentile * score_weight(h) for h in rated) / total


def net_smart_flow(holders: Iterable[Holder]) -> float:
    """
    §3.5: Σ_yes(usd·(score-50)/50) − Σ_no(usd·(score-50)/50).
    Unrated holders (score_percentile is None) contribute 0 — they are
    excluded from weight, not treated as neutral score-50 ($0 weight per §5).
    """
    yes_sum = sum(
        score_weight(h) * (h.score_percentile - 50) / 50
        for h in holders if h.side == "YES" and h.score_percentile is not None
    )
    no_sum = sum(
        score_weight(h) * (h.score_percentile - 50) / 50
        for h in holders if h.side == "NO" and h.score_percentile is not None
    )
    return yes_sum - no_sum


def smart_implied_probability(holders: Iterable[Holder]) -> Optional[float]:
    """
    §3.5: skill-weighted $ share on YES: Σ_yes(usd·max(score,1)) / [Σ_yes + Σ_no].
    Only rated holders contribute (Unrated = $0 weight per §5).
    """
    holders = list(holders)
    yes_w = sum(
        score_weight(h) * max(h.score_percentile, 1)
        for h in holders if h.side == "YES" and h.score_percentile is not None
    )
    no_w = sum(
        score_weight(h) * max(h.score_percentile, 1)
        for h in holders if h.side == "NO" and h.score_percentile is not None
    )
    denom = yes_w + no_w
    if denom <= 0:
        return None
    return yes_w / denom


def undervalued_gap(smart_implied_prob: Optional[float], current_market_price: float) -> Optional[float]:
    """§3.5: smart_implied_probability − current_market_price."""
    if smart_implied_prob is None:
        return None
    return smart_implied_prob - current_market_price


def gap_rank_score(gap: Optional[float], confidence_factor: float, market_liquidity: float) -> Optional[float]:
    """§3.5: rank by |gap| × confidence_factor / market_liquidity (thin markets don't dominate)."""
    if gap is None or market_liquidity is None or market_liquidity <= 0:
        return None
    return abs(gap) * confidence_factor / market_liquidity


# ---------------------------------------------------------------------------
# §3.6 Signed distribution chart
# ---------------------------------------------------------------------------

def signed_score(holder: Holder) -> Optional[float]:
    """(score-50) if YES else -(score-50). None if Unrated."""
    if holder.score_percentile is None:
        return None
    return (holder.score_percentile - 50) if holder.side == "YES" else -(holder.score_percentile - 50)


@dataclass(frozen=True)
class DistributionStats:
    weighted_mean: Optional[float]
    weighted_stdev: Optional[float]
    skewness: Optional[float]
    n: int
    total_weight: float


def signed_distribution_stats(holders: Iterable[Holder]) -> DistributionStats:
    """§3.6: $-weighted mean/stdev/skewness of the signed score across all rated holders."""
    points = []
    for h in holders:
        s = signed_score(h)
        w = score_weight(h)
        if s is not None and w > 0:
            points.append((s, w))
    n = len(points)
    total_weight = sum(w for _, w in points)
    if n == 0 or total_weight <= 0:
        return DistributionStats(None, None, None, n, total_weight)

    wmean = sum(s * w for s, w in points) / total_weight
    variance = sum(w * (s - wmean) ** 2 for s, w in points) / total_weight
    wstd = math.sqrt(variance) if variance > 0 else 0.0

    skew = None
    if wstd > 1e-9:
        m3 = sum(w * (s - wmean) ** 3 for s, w in points) / total_weight
        skew = m3 / (wstd ** 3)

    return DistributionStats(wmean, wstd, skew, n, total_weight)


# ---------------------------------------------------------------------------
# §3.7 Whale concentration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WhaleBucket:
    label: str
    holder_count: int
    usd_share: float  # fraction of this side's total $ (0-1)


@dataclass(frozen=True)
class WhaleConcentration:
    buckets: list
    gini: Optional[float]
    avg_score_top1: Optional[float]
    avg_score_rest: Optional[float]


def gini_coefficient(values: list) -> Optional[float]:
    """Standard Gini formula over a list of non-negative values."""
    vals = sorted(v for v in values if v is not None and v >= 0)
    n = len(vals)
    total = sum(vals)
    if n == 0 or total <= 0:
        return None
    cum = sum((i + 1) * v for i, v in enumerate(vals))  # i is 0-indexed -> rank = i+1
    return (2 * cum - (n + 1) * total) / (n * total)


def whale_concentration(holders_one_side: Iterable[Holder]) -> WhaleConcentration:
    """
    §3.7: sort by position_usd desc; bucket by share of HOLDER COUNT into
    top 1% / next 1-10% / next 10-50% / bottom 50%; report each bucket's
    share of that side's total $. Also Gini and avg_score(top1%) vs avg_score(rest).
    """
    holders = sorted(holders_one_side, key=lambda h: h.position_usd, reverse=True)
    n = len(holders)
    total_usd = sum(h.position_usd for h in holders)
    if n == 0 or total_usd <= 0:
        return WhaleConcentration(buckets=[], gini=None, avg_score_top1=None, avg_score_rest=None)

    top1_end = max(1, math.ceil(0.01 * n))
    next10_end = max(top1_end, math.ceil(0.10 * n))
    next50_end = max(next10_end, math.ceil(0.50 * n))
    next50_end = min(next50_end, n)

    top1 = holders[:top1_end]
    next_1_10 = holders[top1_end:next10_end]
    next_10_50 = holders[next10_end:next50_end]
    bottom_50 = holders[next50_end:]

    def usd_share(group):
        return sum(h.position_usd for h in group) / total_usd

    buckets = [
        WhaleBucket("Top 1%", len(top1), usd_share(top1)),
        WhaleBucket("Next 1-10%", len(next_1_10), usd_share(next_1_10)),
        WhaleBucket("Next 10-50%", len(next_10_50), usd_share(next_10_50)),
        WhaleBucket("Bottom 50%", len(bottom_50), usd_share(bottom_50)),
    ]

    gini = gini_coefficient([h.position_usd for h in holders])

    top1_rated = [h.score_percentile for h in top1 if h.score_percentile is not None]
    rest_rated = [h.score_percentile for h in holders[top1_end:] if h.score_percentile is not None]
    avg_top1 = mean(top1_rated) if top1_rated else None
    avg_rest = mean(rest_rated) if rest_rated else None

    return WhaleConcentration(buckets=buckets, gini=gini, avg_score_top1=avg_top1, avg_score_rest=avg_rest)


# ---------------------------------------------------------------------------
# §3.8 Overall Market Verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketVerdict:
    label: str            # e.g. "Strong Buy YES", "Lean NO", "Fade YES", "Mixed / Conflicting", "Insufficient data"
    conviction: float      # 0-100
    smart_volume_share: Optional[float]
    dumb_volume_share: Optional[float]
    unrated_volume_share: Optional[float]
    net_smart_flow: float
    undervalued_gap: Optional[float]


def overall_market_verdict(
    holders: Iterable[Holder],
    current_market_price: float,
    verdict_cfg: cfg.VerdictConfig = cfg.VERDICT,
) -> MarketVerdict:
    """§3.8: headline label + 0-100 conviction score for the market list page."""
    holders = list(holders)
    total_usd = sum(h.position_usd for h in holders)
    rated = [h for h in holders if h.score_percentile is not None]
    rated_usd = sum(h.position_usd for h in rated)

    unrated_volume_share = (
        (total_usd - rated_usd) / total_usd if total_usd > 0 else None
    )

    smart_usd = sum(h.position_usd for h in rated if h.score_percentile >= verdict_cfg.smart_threshold)
    # §3.3 fade-pool rule: a holder whose lifetime score is low but who is
    # trending "Improving" (or whose recent-window score has already
    # recovered above dumb_threshold) isn't counted as dumb money -- their
    # bad lifetime record no longer reflects their current skill. See
    # holder_dumb_pool_eligible; a holder with no trend data (trend=None,
    # e.g. every backtest holder -- see that function's docstring) is
    # judged on lifetime score alone, unchanged from before this rule existed.
    dumb_usd = sum(h.position_usd for h in rated if holder_dumb_pool_eligible(h, verdict_cfg.dumb_threshold))
    smart_volume_share = smart_usd / rated_usd if rated_usd > 0 else None
    dumb_volume_share = dumb_usd / rated_usd if rated_usd > 0 else None

    nsf = net_smart_flow(holders)
    sip = smart_implied_probability(holders)
    gap = undervalued_gap(sip, current_market_price)

    n_rated_holders = len(rated)

    # Insufficient data gate
    if (
        unrated_volume_share is None
        or unrated_volume_share > verdict_cfg.insufficient_data_unrated_share
        or n_rated_holders < verdict_cfg.insufficient_data_min_rated_holders
    ):
        return MarketVerdict(
            "Insufficient data", 0.0, smart_volume_share, dumb_volume_share,
            unrated_volume_share, nsf, gap,
        )

    smart_side = "YES" if nsf > 0 else "NO"
    dumb_side = "NO" if nsf > 0 else "YES"  # side smart money is fading

    # smart $ concentrated on which side?
    smart_on_yes = sum(h.position_usd for h in rated if h.side == "YES" and h.score_percentile >= verdict_cfg.smart_threshold)
    smart_on_no = sum(h.position_usd for h in rated if h.side == "NO" and h.score_percentile >= verdict_cfg.smart_threshold)
    dumb_on_yes = sum(h.position_usd for h in rated if h.side == "YES" and holder_dumb_pool_eligible(h, verdict_cfg.dumb_threshold))
    dumb_on_no = sum(h.position_usd for h in rated if h.side == "NO" and holder_dumb_pool_eligible(h, verdict_cfg.dumb_threshold))

    smart_dominant_side = "YES" if smart_on_yes >= smart_on_no else "NO"
    dumb_dominant_side = "YES" if dumb_on_yes >= dumb_on_no else "NO"

    strong_buy = (
        gap is not None
        and abs(gap) * 100 >= verdict_cfg.strong_buy_gap_pts
        and smart_volume_share is not None
        and smart_volume_share >= verdict_cfg.strong_buy_smart_share
        and unrated_volume_share <= verdict_cfg.strong_buy_max_unrated_share
    )

    # Mixed/Conflicting: smart and dumb money both concentrated on the SAME side
    mixed = (
        smart_volume_share is not None and dumb_volume_share is not None
        and smart_volume_share > 0.15 and dumb_volume_share > 0.15
        and smart_dominant_side == dumb_dominant_side
    )

    conviction = _conviction_score(smart_volume_share, dumb_volume_share, unrated_volume_share, gap)

    if mixed and not strong_buy:
        return MarketVerdict(
            "Mixed / Conflicting", conviction, smart_volume_share, dumb_volume_share,
            unrated_volume_share, nsf, gap,
        )

    if strong_buy:
        return MarketVerdict(
            f"Strong Buy {smart_side}", conviction, smart_volume_share, dumb_volume_share,
            unrated_volume_share, nsf, gap,
        )

    # Fade {opposite side}: dumb_volume_share dominates one side (dumb_usd/
    # dumb_on_yes/dumb_on_no above already exclude fade-pool-ineligible
    # holders -- see holder_dumb_pool_eligible) and there's no offsetting
    # smart signal on that same side.
    dumb_dominates = (
        dumb_volume_share is not None
        and dumb_volume_share >= 0.30
        and (smart_volume_share is None or smart_volume_share < verdict_cfg.strong_buy_smart_share)
    )
    if dumb_dominates:
        fade_target_side = dumb_dominant_side  # the side dumb money owns; lean the opposite
        lean_side = "NO" if fade_target_side == "YES" else "YES"
        return MarketVerdict(
            f"Fade {fade_target_side} (lean {lean_side})", conviction, smart_volume_share,
            dumb_volume_share, unrated_volume_share, nsf, gap,
        )

    return MarketVerdict(
        f"Lean {smart_side}", conviction, smart_volume_share, dumb_volume_share,
        unrated_volume_share, nsf, gap,
    )


def _conviction_score(
    smart_volume_share: Optional[float],
    dumb_volume_share: Optional[float],
    unrated_volume_share: Optional[float],
    gap: Optional[float],
) -> float:
    """
    0-100 conviction number. Not separately specified by §3.8 beyond "a single
    0-100 conviction number" alongside the verdict label, so this combines:
    how lopsided smart-vs-dumb $ is, how big the mispricing gap is, and how
    much of the market is confidently rated (low unrated share) — each
    normalized to 0-1 and averaged.
    """
    smart = smart_volume_share or 0.0
    dumb = dumb_volume_share or 0.0
    lopsidedness = abs(smart - dumb)  # 0-1
    gap_component = min(abs(gap) * 2, 1.0) if gap is not None else 0.0  # |gap|=0.5 -> maxed
    rated_component = 1.0 - (unrated_volume_share if unrated_volume_share is not None else 1.0)
    rated_component = max(0.0, min(1.0, rated_component))
    conviction = 100.0 * (0.45 * lopsidedness + 0.35 * gap_component + 0.20 * rated_component)
    return round(max(0.0, min(100.0, conviction)), 1)
