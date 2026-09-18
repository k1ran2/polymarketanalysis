"""
Shared read-side logic: turns SQLite rows (markets + market_holders_cache +
traders) into the scoring.Holder objects app.scoring's pure functions expect,
and assembles the full market-detail payload (§3.5/§3.6/§3.7/§3.8).

This is the only layer that touches the DB for reads — routers call these
functions, never write raw SQL of their own, so the "filters query
already-materialized tables, they never trigger recomputation" rule (§4) is
enforced in one place.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Optional

from . import config as cfg
from . import db
from .scoring import (
    Holder, usd_weighted_score, net_smart_flow, smart_implied_probability,
    undervalued_gap, signed_distribution_stats, whale_concentration,
    overall_market_verdict, signed_score, score_weight, usd_weighted_tenure_days,
    tenure_days,
)
from .insiders import summarize_market as summarize_market_insiders


def _current_price_for_market(market_row) -> float:
    """Current YES price, used as the 'current market price' in §3.5/§3.8."""
    try:
        prices = json.loads(market_row["outcome_prices"])
        return float(prices[0])
    except (TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError):
        return 0.5


def get_holders_for_market(conn, condition_id: str) -> list[Holder]:
    rows = conn.execute(
        """
        SELECT h.wallet, h.side, h.position_usd, h.first_seen_at,
               t.percentile, t.tier, t.n, t.volume, t.win_rate_weighted,
               t.first_active_ts, t.fetch_status
        FROM market_holders_cache h
        LEFT JOIN traders t ON t.wallet = h.wallet
        WHERE h.condition_id = ?
        """,
        (condition_id,),
    ).fetchall()
    holders = []
    for r in rows:
        tier = r["tier"] if r["tier"] is not None else "Unrated"
        percentile = r["percentile"] if tier != "Unrated" else None
        n = r["n"]
        avg_bet = (r["volume"] / n) if n and r["volume"] is not None else None
        holders.append(Holder(
            wallet=r["wallet"], side=r["side"], position_usd=r["position_usd"],
            score_percentile=percentile, tier=tier, n_trades=n,
            first_active_ts=r["first_active_ts"],
            fetch_status=r["fetch_status"] if r["fetch_status"] is not None else "never_fetched",
            avg_bet_usd=avg_bet,
            win_rate_weighted=r["win_rate_weighted"],
            first_seen_at=r["first_seen_at"],
        ))
    return holders


def market_row_to_dict(row, staleness_seconds: Optional[int] = None) -> dict:
    return {
        "condition_id": row["condition_id"],
        "question": row["question"],
        "slug": row["slug"],
        "category": row["category"],
        "outcomes": json.loads(row["outcomes"]) if row["outcomes"] else None,
        "outcome_prices": json.loads(row["outcome_prices"]) if row["outcome_prices"] else None,
        "volume": row["volume"],
        "volume_24hr": row["volume_24hr"],
        "liquidity": row["liquidity"],
        "active": bool(row["active"]),
        "closed": bool(row["closed"]),
        "updated_at": row["updated_at"],
        "stale": staleness_seconds is not None and staleness_seconds > 3600,
        "staleness_seconds": staleness_seconds,
    }


def _verdict_dict(verdict) -> dict:
    return {
        "label": verdict.label,
        "conviction": verdict.conviction,
        "smart_volume_share": verdict.smart_volume_share,
        "dumb_volume_share": verdict.dumb_volume_share,
        "unrated_volume_share": verdict.unrated_volume_share,
        "net_smart_flow": verdict.net_smart_flow,
        "undervalued_gap": verdict.undervalued_gap,
    }


def build_market_summary(conn, market_row) -> dict:
    """One row for the market list page: metadata + §3.8 verdict (headline column)."""
    condition_id = market_row["condition_id"]
    holders = get_holders_for_market(conn, condition_id)
    now_ts = int(time.time())
    staleness = now_ts - (market_row["updated_at"] or now_ts)

    out = market_row_to_dict(market_row, staleness)
    rated = [h for h in holders if h.score_percentile is not None]
    out["holder_count"] = len(holders)
    out["rated_holder_count"] = len(rated)
    out["analyzed"] = len(holders) > 0
    # A verdict needs holder analysis; markets not yet opened just show metadata.
    out["verdict"] = _verdict_dict(overall_market_verdict(holders, _current_price_for_market(market_row))) \
        if holders else None
    return out


def build_market_detail(conn, market_row) -> dict:
    """Full market detail payload: holder table + §3.5/§3.6/§3.7/§3.8."""
    condition_id = market_row["condition_id"]
    holders = get_holders_for_market(conn, condition_id)
    current_price = _current_price_for_market(market_row)

    yes_holders = [h for h in holders if h.side == "YES"]
    no_holders = [h for h in holders if h.side == "NO"]

    yes_sip_component = usd_weighted_score(yes_holders)
    no_sip_component = usd_weighted_score(no_holders)
    sip = smart_implied_probability(holders)
    gap = undervalued_gap(sip, current_price)
    nsf = net_smart_flow(holders)

    dist = signed_distribution_stats(holders)
    wc_yes = whale_concentration(yes_holders)
    wc_no = whale_concentration(no_holders)
    verdict = overall_market_verdict(holders, current_price)

    now_ts = int(time.time())

    holder_table = sorted(
        [
            {
                "wallet": h.wallet, "side": h.side, "position_usd": h.position_usd,
                "score_percentile": h.score_percentile, "tier": h.tier,
                "signed_score": signed_score(h),
                "tenure_days": tenure_days(h, now_ts),
                "fetch_status": h.fetch_status,
            }
            for h in holders
        ],
        key=lambda x: x["position_usd"], reverse=True,
    )

    staleness = now_ts - (market_row["updated_at"] or now_ts)

    out = market_row_to_dict(market_row, staleness)
    out["current_price_yes"] = current_price
    out["aggregation"] = {
        "usd_weighted_score_yes": yes_sip_component,
        "usd_weighted_score_no": no_sip_component,
        "net_smart_flow": nsf,
        "smart_implied_probability": sip,
        "undervalued_gap": gap,
        # $-weighted avg days since each side's holders' earliest observed
        # trade -- None if no holder on that side has a known first trade yet.
        "avg_tenure_days_yes": usd_weighted_tenure_days(yes_holders, now_ts),
        "avg_tenure_days_no": usd_weighted_tenure_days(no_holders, now_ts),
    }
    out["distribution"] = {
        "weighted_mean": dist.weighted_mean, "weighted_stdev": dist.weighted_stdev,
        "skewness": dist.skewness, "n": dist.n, "total_weight": dist.total_weight,
        "points": [
            {"signed_score": signed_score(h), "weight": score_weight(h),
             "position_usd": h.position_usd, "side": h.side}
            for h in holders if h.score_percentile is not None
        ],
    }
    out["whale_concentration"] = {
        "yes": {
            "buckets": [asdict(b) for b in wc_yes.buckets], "gini": wc_yes.gini,
            "avg_score_top1": wc_yes.avg_score_top1, "avg_score_rest": wc_yes.avg_score_rest,
        },
        "no": {
            "buckets": [asdict(b) for b in wc_no.buckets], "gini": wc_no.gini,
            "avg_score_top1": wc_no.avg_score_top1, "avg_score_rest": wc_no.avg_score_rest,
        },
    }
    out["verdict"] = _verdict_dict(verdict)
    out["holders"] = holder_table
    out["holder_count"] = len(holders)
    out["rated_holder_count"] = sum(1 for h in holders if h.score_percentile is not None)

    ins = summarize_market_insiders(conn, condition_id)
    out["insider_summary"] = {
        "label": ins.label, "conviction": ins.conviction,
        "yes_flagged_usd": ins.yes_flagged_usd, "no_flagged_usd": ins.no_flagged_usd,
        "yes_flag_count": ins.yes_flag_count, "no_flag_count": ins.no_flag_count,
        "net_insider_flow": ins.net_insider_flow, "top_flags": ins.top_flags,
    }
    return out
