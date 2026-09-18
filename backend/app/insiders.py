"""
Heuristic "insider-pattern" detector (app.config.InsiderConfig).

Six factors, re-derived from public reporting on real Polymarket insider
cases and third-party/Polymarket-internal detection methodology (see
config.InsiderConfig's docstring for sources):
  - age: how recently the wallet became active on Polymarket
  - size: how large this position is, relative to the wallet's own history
    (or absolute $, log-scaled, when there's no history to compare against)
  - thin: how little resolved trade history the wallet has at all
  - cluster: whether other similarly new/thin wallets took the same side of
    the same market around the same time (documented in multiple real cases)
  - winrate: an anomalously high win rate for a wallet with *enough* history
    to make that mean something (catches insiders who aren't thin-history)
  - urgency: whether the position surfaced with the market close to resolving

age/size/thin are always computable. cluster/winrate/urgency only apply when
there's a real basis to judge them (enough trades to trust a win rate, a
parseable resolution date, other thin holders to compare arrival times
against) -- when a factor doesn't apply, it's left out of the weighted
average entirely (the same renormalize-over-available-signals technique as
scoring.smart_score_from_raw) rather than silently counted as a reassuring
zero.

This is a first-pass, unvalidated heuristic, not a fraud/regulatory
determination -- every threshold lives in config.INSIDER and is meant to be
tuned as real markets are observed, not treated as ground truth.

On timestamps: a flag's `first_seen_at` is when THIS SYSTEM first observed
the wallet holding that position (market_holders_cache.first_seen_at, set
once on first insert and never touched again) -- it is NOT the trade's real
execution time. Getting the literal trade timestamp would mean pulling the
market's entire trade tape (Data API /trades, the same expensive per-market
fetch app.backtest_data does for backtesting -- minutes for a busy market,
since Polymarket exposes no "this wallet's trades in this market" filter)
on every poller cycle for every flagged wallet, which isn't affordable for a
live, repeatedly-re-scanned feature. `first_seen_at` is accurate to within
one poll interval (PollerConfig.interval_seconds) *for markets this system
has been continuously tracking since before the position was opened* -- for
a market only just started tracking, it can lag the real entry by however
long ago tracking began.
"""
from __future__ import annotations

import bisect
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import config as cfg
from .scoring import Holder, tenure_days


@dataclass(frozen=True)
class InsiderFlag:
    wallet: str
    side: str
    position_usd: float
    insider_score: float  # 0-100
    reasons: list
    tenure_days: Optional[float]
    n_resolved: int
    size_multiple: Optional[float]  # position_usd / the wallet's own historical avg bet
    score_percentile: Optional[float] = None  # the wallet's own Smart Score, for context alongside insider_score
    tier: str = "Unrated"
    first_seen_at: Optional[int] = None  # when THIS SYSTEM first observed the position, not the trade's real timestamp -- see module docstring


