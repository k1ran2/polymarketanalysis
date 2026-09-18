"""
Single source of truth for every threshold / weight used by the scoring
engine (§3) and the filter panel (§4) of the Polymarket Smart Money Dashboard.

Nothing in scoring.py or the routers should hard-code a magic number that
belongs here. Changing behavior should mean changing this file (or, at
runtime, the query params that override a subset of these for filters).
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Loads backend/.env if present (see .env.example). Real secrets -- SMTP
# credentials -- live only there, never in this file or in git.
load_dotenv()


@dataclass(frozen=True)
class ConfidenceTierConfig:
    # Unrated: n < unrated_max_n -> excluded from everything (contributes $0 weight)
    unrated_max_n: int = 5

    # Bronze: n < bronze_min_n OR volume < bronze_min_volume -> down-weighted
    bronze_min_n: int = 15
    bronze_min_volume: float = 500.0
    bronze_weight_cap_denominator: int = 30  # weight = min(1, n / 30)

    # Silver: n >= silver_min_n AND volume >= silver_min_volume
    silver_min_n: int = 15
    silver_min_volume: float = 500.0

    # Gold: n >= gold_min_n AND volume >= gold_min_volume AND active in last gold_active_days
    gold_min_n: int = 50
    gold_min_volume: float = 5000.0
    gold_active_days: int = 90


@dataclass(frozen=True)
class CompositeScoreWeights:
    sharpe: float = 0.35
    mean_return: float = 0.30
    win_rate: float = 0.20
    brier: float = 0.15  # applied to (-brier_score)

    def as_tuple(self):
        return (self.sharpe, self.mean_return, self.win_rate, self.brier)


@dataclass(frozen=True)
class SmartScoreParams:
    """
    Per-wallet Smart Score (0-100), computed from a trader's own resolved
    history with a *fixed* curve -- no population percentile (a deliberate
    simplification of spec §3.2: each wallet is scored in isolation, so the
    score is stable and doesn't need everyone else's data on hand).

    Each sub-metric is mapped to 0-1 by a fixed function, weight-averaged over
    whatever metrics the trader has, scaled to 0-100, then shrunk toward a
    neutral 50 by sample size so a 10-bet record can't read as a 95.
    """
    min_n: int = 5  # below this, no score (matches the Unrated gate)

    # weights (mirror CompositeScoreWeights; renormalized over available metrics)
    w_sharpe: float = 0.35
    w_return: float = 0.30
    w_winrate: float = 0.20
    w_brier: float = 0.15

    # mean_return -> logistic(mean_return / return_scale); +0.25 avg return ~ 0.73
    return_scale: float = 0.25
    # sharpe_like -> logistic(sharpe_like / sharpe_scale); +1.0 ~ 0.73
    sharpe_scale: float = 1.0
    # win_rate_weighted linearly mapped from [floor, ceil] -> [0, 1]
    winrate_floor: float = 0.40
    winrate_ceil: float = 0.80
    # brier linearly mapped from [worst, best] -> [0, 1] (lower brier = better)
    brier_worst: float = 0.35
    brier_best: float = 0.10
    # small-sample shrink: score = 50 + (raw-50) * n/(n+shrink_k)
    shrink_k: float = 25.0


@dataclass(frozen=True)
class TrendConfig:
    default_lookback_days: int = 90
    lookback_options: tuple = (30, 60, 90, 180)
    improving_threshold: float = 15.0   # recent_score - lifetime_score > +15
    declining_threshold: float = -15.0  # recent_score - lifetime_score < -15
    recent_min_n: int = 5  # gate for computing recent_score at all


@dataclass(frozen=True)
class VerdictConfig:
    smart_threshold: float = 60.0
    dumb_threshold: float = 40.0
    strong_buy_gap_pts: float = 10.0          # |undervalued_gap| >= 10 percentage points
    strong_buy_smart_share: float = 0.30      # smart_volume_share >= 30%
    strong_buy_max_unrated_share: float = 0.50  # unrated_volume_share <= 50%
    insufficient_data_unrated_share: float = 0.70  # unrated_volume_share > 70% -> Insufficient data
    insufficient_data_min_rated_holders: int = 3   # fewer rated holders than this -> Insufficient data


@dataclass(frozen=True)
class FadePoolConfig:
    # A trader may enter the dumb-money/fade pool only if lifetime_score < dumb_threshold
    # AND trend is not "Improving" AND (recent_score unavailable OR also < dumb_threshold).
    exclude_improving_from_fade: bool = True      # default ON per §4
    exclude_declining_from_smart: bool = False    # default OFF per §4 (symmetric optional flag)


@dataclass(frozen=True)
class FilterDefaults:
    trader_min_volume: float = 0.0
    trader_min_trades: int = 0
    trader_min_tier: str = "Unrated"  # dropdown: Unrated/Bronze/Silver/Gold
    market_min_liquidity: float = 0.0
    market_min_holders: int = 0
    categories: tuple = ()  # empty = all


@dataclass(frozen=True)
class RateLimitConfig:
    # Conservative fractions of documented ceilings, expressed as (max_requests, per_seconds)
    gamma: tuple = (350, 10.0)       # docs: ~4,000 / 10s
    clob: tuple = (800, 10.0)        # docs: ~9,000 / 10s
    # docs: ~1,000 / 10s -- the tight one, and evidently the documented
    # ceiling isn't what's actually enforced in practice: 500/10s (half the
    # documented limit) with 20 concurrent workers got 1001/1447 wallets
    # rate-limited to outright failure on a real run. Whatever Polymarket
    # really enforces for unauthenticated Data API traffic is well below the
    # docs. 100/10s (~10/s) with 5 workers gets through a real ~110-wallet
    # seed in ~2 min, dropping only a handful of wallets to 429s -- which the
    # single-threaded retry pass in backfill.py then mops up. The real fix
    # for speed is a paid CLOB/Data API key, not a higher unauth ceiling.
    data_api: tuple = (70, 10.0)


@dataclass(frozen=True)
class BackfillConfig:
    seed_leaderboard_limit: int = 30
    seed_top_markets_limit: int = 20
    # Holders to pull per market side when seeding (§7). The /holders fast path
    # returns up to ~100/side; every wallet it returns gets a full
    # closed-positions backfill, so 20 markets * 100 * 2 sides = ~4k wallets and
    # a ~15 min first run. 25/side keeps a real seed run near ~3-4 min while
    # still capturing the whales that actually move the §3.5/§3.8 aggregates.
    # The rest of a market's holders surface as Unrated until a later pass.
    seed_holders_per_market_side: int = 25
    closed_positions_page_size: int = 50  # /closed-positions caps `limit` at 50 server-side
    # We page /closed-positions newest-first (sortBy=TIMESTAMP) and stop here.
    # The Smart Score is stable within a few hundred positions, and the *recent*
    # few hundred bets are a better read on current skill than a trader's 5,000
    # all-time positions (deep pagination on whales was the single biggest drag
    # on a cold market -- 300 keeps their holders scoring in well under a minute).
    max_closed_positions_per_wallet: int = 300
    closed_positions_sort_by: str = "TIMESTAMP"
    # Wallets are fetched concurrently (network-bound, not CPU-bound -- most
    # wall-clock time is spent waiting on Polymarket's response, not doing
    # work), so overlapping requests instead of doing them one at a time is
    # the single biggest speedup available. Shared with the rate limiter's
    # token bucket, so this doesn't bypass it -- it just stops each request
    # from waiting for the previous one's full round-trip before starting.
    #
    # Was 20, which -- combined with the old 500/10s data_api ceiling --
    # caused a real run to get 1001/1447 wallets rate-limited to failure.
    # 5 workers against 100/10s (see RateLimitConfig.data_api) clears a real
    # ~110-wallet seed in ~2 min; the few wallets that still 429 out are
    # recovered by backfill.py's single-threaded retry pass.
    fetch_concurrency: int = 4
    progress_every_n_wallets: int = 25

    # Retry/backoff budget for the Data API client used by backfill specifically
    # (not search -- see search_service.py, which intentionally keeps the
    # short/fast-fail defaults in RateLimitedClient because §6 promises search
    # falls back "in seconds"). Backfill is a background script the user runs
    # once and waits on, so it can and should absorb transient 429s by
    # retrying patiently instead of giving up on a wallet after ~15s.
    # Kept modest: a stuck 429'ing request with a long backoff * many retries,
    # multiplied across the worker pool, is what turned an earlier run into a
    # 15-minute hang. The main pass should fail a rate-limited wallet quickly
    # and let the single-threaded retry pass (concurrency 1, which barely 429s
    # at all) pick it back up.
    data_api_max_retries: int = 4
    data_api_backoff_cap: float = 8.0


@dataclass(frozen=True)
class OnDemandSearchConfig:
    # §6: fast-path holder fetch for a searched market not yet in the DB
    holders_fast_path_limit: int = 100
    full_reconstruction_min_volume: float = 50_000.0  # only fully reconstruct markets above this
    repeat_search_threshold: int = 2  # or a market searched more than N times

    # On-demand market analysis (the primary Phase 1 flow -- pick a market, score
    # its holders now). Keep the holder count modest so a cold market resolves
    # fast; the long tail of tiny holders barely moves the §3.5/§3.8 aggregates.
    analyze_holders_per_side: int = 40
    analyze_concurrency: int = 3
    # A market whose holder snapshot is younger than this is served straight
    # from cache with no live refetch (keeps re-opens instant).
    market_fresh_seconds: int = 900
    # Market-list metadata refresh cadence (one Gamma call, no holders).
    market_list_fresh_seconds: int = 600
    market_list_size: int = 24


@dataclass(frozen=True)
class InsiderConfig:
    """
    Heuristic (unvalidated, first-pass) "insider-pattern" detector. Six
    factors, re-derived from public reporting on real Polymarket insider
    cases (Iran peace deal, Maduro removal, Axiom reveal, a 150+-wallet
    military-secrets cluster) and the detection methodology Polymarket's own
    Chainalysis-based tooling and independent trackers (Polysights/Insider
    Finder, open-source detectors) reportedly use -- NOT a validated model,
    just a better-informed guess than the original version. See app.insiders
    for the scoring; every threshold/weight here is meant to be tuned as real
    markets are observed.

    Sources consulted (Sept 2026): Bloomberg's Iran-strikes and "traders who
    knew the future" pieces, Forbes' "$1.2M hours before Iran airstrikes",
    Yahoo/Finance on the 150-wallet military case, BeInCrypto on the Maduro
    bet, and the pselamy/suislanchez open-source Polymarket insider detectors.
    """
    # $1,000 flagged almost every casual small bettor with <5 resolved trades
    # (extremely common on Polymarket) as "82.5/100 insider" regardless of
    # size, since a wallet with zero history has no personal baseline to
    # compare against. $5,000 cuts that noise down; still tunable per-deployment.
    min_position_usd: float = 5_000.0

    # --- age: reported cases cluster in HOURS, not weeks --------------------
    # "57% of flagged wallets were created <24h before the trade"; the Maduro
    # wallets were "days" old. Full suspicion inside 24h, decaying to zero by
    # 10 days -- the original 30-day linear window was far too generous.
    new_wallet_hours_full: float = 24.0
    new_wallet_days_zero: float = 10.0

    thin_history_n: int = 5                 # <= this many resolved trades = "no track record"
    size_multiple_flag: float = 5.0         # position >= Nx the wallet's own historical avg bet

    # --- wallet clustering: repeatedly present in real cases -----------------
    # (Iran strikes: several fresh wallets bought within minutes of each other;
    # Maduro: 3 wallets; Axiom: "a small cluster"). Needs market_holders_cache.
    # first_seen_at, which only starts accumulating from when this shipped --
    # it can't retroactively detect clusters in already-cached snapshots.
    cluster_window_hours: float = 48.0      # other thin/new wallets, same side, first-seen within this window
    cluster_full_at: int = 3                # this many co-arrivals (including self) -> maximum cluster_factor

    # --- win-rate anomaly: catches insiders who DO have some history --------
    # Polymarket's own tooling reportedly flags >80% win rate as statistically
    # anomalous; needs enough trades for a win rate to mean anything.
    winrate_anomaly_min_n: int = 10
    winrate_anomaly_floor: float = 0.65     # win rate at/below this -> not anomalous (0)
    winrate_anomaly_ceil: float = 0.92      # win rate at/above this -> maximally anomalous (1)

    # --- urgency: position visible with resolution imminent -----------------
    # A pattern professional detectors use ("pre-resolution timing"). Needs
    # markets.end_date (from Gamma); most markets don't resolve soon, so
    # "unknown/far away" reads as 0, not suspicious, unlike the age factor.
    urgency_days_full: float = 3.0
    urgency_days_zero: float = 30.0

    # score = 100 * sum(weight * factor) over age/size/thin/cluster/winrate/urgency
    w_age: float = 0.25
    w_size: float = 0.20
    w_thin: float = 0.15
    w_cluster: float = 0.20
    w_winrate: float = 0.10
    w_urgency: float = 0.10
    alert_threshold: float = 70.0           # insider_score >= this -> eligible for an email alert

    # Market-level roll-up (app.insiders.summarize_market): how much
    # severity-weighted $ counts as "a lot of insider activity" for the
    # market's conviction number -- a market at or above this is treated as
    # maximally active on that axis. Tunable; not derived from anything.
    activity_scale_usd: float = 200_000.0
    # Below this fraction of |YES-lean - NO-lean| the market reads as
    # "activity on both sides" rather than leaning a direction.
    balanced_imbalance: float = 0.2

    # A market priced within this margin ($0.01 -- 1 cent) of $0 or $1 is
    # effectively already resolved -- there's no room left to trade an edge
    # either direction, so flags there are academic (a wallet could still be
    # a genuine insider, but there's no actionable mispricing left). Hidden
    # from the Insiders tab by default; still visible via
    # GET /api/insiders?include_near_resolved=true.
    near_resolved_price_margin: float = 0.01


@dataclass(frozen=True)
class BacktestConfig:
    """
    Point-in-time replay of the Smart Money verdict / insider-score signals
    against already-resolved markets (app.backtest_data / app.backtest_engine).

    IMPORTANT limitation, disclosed in the UI too: entry_price comes from
    CLOB /prices-history, which is the last TRADED price at each sample, not
    a true historical bid/ask. Polymarket doesn't expose historical
    order-book depth publicly, so backtest P&L is somewhat optimistic versus
    what a real fill would have cost, especially on thin/illiquid markets.
    """
    default_stake_usd: float = 100.0        # $ per simulated trade, so results are comparable across trades
    default_checkpoint_hours: float = 6.0   # how often to re-check the signal over a market's life
    max_checkpoints_per_market: int = 200   # hard cap so a years-long market can't blow up one run
    min_holder_position_usd: float = 1.0    # floor for a reconstructed position to count as a "holder" at all
    # A signal recommending a side already priced at/above this isn't a real
    # trade worth taking -- confirmed from real backtest output: buying a
    # "sure thing" at 99.8c risks the full stake to make pennies if right
    # (payout = stake*(1-price)/price -> ~$0.20 per $100 at 0.998), so a
    # handful of genuine wins there contribute almost nothing while a single
    # unexpected loss wipes out dozens of them -- exactly what was dragging
    # a plausible-looking win rate down to a negative overall ROI. Only
    # gates the HIGH side (entry near $1): a low entry (near $0) is the
    # opposite case -- a correct contrarian call there pays out a large
    # multiple, which is genuine signal value, not noise, so it's kept.
    max_no_edge_entry_price: float = 0.98
    price_lookback_hours: float = 48.0      # a signal is skipped if no price sample exists within this window of it
    trades_page_size: int = 500             # Data API /trades page size
    max_trade_pages_per_market: int = 400   # hard cap (~200k trades) so one whale market can't stall a run
    wallet_fresh_days: int = 30             # a wallet's cached closed-position history is reused for this long

    # The actual cost driver isn't which markets get backtested, it's how
    # many DISTINCT wallets' full trading histories get fetched -- a $5M
    # market can have 1,000+ of them (confirmed live: ~1,500 wallets, ~10min).
    # But every $-weighted metric here (smart_implied_probability,
    # net_smart_flow, overall_market_verdict) is dominated by the largest
    # positions -- a wallet holding $40 of a $5M market moves the aggregate
    # by nothing. So only the wallets that can actually change the outcome
    # get their history fetched at all:
    #   - the top N holders by position size at each checkpoint (smart-score
    #     accuracy for the aggregate verdict), and
    #   - separately, anyone whose position clears INSIDER.min_position_usd
    #     (insider detection needs to see them regardless of rank).
    # Everyone else is scored exactly as an untracked wallet already is
    # ("Unrated", $0 weight) -- no separate code path, just never fetched.
    # This is what makes a $5M market backtest at roughly the same wall-clock
    # cost as a $50k one instead of ~50x slower.
    smart_score_top_n_holders: int = 40
    # _wallets_worth_scoring samples this many checkpoints (downsampled from
    # the full, finer-grained `checkpoints` list -- signal EVALUATION still
    # uses all of them, this only affects which wallets get fetched at all)
    # to find the top-N holders at each. Confirmed live: using every one of
    # up to max_checkpoints_per_market (200) checkpoints for this made the
    # UNION of "top 40 at that moment" balloon back up to 100-200+ distinct
    # wallets for a market with real holder turnover -- almost as bad as not
    # reducing the wallet set at all.
    #
    # The real tradeoff, stated accurately (an earlier version of this
    # comment claimed samples land on "adjacent 6-hour checkpoints", which
    # was never true -- 16 samples spread over up to 200 checkpoints are
    # themselves ~12 checkpoints, i.e. ~3 days, apart): a wallet that puts
    # on a large position and holds it is very likely caught at the NEXT
    # sample regardless of gap size, since the position is still there.
    # The real risk is a large position taken late and never sampled at all
    # before the market resolves -- exactly the insider profile this
    # detector exists to catch. bulk_share_timeline/_evenly_spaced_indices
    # guarantee the market's FINAL checkpoint is always one of the 16
    # samples (a real bug fixed this session: the naive even-stride pick
    # used to silently drop the last ~1% of a long market's checkpoints,
    # which for a 455-day market was the final 2.25 days), which closes the
    # single highest-value gap without needing more samples. Increasing
    # this further is a real lever for reducing GENERAL mid-life gaps if a
    # future audit finds that still costs real signals, at a proportional
    # increase in per-market compute (no extra network cost either way --
    # this is a purely local pass over already-cached data).
    wallet_discovery_checkpoints: int = 16
    # GLOBAL cap (backtest_engine._data_api_inflight) on concurrent Data API
    # /closed-positions calls, shared across every market a run is
    # processing at once -- not per-market. Confirmed live: running several
    # markets concurrently without a shared cap let per-market pools stack
    # (market_concurrency x this number), which caused real request
    # failures ("rate-limited after 5 retries", wallets silently dropped),
    # not just slower throughput -- the same failure mode RateLimitConfig
    # documents from the original backfill script at higher concurrency.
    fetch_concurrency: int = 6
    # Markets processed at once in one run. Almost all of a market's cost is
    # waiting on rate-limited network calls, and different markets don't
    # need to wait on each other for that -- the shared token buckets in
    # polymarket_client cap real aggregate throughput correctly regardless
    # of how many markets are in flight, so this just keeps that budget
    # continuously used instead of idle behind one slow market. Kept modest
    # (not e.g. 20) mainly to bound SQLite write contention, not because a
    # higher rate limit couldn't be usefully shared across more of them.
    market_concurrency: int = 4


@dataclass(frozen=True)
class AlertConfig:
    """
    Email alerting for insider_flags (app.alerts). All from-the-environment
    (backend/.env, see .env.example) -- never hard-code credentials here.
    If SMTP_HOST is unset, alerts are just logged instead of sent, so the
    detector is testable before you've set up a mail relay.
    """
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() != "false"
    alert_from: str = os.getenv("ALERT_EMAIL_FROM", "")
    alert_to: str = os.getenv("ALERT_EMAIL_TO", "")

    @property
    def configured(self) -> bool:
        return bool(self.smtp_host and self.alert_from and self.alert_to)


@dataclass(frozen=True)
class PollerConfig:
    """Background loop (app.poller) that re-analyzes tracked markets so insider
    alerts can fire even when nobody has the dashboard open. Runs only while
    the backend process is running -- see README for a real always-on setup."""
    enabled: bool = os.getenv("POLLER_ENABLED", "true").lower() != "false"
    interval_seconds: int = int(os.getenv("POLLER_INTERVAL_SECONDS", "300"))


TIERS = ConfidenceTierConfig()
WEIGHTS = CompositeScoreWeights()
SCORE = SmartScoreParams()
TREND = TrendConfig()
VERDICT = VerdictConfig()
FADE = FadePoolConfig()
FILTERS = FilterDefaults()
RATE_LIMITS = RateLimitConfig()
BACKFILL = BackfillConfig()
ON_DEMAND = OnDemandSearchConfig()
INSIDER = InsiderConfig()
ALERTS = AlertConfig()
POLLER = PollerConfig()
BACKTEST = BacktestConfig()

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

DISCLAIMER = (
    "This dashboard is for informational and research purposes only. It is not "
    "financial advice, and nothing shown here is a recommendation to buy, sell, "
    "or hold any position. Prediction markets carry real financial risk. Do your "
    "own research and consult a licensed financial advisor before trading."
)
