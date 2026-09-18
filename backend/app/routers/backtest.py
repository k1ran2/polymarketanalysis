from __future__ import annotations

import dataclasses
import json
import random
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import backtest_engine as engine
from .. import config as cfg
from ..polymarket_client import GammaClient, PolymarketAPIError

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

_gamma = GammaClient()

# Gamma's own top-level tags (id confirmed live via GET /tags/slug/<slug>) --
# there's no clean "list the top-level categories" endpoint (the plain /tags
# list is thousands of granular, uncurated micro-tags), so this is a
# hand-picked set matching Polymarket's own site nav, not something derived
# from the API. `tag_id` genuinely filters /markets server-side (confirmed
# live); the earlier `tag_slug`/`tag` param names are silently ignored.
CATEGORY_TAG_IDS = {
    "Politics": 2,
    "Sports": 1,
    "Crypto": 21,
    "Culture": 596,
    "Business": 107,
    "Science": 74,
    "Tech": 1401,
    "World": 101970,
}


def _market_dict(m: dict, category: Optional[str] = None) -> Optional[dict]:
    if not m.get("closed"):
        return None
    try:
        outcome_prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
    except (TypeError, ValueError, json.JSONDecodeError):
        outcome_prices = None
    if category is None:
        events = m.get("events") or []
        if events:
            tags = events[0].get("tags") or []
            if tags:
                category = tags[0].get("label")
    return {
        "condition_id": m.get("conditionId"),
        "question": m.get("question"),
        "slug": m.get("slug"),
        "category": category,
        "volume": float(m.get("volumeNum") or m.get("volume") or 0.0),
        "end_date": m.get("endDate"),
        "resolved_yes": (float(outcome_prices[0]) >= 0.5) if outcome_prices else None,
    }


@router.get("/categories")
def get_categories():
    """The curated category dropdown options for the market picker (see CATEGORY_TAG_IDS)."""
    return {"categories": sorted(CATEGORY_TAG_IDS.keys())}


class MarketSelection(BaseModel):
    categories: list[str] = Field(default_factory=list)  # empty = all
    year_min: int = Field(default=2020, ge=2015, le=2035)
    year_max: int = Field(default=datetime.now(timezone.utc).year, ge=2015, le=2035)
    count: int = Field(default=30, ge=1, le=500)


# Floor only -- excludes markets with essentially no real trading (a handful
# of dust trades), not "small" markets in general. There is deliberately no
# ceiling: the wallet-fetch reduction in app.backtest_engine
# (smart_score_top_n_holders) is what keeps a huge market's backtest fast, so
# market SELECTION doesn't need to dodge big markets to stay fast -- and it
# shouldn't, since real trading skews toward the liquid, high-volume markets
# this app is meant to be used on.
_MIN_REAL_VOLUME = 500.0