def _age_factor(days: Optional[float], n_resolved: int, insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> float:
    """
    1.0 = as new as it gets. Public reporting on flagged Polymarket wallets
    found 57% were created under 24h before the trade, with the rest mostly
    "days" old -- so full suspicion holds through new_wallet_hours_full (24h),
    then decays linearly to zero by new_wallet_days_zero (10 days). Much
    steeper than treating "new" as anything under a month.

    If tenure is unknown (no first_active_ts on record) that only reads as
    "new" when we also have zero resolved history to go on -- if n_resolved
    is substantial, a missing tenure is a data gap (e.g. an old traders row
    from before this field existed), not evidence the wallet is new.
    """
    if days is None:
        return 1.0 if n_resolved == 0 else 0.0
    hours_full = insider_cfg.new_wallet_hours_full
    if days * 24.0 <= hours_full:
        return 1.0
    days_zero = insider_cfg.new_wallet_days_zero
    if days >= days_zero:
        return 0.0
    span = days_zero - hours_full / 24.0
    return 1.0 - (days - hours_full / 24.0) / span


def _size_factor(position_usd: float, avg_bet_usd: Optional[float], insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> float:
    """
    0-1: how large this position is. Prefers comparing against the wallet's
    own historical average bet; with no baseline (typically a wallet with no
    resolved history at all, so there's nothing to average), falls back to
    log-scaled absolute size so a $1,001 position and a $1,000,000 position
    from an equally history-less wallet don't score identically.
    """
    if avg_bet_usd and avg_bet_usd > 0:
        multiple = position_usd / avg_bet_usd
        if multiple < insider_cfg.size_multiple_flag:
            return 0.0
        return min(1.0, (multiple - insider_cfg.size_multiple_flag) / (insider_cfg.size_multiple_flag * 3))
    if position_usd < insider_cfg.min_position_usd:
        return 0.0
    ratio = position_usd / insider_cfg.min_position_usd
    return max(0.0, min(1.0, math.log10(max(ratio, 1.0)) / 3.0))  # 1000x the min-position floor -> maxed


def _thin_factor(n_resolved: int, insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> float:
    if n_resolved >= insider_cfg.thin_history_n:
        return 0.0
    return 1.0 - n_resolved / insider_cfg.thin_history_n


def _winrate_anomaly_factor(win_rate: float, insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> float:
    """0-1: how far above a plausible-but-good win rate this is. Caller gates on n_resolved."""
    floor, ceil = insider_cfg.winrate_anomaly_floor, insider_cfg.winrate_anomaly_ceil
    if win_rate <= floor:
        return 0.0
    return min(1.0, (win_rate - floor) / (ceil - floor))


def _urgency_factor(end_date: Optional[str], now_ts: int, insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> Optional[float]:
    """
    0-1, or None if inapplicable (no/unparseable end date). Unlike age, an
    unknown or distant resolution date is NOT suspicious by default -- most
    markets aren't close to resolving most of the time, so "unknown" must
    stay excluded from the score rather than reading as 0-and-reassuring or
    (worse) 1-and-alarming.
    """
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        end_ts = dt.timestamp()
    except (ValueError, TypeError):
        return None
    days_left = (end_ts - now_ts) / 86400.0
    full, zero = insider_cfg.urgency_days_full, insider_cfg.urgency_days_zero
    if days_left <= full:
        return 1.0
    if days_left >= zero:
        return 0.0
    return 1.0 - (days_left - full) / (zero - full)


def _compute_cluster_factors(holders: list, insider_cfg: cfg.InsiderConfig = cfg.INSIDER) -> dict:
    """
    {(wallet, side): cluster_factor} for thin-history holders with a known
    first_seen_at -- how many OTHER thin holders on the same side were first
    seen within cluster_window_hours of this one (0 others -> 0.0, full at
    cluster_full_at-1 others -> 1.0). A holder absent from the returned dict
    means "no basis to judge" (not thin, or first_seen_at not yet known) --
    compute_insider_score then excludes the cluster factor entirely for them
    rather than counting it as a reassuring zero.

    Caveat: first_seen_at is when *we* first observed the wallet holding this
    market+side, not when they actually entered the position -- on a market's
    first-ever scan, every holder gets the same timestamp, so this
    over-triggers there. It becomes meaningful as a market is re-scanned
    repeatedly over time (the poller does this every few minutes) and only a
    subset of holders are genuinely new arrivals in a given pass.
    """
    window_s = insider_cfg.cluster_window_hours * 3600.0
    by_side: dict = {}
    for h in holders:
        n_resolved = h.n_trades or 0
        if n_resolved < insider_cfg.thin_history_n and h.first_seen_at is not None:
            by_side.setdefault(h.side, []).append(h)

    others_needed = max(1, insider_cfg.cluster_full_at - 1)
    factors = {}
    for group in by_side.values():
        # Sort once per side, then find each holder's co-arrival count with
        # two bisects -- O(k log k) total instead of the all-pairs O(k^2)
        # comparison this used to do. A real insider-cluster signature (many
        # thin/new holders arriving together) is exactly the case that used
        # to make this the single most expensive step in a backtest checkpoint.
        sorted_ts = sorted(h.first_seen_at for h in group)
        for h in group:
            lo = bisect.bisect_left(sorted_ts, h.first_seen_at - window_s)
            hi = bisect.bisect_right(sorted_ts, h.first_seen_at + window_s)
            co_arrivals = (hi - lo) - 1  # exclude h's own timestamp
            factors[(h.wallet, h.side)] = min(1.0, co_arrivals / others_needed)
    return factors


def compute_insider_score(
    holder: Holder,
    now_ts: Optional[int] = None,
    cluster_factor: Optional[float] = None,
    end_date: Optional[str] = None,
    insider_cfg: cfg.InsiderConfig = cfg.INSIDER,
) -> Optional[InsiderFlag]:
    """
    None if the position is too small to bother flagging, or if we don't
    actually have a confirmed read on this wallet's history yet. That second
    check matters a lot: both "still being fetched" and "Polymarket's API
    errored on this wallet" (see the market-maker wallet in the README/dev
    notes whose /closed-positions consistently 500s) present as n_trades=0 /
    tenure=unknown -- exactly what a *genuinely* new, thin-history wallet
    looks like. Scoring those as insiders would flag most of a freshly-opened
    market, or a wallet Polymarket's API can't serve, for no real reason.

    `cluster_factor` (from _compute_cluster_factors, computed once per market
    scan) and `end_date` (the market's resolution date) are optional inputs
    from the caller since they're market-level context this function doesn't
    have on its own. `insider_cfg` defaults to the live weights (config.INSIDER)
    but app.backtest_engine passes a per-run override built from the same
    dataclass, to test different weightings without touching production.
    """
    if holder.fetch_status != "ok":
        return None
    if holder.position_usd < insider_cfg.min_position_usd:
        return None
    now_ts = now_ts or int(time.time())
    n_resolved = holder.n_trades or 0

    days = tenure_days(holder, now_ts)
    age_f = _age_factor(days, n_resolved, insider_cfg)
    size_f = _size_factor(holder.position_usd, holder.avg_bet_usd, insider_cfg)
    thin_f = _thin_factor(n_resolved, insider_cfg)

    weighted = insider_cfg.w_age * age_f + insider_cfg.w_size * size_f + insider_cfg.w_thin * thin_f
    weight_total = insider_cfg.w_age + insider_cfg.w_size + insider_cfg.w_thin

    if cluster_factor is not None:
        weighted += insider_cfg.w_cluster * cluster_factor
        weight_total += insider_cfg.w_cluster

    winrate_f = None
    if holder.win_rate_weighted is not None and n_resolved >= insider_cfg.winrate_anomaly_min_n:
        winrate_f = _winrate_anomaly_factor(holder.win_rate_weighted, insider_cfg)
        weighted += insider_cfg.w_winrate * winrate_f
        weight_total += insider_cfg.w_winrate

    urgency_f = _urgency_factor(end_date, now_ts, insider_cfg)
    if urgency_f is not None:
        weighted += insider_cfg.w_urgency * urgency_f
        weight_total += insider_cfg.w_urgency

    score = 100.0 * weighted / weight_total if weight_total > 0 else 0.0

    reasons = []
    if n_resolved == 0:
        reasons.append("no observed Polymarket trading history")
    elif days is not None and days * 24.0 <= insider_cfg.new_wallet_hours_full:
        reasons.append(f"wallet active on Polymarket for only ~{days * 24:.0f} hours")
    elif days is not None and days < insider_cfg.new_wallet_days_zero:
        reasons.append(f"only active on Polymarket for ~{days:.0f} day{'s' if round(days) != 1 else ''}")
    size_multiple = (holder.position_usd / holder.avg_bet_usd) if holder.avg_bet_usd else None
    if size_multiple is not None and size_multiple >= insider_cfg.size_multiple_flag:
        reasons.append(f"position is ~{size_multiple:.0f}x this wallet's typical bet size")
    elif size_multiple is None and size_f > 0:
        # No personal baseline to compare against (typically n_resolved==0) --
        # the absolute $ size itself is what's driving the score component.
        reasons.append(f"large position (${holder.position_usd:,.0f}) from a wallet with no bet-size baseline")
    if n_resolved < insider_cfg.thin_history_n:
        reasons.append(f"only {n_resolved} resolved trade{'s' if n_resolved != 1 else ''} ever")
    if cluster_factor:
        reasons.append("other similarly new/thin wallets took the same side of this market around the same time")
    if winrate_f:
        reasons.append(f"unusually high win rate (~{holder.win_rate_weighted * 100:.0f}%) over {n_resolved} trades")
    if urgency_f:
        reasons.append("position surfaced with the market close to resolution")
    if not reasons:
        reasons.append(f"large position (${holder.position_usd:,.0f}) with no other flags")

    return InsiderFlag(
        wallet=holder.wallet, side=holder.side, position_usd=holder.position_usd,
        insider_score=round(score, 1), reasons=reasons, tenure_days=days,
        n_resolved=n_resolved, size_multiple=size_multiple,
        score_percentile=holder.score_percentile, tier=holder.tier,
        first_seen_at=holder.first_seen_at,
    )


# ---------------------------------------------------------------------------
# Persist + surface for the Insiders tab / poller
# ---------------------------------------------------------------------------

def scan_market(conn, condition_id: str, holders: list, now_ts: Optional[int] = None) -> list:
    """
    Score every holder of `condition_id`, upsert into insider_flags, and
    return the (InsiderFlag, condition_id) pairs that just crossed
    config.INSIDER.alert_threshold for the first time -- i.e. what a caller
    (app.poller) should actually send an alert for.
    """
    now_ts = now_ts or int(time.time())
    row = conn.execute("SELECT end_date FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
    end_date = row["end_date"] if row else None
    cluster_factors = _compute_cluster_factors(holders)

    newly_alertable = []
    for h in holders:
        flag = compute_insider_score(
            h, now_ts, cluster_factor=cluster_factors.get((h.wallet, h.side)), end_date=end_date,
        )
        if flag is None:
            continue
        if _is_market_maker_like(conn, h.wallet, condition_id, h.side):
            conn.execute(
                "DELETE FROM insider_flags WHERE condition_id=? AND wallet=? AND side=?",
                (condition_id, h.wallet, h.side),
            )
            continue
        existing = conn.execute(
            "SELECT alerted_at FROM insider_flags WHERE condition_id=? AND wallet=? AND side=?",
            (condition_id, h.wallet, h.side),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO insider_flags (condition_id, wallet, side, position_usd, insider_score,
                reasons, tenure_days, n_resolved, size_multiple, score_percentile, tier,
                first_seen_at, detected_at, alerted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(condition_id, wallet, side) DO UPDATE SET
                position_usd=excluded.position_usd, insider_score=excluded.insider_score,
                reasons=excluded.reasons, tenure_days=excluded.tenure_days,
                n_resolved=excluded.n_resolved, size_multiple=excluded.size_multiple,
                score_percentile=excluded.score_percentile, tier=excluded.tier,
                first_seen_at=excluded.first_seen_at, detected_at=excluded.detected_at
            """,
            (condition_id, h.wallet, h.side, flag.position_usd, flag.insider_score,
             json.dumps(flag.reasons), flag.tenure_days, flag.n_resolved, flag.size_multiple,
             flag.score_percentile, flag.tier, flag.first_seen_at, now_ts),
        )
        if flag.insider_score >= cfg.INSIDER.alert_threshold and (existing is None or existing["alerted_at"] is None):
            newly_alertable.append((flag, condition_id))
    return newly_alertable


def _is_market_maker_like(conn, wallet: str, condition_id: str, side: str, min_other_markets: int = 3) -> bool:
    """
    True if `wallet` holds a same-side position in `min_other_markets`+ OTHER
    markets belonging to the same event -- the classic multi-outcome
    "[side] on every candidate/outcome" arbitrage or market-making pattern
    (observed directly during development: a single wallet holding a >$190M
    symmetric NO position across every 2024 presidential-candidate market).
    That's structural liquidity provision, not informed betting on one
    outcome, so wallets matching it are excluded from insider flags entirely
    rather than just scored lower.
    """
    row = conn.execute("SELECT event_id FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
    event_id = row["event_id"] if row else None
    if not event_id:
        return False
    count = conn.execute(
        """
        SELECT COUNT(DISTINCT h.condition_id) FROM market_holders_cache h
        JOIN markets m ON m.condition_id = h.condition_id
        WHERE h.wallet = ? AND h.side = ? AND m.event_id = ? AND h.condition_id != ?
        """,
        (wallet, side, event_id, condition_id),
    ).fetchone()[0]
    return count >= min_other_markets


def mark_alerted(conn, condition_id: str, wallet: str, side: str, now_ts: Optional[int] = None):
    conn.execute(
        "UPDATE insider_flags SET alerted_at=? WHERE condition_id=? AND wallet=? AND side=?",
        (now_ts or int(time.time()), condition_id, wallet, side),
    )


@dataclass(frozen=True)
class MarketInsiderSummary:
    label: str            # "Insiders lean YES" | "...NO" | "Insider activity on both sides" | "No significant insider activity"
    conviction: float      # 0-100, direction-agnostic -- how much + how severe, not which side
    yes_flagged_usd: float
    no_flagged_usd: float
    yes_flag_count: int
    no_flag_count: int
    net_insider_flow: float  # signed, severity-weighted: yes - no
    top_flags: list           # up to 5 highest-severity-$ flags, as dicts


def summarize_insider_rows(rows: list) -> MarketInsiderSummary:
    """
    The pure roll-up math behind summarize_market(), pulled out so a caller
    with an in-memory list of flags -- app.backtest_engine's point-in-time
    replay, which must never touch the live insider_flags table -- can reuse
    the exact same aggregation §3.8-style logic instead of a parallel copy.

    `rows`: dicts (or sqlite3.Row) with side/position_usd/insider_score/wallet/
    reasons (already a list, not a JSON string)/score_percentile/tier.
    """
    def _weighted(side):
        return sum(r["position_usd"] * (r["insider_score"] / 100.0) for r in rows if r["side"] == side)

    yes_w, no_w = _weighted("YES"), _weighted("NO")
    yes_raw = sum(r["position_usd"] for r in rows if r["side"] == "YES")
    no_raw = sum(r["position_usd"] for r in rows if r["side"] == "NO")
    yes_count = sum(1 for r in rows if r["side"] == "YES")
    no_count = sum(1 for r in rows if r["side"] == "NO")

    total_w = yes_w + no_w
    total_raw = yes_raw + no_raw
    net = yes_w - no_w

    top_flags = [
        {
            "wallet": r["wallet"], "side": r["side"], "position_usd": r["position_usd"],
            "insider_score": r["insider_score"], "reasons": r["reasons"],
            "score_percentile": r["score_percentile"], "tier": r["tier"],
            "first_seen_at": r["first_seen_at"],
        }
        for r in sorted(rows, key=lambda r: r["insider_score"], reverse=True)[:5]
    ]

    if total_w <= 0:
        return MarketInsiderSummary(
            "No significant insider activity", 0.0, yes_raw, no_raw, yes_count, no_count, 0.0, top_flags,
        )

    imbalance = abs(net) / total_w  # 0 = perfectly split, 1 = all one side
    avg_severity = total_w / total_raw if total_raw > 0 else 0.0  # $-weighted avg score/100
    scale = min(1.0, total_raw / cfg.INSIDER.activity_scale_usd)
    conviction = round(100.0 * (0.5 * imbalance + 0.3 * avg_severity + 0.2 * scale), 1)

    if imbalance < cfg.INSIDER.balanced_imbalance:
        label = "Insider activity on both sides"
    elif net > 0:
        label = "Insiders lean YES"
    else:
        label = "Insiders lean NO"

    return MarketInsiderSummary(
        label, conviction, yes_raw, no_raw, yes_count, no_count, net, top_flags,
    )


def summarize_market(conn, condition_id: str) -> MarketInsiderSummary:
    """
    Roll every persisted insider_flags row for one market up into a single
    headline read, the same way §3.8's overall_market_verdict rolls up
    per-holder Smart Scores. Each flag counts toward its side weighted by
    position_usd * (insider_score/100) -- a high-severity flag pulls harder
    than a marginal one, and low-severity rows (most wallets that clear the
    $ floor but aren't otherwise suspicious) barely move the needle.
    """
    rows = conn.execute(
        "SELECT * FROM insider_flags WHERE condition_id=? ORDER BY insider_score DESC", (condition_id,)
    ).fetchall()
    dict_rows = [
        {**dict(r), "reasons": json.loads(r["reasons"]) if r["reasons"] else []}
        for r in rows
    ]
    return summarize_insider_rows(dict_rows)


def list_flags(conn, limit: int = 100, min_score: float = 0.0, condition_id: Optional[str] = None) -> list:
    """
    Flagged positions, most-suspicious/most-recent first. Pass `condition_id`
    to get every flag for one market (e.g. after a user clicks into it from
    list_markets_with_flags); omit it for a global feed across all markets.
    """
    query = """
        SELECT f.*, m.question, m.slug, m.category
        FROM insider_flags f
        LEFT JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.insider_score >= ?
    """
    params: list = [min_score]
    if condition_id:
        query += " AND f.condition_id = ?"
        params.append(condition_id)
    query += " ORDER BY f.detected_at DESC, f.insider_score DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        out.append(d)
    return out


def current_price_yes_from_json(outcome_prices_json: Optional[str]) -> Optional[float]:
    try:
        return float(json.loads(outcome_prices_json)[0])
    except (TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError):
        return None


def is_near_resolved(price_yes: Optional[float], margin: float = cfg.INSIDER.near_resolved_price_margin) -> bool:
    """
    True if the market is already priced within `margin` of $0 or $1 -- i.e.
    effectively resolved. A flagged wallet there might still be a genuine
    insider, but there's no mispricing left to act on, so these clutter the
    Insiders tab with markets nobody can do anything about.
    """
    if price_yes is None:
        return False
    return price_yes <= margin or price_yes >= (1.0 - margin)


def list_markets_with_flags(
    conn, min_score: float = 0.0, limit: int = 100, include_near_resolved: bool = False,
) -> list:
    """
    One row per market that has any insider_flags row -- so the Insiders tab
    lists each market once (click through for the wallet-level detail via
    list_flags(condition_id=...)) instead of repeating a market for every
    flagged wallet on it. Reuses summarize_market's roll-up, the same one
    shown on the market detail page, so the two views agree.

    Markets already priced within near_resolved_price_margin of $0/$1 are
    dropped by default (see is_near_resolved) -- there's no room left to
    trade an edge there, so surfacing them is just noise. `limit` applies
    after that filter.
    """
    rows = conn.execute(
        """
        SELECT f.condition_id, MAX(f.detected_at) last_detected_at, MAX(f.insider_score) max_score,
               m.question, m.slug, m.category, m.outcome_prices, m.volume, m.volume_24hr, m.liquidity
        FROM insider_flags f
        LEFT JOIN markets m ON m.condition_id = f.condition_id
        GROUP BY f.condition_id
        HAVING max_score >= ?
        ORDER BY last_detected_at DESC
        """,
        (min_score,),
    ).fetchall()
    out = []
    for r in rows:
        price_yes = current_price_yes_from_json(r["outcome_prices"])
        if not include_near_resolved and is_near_resolved(price_yes):
            continue
        s = summarize_market(conn, r["condition_id"])
        out.append({
            "condition_id": r["condition_id"], "question": r["question"], "slug": r["slug"],
            "category": r["category"], "last_detected_at": r["last_detected_at"],
            "label": s.label, "conviction": s.conviction,
            "yes_flagged_usd": s.yes_flagged_usd, "no_flagged_usd": s.no_flagged_usd,
            "yes_flag_count": s.yes_flag_count, "no_flag_count": s.no_flag_count,
            "current_price_yes": price_yes,
            "volume": r["volume"], "volume_24hr": r["volume_24hr"], "liquidity": r["liquidity"],
        })
        if len(out) >= limit:
            break
    return out
