"""
Shared directional-signal logic for BOTH the backtest engine and the live
trading watcher -- extracted from app.backtest_engine so "the bot trades the
backtested strategy" is a property of the code (one implementation, two
callers) rather than something that has to be kept in sync by hand.

Everything here is pure: given a list of Holder objects (already built from
whatever data source -- live DB-backed holders for the watcher, point-in-time
reconstructed holders for the backtest) and a params dict, return a signal or
None. No I/O, no knowledge of checkpoints, trades, or persistence.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Optional

from . import config as cfg
from .insiders import _compute_cluster_factors, compute_insider_score, summarize_insider_rows
from .scoring import overall_market_verdict

_STRONG_BUY_RE = re.compile(r"^Strong Buy (YES|NO)$")
_LEAN_RE = re.compile(r"^Lean (YES|NO)$")
_FADE_RE = re.compile(r"^Fade (YES|NO)")


def parse_verdict_side(label: str) -> Optional[tuple]:
    """label -> (side, strength) from the same MarketVerdict labels the live UI shows, or None if non-directional."""
    m = _STRONG_BUY_RE.match(label)
    if m:
        return m.group(1), "strong_buy"
    m = _LEAN_RE.match(label)
    if m:
        return m.group(1), "lean"
    m = _FADE_RE.match(label)
    if m:
        fade_target = m.group(1)
        return ("NO" if fade_target == "YES" else "YES"), "fade"
    return None


STRENGTH_RANK = {"strong_buy": 3, "lean": 2, "fade": 1}


def build_verdict_cfg(overrides: Optional[dict]) -> cfg.VerdictConfig:
    """A per-call VerdictConfig with `overrides` applied over the live defaults --
    never mutates cfg.VERDICT itself, so this can't affect the live Markets tab.
    None values are dropped first (a Pydantic request model reports every
    field, set or not, as None when unset) so an unset field falls through to
    the default instead of overwriting it with a literal None."""
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    if not overrides:
        return cfg.VERDICT
    return dataclasses.replace(cfg.VERDICT, **overrides)


def build_insider_cfg(overrides: Optional[dict]) -> cfg.InsiderConfig:
    """Same idea as build_verdict_cfg, for the insider detector's weights."""
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    if not overrides:
        return cfg.INSIDER
    return dataclasses.replace(cfg.INSIDER, **overrides)


def smart_money_signal(holders: list, current_price_yes: float, params: dict) -> Optional[tuple]:
    """(side, conviction, strength) if the Smart Money verdict clears params'
    min_signal_strength/min_conviction gates, else None."""
    verdict_cfg = build_verdict_cfg(params.get("verdict_overrides"))
    verdict = overall_market_verdict(holders, current_price_yes, verdict_cfg=verdict_cfg)
    parsed = parse_verdict_side(verdict.label)
    if parsed is None:
        return None
    side, strength = parsed
    min_strength = params.get("min_signal_strength", "strong_buy")
    if STRENGTH_RANK[strength] < STRENGTH_RANK.get(min_strength, 3):
        return None
    min_conviction = float(params.get("min_conviction", 0.0))
    if verdict.conviction < min_conviction:
        return None
    return side, verdict.conviction, strength


def insider_signal(holders: list, t: int, end_date_iso: Optional[str], params: dict) -> Optional[tuple]:
    """(side, conviction, "insider") if the insider-score summary clears
    params' min_conviction gate and leans a direction, else None.

    NOTE (live vs backtest divergence, disclosed): this applies insider
    scoring to exactly the holders it's given -- the live watcher's holder
    source additionally excludes market-maker-like wallets (see
    insiders._is_market_maker_like) before calling this, while the
    backtest's point-in-time holder reconstruction does not (no live
    `markets`/event data to check that against, historically). Live insider
    signals are therefore slightly stricter than backtested ones -- already
    documented in backtest_engine's module docstring.
    """
    insider_cfg = build_insider_cfg(params.get("insider_overrides"))
    cluster_factors = _compute_cluster_factors(holders, insider_cfg)
    flags = []
    for h in holders:
        flag = compute_insider_score(
            h, t, cluster_factor=cluster_factors.get((h.wallet, h.side)), end_date=end_date_iso,
            insider_cfg=insider_cfg,
        )
        if flag is None:
            continue
        flags.append({
            "wallet": flag.wallet, "side": flag.side, "position_usd": flag.position_usd,
            "insider_score": flag.insider_score, "reasons": flag.reasons,
            "score_percentile": flag.score_percentile, "tier": flag.tier,
            "first_seen_at": flag.first_seen_at,
        })
    if not flags:
        return None
    summary = summarize_insider_rows(flags)
    min_conviction = float(params.get("min_conviction", insider_cfg.alert_threshold))
    if summary.conviction < min_conviction:
        return None
    if summary.label == "Insiders lean YES":
        return "YES", summary.conviction, "insider"
    if summary.label == "Insiders lean NO":
        return "NO", summary.conviction, "insider"
    return None  # "both sides" / "no significant activity" isn't a directional trade