def _gamma_call_with_retry(gamma, retries: int = 2, **kwargs) -> list:
    """gamma.get_resolved_markets(**kwargs), retried a couple more times on
    top of RateLimitedClient's own 3x-with-backoff -- see the call site in
    auto_select_markets for why a whole market selection is worth a little
    extra insurance against one flaky category."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            return gamma.get_resolved_markets(**kwargs)
        except PolymarketAPIError as e:
            last_error = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def auto_select_markets(gamma, selection: MarketSelection) -> list[dict]:
    """
    Picks up to `selection.count` resolved markets spread across categories,
    years, AND market size within the requested window -- for "test this
    strategy broadly," not "test this one market I already have in mind."

    Deliberately samples both the top of each category's volume range (the
    big, liquid markets real trading would actually use) and a broader swath
    below it (smaller markets), rather than only the highest-volume ones --
    a pure top-by-volume pick would give a strategy's win rate against
    exactly the 2024 US election and nothing else. Also spreads across
    resolution year within each slice, so one era can't dominate either.
    """
    cats = selection.categories or list(CATEGORY_TAG_IDS.keys())
    per_category = max(1, -(-selection.count // len(cats)))  # ceil
    date_kwargs = {"end_date_min": f"{selection.year_min}-01-01", "end_date_max": f"{selection.year_max}-12-31"}

    picked, seen = [], set()
    for cat in cats:
        tag_id = CATEGORY_TAG_IDS.get(cat)
        # Top-by-volume (the big, liquid markets) + a broad ascending sweep
        # (smaller-but-real markets, floored to skip dust) -- combining both
        # is what gives genuine size diversity instead of leaning either way.
        # A whole market-selection call makes up to 16 of these back to back
        # (2 per category); Gamma has been observed 500-ing intermittently
        # under that burst even though the exact same call in isolation
        # succeeds -- RateLimitedClient already retries 3x on a 5xx, but a
        # short category-level retry on top of that is cheap insurance
        # against one bad category losing the whole selection.
        top = _gamma_call_with_retry(gamma, limit=100, order="volumeNum", ascending=False, tag_id=tag_id, **date_kwargs)
        small = _gamma_call_with_retry(gamma, limit=200, order="volumeNum", ascending=True, tag_id=tag_id, **date_kwargs)

        by_year: dict = {}
        for m in top + small:
            d = _market_dict(m, category=cat)
            if not d or not d["condition_id"] or d["condition_id"] in seen:
                continue
            if d["volume"] < _MIN_REAL_VOLUME:
                continue
            by_year.setdefault((d["end_date"] or "")[:4], []).append(d)

        years = list(by_year.keys())
        random.shuffle(years)
        taken = 0
        while taken < per_category and years:
            for yr in list(years):
                bucket = by_year[yr]
                if not bucket:
                    years.remove(yr)
                    continue
                d = bucket.pop(random.randrange(len(bucket)))
                seen.add(d["condition_id"])
                picked.append(d)
                taken += 1
                if taken >= per_category:
                    break

    random.shuffle(picked)
    return picked[:selection.count]


@router.get("/markets")
def browse_resolved_markets(
    limit: int = Query(30, le=100),
    offset: int = Query(0, ge=0),
    order: str = Query("volumeNum", pattern="^(volumeNum|endDate|startDate)$"),
    search: Optional[str] = Query(None, description="Free-text search across ALL resolved markets, not just the current page"),
    category: Optional[str] = Query(None, description="One of GET /api/backtest/categories"),
    end_date_min: Optional[str] = Query(None, description="ISO date, e.g. 2024-01-01 -- only markets scheduled to resolve on/after this"),
    end_date_max: Optional[str] = Query(None, description="ISO date -- only markets scheduled to resolve on/before this"),
):
    """
    Resolved markets to choose from for a backtest -- a live Gamma query, not
    our own cache (that only fills in once a market is actually backtested).

    Without `search`: the volume-sorted /markets listing, paginated, with
    `category`/`end_date_min`/`end_date_max` applied server-side by Gamma.
    With `search`: Gamma's /public-search (events_status=closed) instead --
    real full-text search across every resolved market, unlike filtering
    substrings over just the current volume-sorted page (which would
    silently miss anything not near the top by volume) -- `category` is then
    applied client-side over the search results (public-search has no
    category/date param of its own), and date range is not applied here.
    """
    tag_id = CATEGORY_TAG_IDS.get(category) if category else None
    out = []
    if search:
        result = _gamma.public_search(search, events_status="closed", limit_per_type=limit)
        for ev in result.get("events", []):
            ev_category = None
            tags = ev.get("tags") or []
            if tags:
                ev_category = tags[0].get("label")
            if category and ev_category != category:
                continue
            for m in ev.get("markets", []):
                d = _market_dict(m, category=ev_category)
                if d and d["condition_id"]:
                    out.append(d)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
    else:
        raw = _gamma.get_resolved_markets(
            limit=limit, offset=offset, order=order, tag_id=tag_id,
            end_date_min=end_date_min, end_date_max=end_date_max,
        )
        for m in raw:
            d = _market_dict(m, category=category)
            if d:
                out.append(d)
    return {"markets": out}


class VerdictOverrides(BaseModel):
    """
    A subset of app.config.VerdictConfig -- unset (None) fields fall through
    to the live default (see backtest_engine.build_verdict_cfg). Only the
    handful of thresholds that actually decide "Strong Buy" are exposed here,
    not every field on VerdictConfig (e.g. the conviction-score blend
    weights) -- these are the ones a real tuning pass would touch first.
    """
    smart_threshold: Optional[float] = Field(default=None, ge=0, le=100)
    dumb_threshold: Optional[float] = Field(default=None, ge=0, le=100)
    strong_buy_gap_pts: Optional[float] = Field(default=None, ge=0, le=100)
    strong_buy_smart_share: Optional[float] = Field(default=None, ge=0, le=1)
    strong_buy_max_unrated_share: Optional[float] = Field(default=None, ge=0, le=1)


class InsiderOverrides(BaseModel):
    """A subset of app.config.InsiderConfig -- the 6 factor weights plus the
    $ floor for a position to be considered at all. See VerdictOverrides."""
    w_age: Optional[float] = Field(default=None, ge=0, le=1)
    w_size: Optional[float] = Field(default=None, ge=0, le=1)
    w_thin: Optional[float] = Field(default=None, ge=0, le=1)
    w_cluster: Optional[float] = Field(default=None, ge=0, le=1)
    w_winrate: Optional[float] = Field(default=None, ge=0, le=1)
    w_urgency: Optional[float] = Field(default=None, ge=0, le=1)
    min_position_usd: Optional[float] = Field(default=None, ge=0)


class RunRequest(BaseModel):
    strategy: str = Field(pattern="^(smart_money|insider)$")
    # Exactly one of these two: condition_ids for "I picked these markets
    # myself", market_selection for "pick a spread of markets for me".
    condition_ids: Optional[list[str]] = Field(default=None, max_length=500)
    market_selection: Optional[MarketSelection] = None
    stake_usd: float = Field(default=100.0, gt=0)
    min_conviction: Optional[float] = Field(default=None, ge=0, le=100)
    min_signal_strength: str = Field(default="strong_buy", pattern="^(strong_buy|lean|fade)$")
    # A signal recommending a side already priced at/above this is skipped --
    # no real edge left, just a lopsided risk/reward (win a few cents, risk
    # the whole stake). Default matches BacktestConfig.max_no_edge_entry_price.
    max_no_edge_entry_price: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    # Per-run weight tuning -- never touches the live cfg.VERDICT/cfg.INSIDER
    # singletons every other tab reads (see backtest_engine.build_verdict_cfg
    # / build_insider_cfg). Stored in the run's params either way, so a past
    # run always shows exactly which weights produced its result.
    verdict_overrides: Optional[VerdictOverrides] = None
    insider_overrides: Optional[InsiderOverrides] = None


@router.get("/strategy-defaults")
def get_strategy_defaults():
    """
    The live default weights/thresholds for both strategies, plus a concise
    description of each formula -- single source of truth for the Backtest
    page's methodology section AND the starting values for its editable
    weight form, so neither can drift out of sync with what config.py
    actually specifies.
    """
    v = cfg.VERDICT
    i = cfg.INSIDER
    return {
        "smart_money": {
            "description": (
                "Ranks every holder's own resolved-trade history into a 0-100 Smart Score, then rolls "
                "$-weighted scores up per side. \"Strong Buy {side}\" requires ALL of: smart money "
                "(score >= smart_threshold) holds >= strong_buy_smart_share of RATED $ on one side, the "
                "market's price is off from what that smart money implies by >= strong_buy_gap_pts "
                "percentage points, and unrated $ is <= strong_buy_max_unrated_share of the total. Below "
                "that bar but still net-leaning smart money gives \"Lean {side}\" instead. Separately, "
                "\"Fade {side}\" fires when UNSKILLED money (score < dumb_threshold) clearly dominates one "
                "side with no offsetting smart money -- a contrarian bet against bad traders, not a "
                "confirmation from good ones."
            ),
            "params": dataclasses.asdict(v),
            "param_labels": {
                "smart_threshold": "Smart Score at/above this counts as \"smart money\"",
                "dumb_threshold": "Smart Score below this counts as \"dumb money\"",
                "strong_buy_gap_pts": "Min price/smart-implied gap (percentage points) for Strong Buy",
                "strong_buy_smart_share": "Min share of rated $ smart money must hold for Strong Buy (0-1)",
                "strong_buy_max_unrated_share": "Max share of $ that can be Unrated for Strong Buy (0-1)",
            },
        },
        "insider": {
            "description": (
                "Six 0-1 factors -- age (how new the wallet is), size (position vs. the wallet's own "
                "typical bet), thin (how little resolved history they have), cluster (other similarly "
                "new/thin wallets arriving on the same side around the same time), winrate (anomalously "
                "high win rate for a wallet WITH enough history to trust it), urgency (position surfaced "
                "close to resolution) -- combined as a weighted average, renormalized over only the "
                "factors that actually apply to a given wallet (e.g. winrate is skipped entirely below "
                "winrate_anomaly_min_n trades, not counted as a reassuring zero). "
                "score = 100 * sum(weight_i * factor_i) / sum(weight_i, over applicable factors)."
            ),
            "params": dataclasses.asdict(i),
            "param_labels": {
                "w_age": "Weight: how new the wallet is",
                "w_size": "Weight: position size vs. the wallet's own typical bet",
                "w_thin": "Weight: how little resolved trade history the wallet has",
                "w_cluster": "Weight: other new/thin wallets arriving on the same side together",
                "w_winrate": "Weight: anomalously high win rate (only counted with enough trade history)",
                "w_urgency": "Weight: position surfaced close to the market's resolution",
                "min_position_usd": "Minimum position size ($) to be considered at all",
            },
        },
        "general": {
            "description": (
                "Applies to both strategies. A signal recommending a side already priced at or above "
                "max_no_edge_entry_price is skipped -- the market already agrees almost entirely, so a "
                "win pays out only a few cents on the dollar while a loss costs the full stake. A cheap "
                "entry (price near $0) is the opposite case -- a correct contrarian call there pays out a "
                "large multiple -- and is never filtered."
            ),
            "params": {"max_no_edge_entry_price": cfg.BACKTEST.max_no_edge_entry_price},
            "param_labels": {
                "max_no_edge_entry_price": "Skip a signal if its entry price is at/above this (0.5-1.0)",
            },
        },
    }


@router.post("/runs")
def create_run(req: RunRequest):
    """
    Kick off a backtest in the background (mirrors POST /api/insiders/scan-now):
    returns immediately with a run_id to poll. Only one run goes at a time --
    a request while one is already running hands back that run's id instead
    of starting a second (a lot of network I/O either way).

    Market selection is resolved to a concrete condition_id list right here
    (a few fast Gamma reads), before the run is created, so progress_total
    is known from the start and the exact set backtested is recorded in the
    run's stored params either way -- auto-picked or hand-picked look
    identical to everything downstream.
    """
    if req.condition_ids:
        condition_ids = req.condition_ids
    elif req.market_selection:
        picked = auto_select_markets(_gamma, req.market_selection)
        if not picked:
            raise HTTPException(
                400, "No resolved markets matched that category/date/size combination -- "
                     "try a wider date range, more categories, or a larger size preset.",
            )
        condition_ids = [m["condition_id"] for m in picked]
    else:
        raise HTTPException(400, "Provide either condition_ids or market_selection.")

    params = req.model_dump()
    params["condition_ids"] = condition_ids
    if params["min_conviction"] is None:
        params["min_conviction"] = 50.0 if req.strategy == "smart_money" else 70.0
    result = engine.start_backtest(params)
    return result


@router.get("/runs")
def get_runs(limit: int = Query(20, le=100)):
    return {"runs": engine.list_runs(limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    run = engine.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


# ---------------------------------------------------------------------------
# Weight search -- random search over a parameter space, tuned on a TRAIN
# slice of the selected markets and graded, once, on a held-out TEST slice
# it never saw while tuning. Which override model applies is implied by
# strategy (smart_money -> VerdictOverrides, insider -> InsiderOverrides) --
# there's no strategy for which BOTH configs are in play at once, so a
# separate config_kind field would just be one more way to send a
# self-contradictory request.
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    strategy: str = Field(pattern="^(smart_money|insider)$")
    condition_ids: Optional[list[str]] = Field(default=None, max_length=500)
    market_selection: Optional[MarketSelection] = None
    # Fraction of the selected markets held out as TEST -- never touched by
    # any of the n_iterations tuning runs, only by the one final evaluation
    # of the best config the search finds. Small selections still keep at
    # least one test market (see train_test_split_markets).
    test_fraction: float = Field(default=0.3, gt=0.0, lt=1.0)
    seed: Optional[int] = None
    n_iterations: int = Field(default=15, ge=1, le=100)
    # {param_name: [min, max]} -- param_name must be a field of
    # VerdictOverrides (strategy=smart_money) or InsiderOverrides
    # (strategy=insider); each iteration samples one value per param
    # uniformly from its range.
    param_space: dict[str, tuple[float, float]]
    stake_usd: float = Field(default=100.0, gt=0)
    min_conviction: Optional[float] = Field(default=None, ge=0, le=100)
    min_signal_strength: str = Field(default="strong_buy", pattern="^(strong_buy|lean|fade)$")
    max_no_edge_entry_price: Optional[float] = Field(default=None, ge=0.5, le=1.0)


@router.post("/search")
def create_search(req: SearchRequest):
    """
    Kick off a weight search in the background (mirrors POST /runs): returns
    immediately with a search_id to poll. Shares the same single-flight slot
    as a normal run -- a search is n_iterations+1 full backtests back to
    back, so it's exactly as network-heavy as a run and shouldn't overlap
    with one.
    """
    if req.condition_ids:
        condition_ids = req.condition_ids
    elif req.market_selection:
        picked = auto_select_markets(_gamma, req.market_selection)
        if not picked:
            raise HTTPException(
                400, "No resolved markets matched that category/date/size combination -- "
                     "try a wider date range or more categories.",
            )
        condition_ids = [m["condition_id"] for m in picked]
    else:
        raise HTTPException(400, "Provide either condition_ids or market_selection.")

    if len(condition_ids) < 4:
        raise HTTPException(400, "Need at least 4 markets so a train/test split means something.")

    allowed = set(VerdictOverrides.model_fields) if req.strategy == "smart_money" else set(InsiderOverrides.model_fields)
    unknown = sorted(set(req.param_space) - allowed)
    if unknown:
        raise HTTPException(400, f"Unknown parameter(s) for strategy={req.strategy}: {unknown}")
    if not req.param_space:
        raise HTTPException(400, "param_space must name at least one parameter to search over.")

    train_ids, test_ids = engine.train_test_split_markets(condition_ids, test_fraction=req.test_fraction, seed=req.seed)

    base_params = {
        "stake_usd": req.stake_usd,
        "min_conviction": req.min_conviction if req.min_conviction is not None else (
            50.0 if req.strategy == "smart_money" else 70.0
        ),
        "min_signal_strength": req.min_signal_strength,
        "max_no_edge_entry_price": req.max_no_edge_entry_price,
    }
    params = {
        "strategy": req.strategy,
        "config_kind": "verdict" if req.strategy == "smart_money" else "insider",
        "param_space": {k: list(v) for k, v in req.param_space.items()},
        "n_iterations": req.n_iterations,
        "train_condition_ids": train_ids,
        "test_condition_ids": test_ids,
        "base_params": base_params,
        "seed": req.seed,
        "all_condition_ids": condition_ids,
    }
    return engine.start_search(params)


@router.get("/searches")
def get_searches(limit: int = Query(20, le=100)):
    return {"searches": engine.list_searches(limit=limit)}


@router.get("/search/{search_id}")
def get_search(search_id: str):
    search = engine.get_search(search_id)
    if search is None:
        raise HTTPException(404, "Search not found")
    return search
