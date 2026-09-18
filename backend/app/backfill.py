"""
Seed script (§7, §8).

The primary Phase 1 flow is on-demand: pick a market, score its holders now
(app.market_analysis). This script just bootstraps the trader cache so the
pool isn't empty on a cold start -- it backfills the ~30 /v1/leaderboard
wallets (known sharps), and can optionally pre-analyze the top few markets so
the first page load already has data.

    python -m app.backfill                    # seed the ~30 leaderboard wallets
    python -m app.backfill --analyze-markets 5 # + pre-analyze the top 5 markets
    python -m app.backfill --offline           # use app/fixtures/*.json instead of live APIs
    python -m app.backfill --force             # ignore the 24h wallet-freshness cache
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

from . import config as cfg
from . import db
from .scoring import ClosedPosition, parse_closed_position

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Data source abstraction: live Polymarket APIs, or offline JSON fixtures.
# Both expose the same four methods so backfill logic doesn't care which.
# ---------------------------------------------------------------------------

class LiveDataSource:
    def __init__(self):
        from .polymarket_client import GammaClient, DataAPIClient
        self.gamma = GammaClient()
        # Patient retry/backoff for backfill specifically (not search's
        # fast-fail defaults) -- see BackfillConfig.data_api_max_retries /
        # data_api_backoff_cap for why.
        self.data_api = DataAPIClient(
            max_retries=cfg.BACKFILL.data_api_max_retries,
            backoff_cap=cfg.BACKFILL.data_api_backoff_cap,
        )

    def get_top_events(self, limit: int) -> list:
        return self.gamma.get_active_events(limit=limit, order="volume24hr")

    def get_leaderboard(self, limit: int) -> list:
        return self.data_api.get_leaderboard(limit=limit)

    def get_holders(self, condition_id: str, limit: int = 100) -> list:
        return self.data_api.get_holders(condition_id, limit=limit)

    def get_closed_positions_page(self, wallet: str, limit: int, offset: int) -> list:
        return self.data_api.get_closed_positions(
            wallet, limit=limit, offset=offset, sort_by=cfg.BACKFILL.closed_positions_sort_by
        )


class FixtureDataSource:
    """
    Reads real Polymarket API responses that were captured (via WebFetch,
    since this sandbox's direct egress to *.polymarket.com is blocked by org
    policy) into app/fixtures/*.json. Every file's content is genuine live
    data pulled from the documented endpoints in §2, just relayed through a
    tool that isn't subject to this container's outbound network allowlist.
    """
    def __init__(self):
        with open(FIXTURES_DIR / "events.json") as f:
            self._events = json.load(f)
        with open(FIXTURES_DIR / "leaderboard.json") as f:
            self._leaderboard = json.load(f)
        with open(FIXTURES_DIR / "holders_by_market.json") as f:
            self._holders = json.load(f)  # {condition_id: [holders...]}
        with open(FIXTURES_DIR / "closed_positions_by_wallet.json") as f:
            self._closed_positions = json.load(f)  # {wallet: [positions...]}

    def get_top_events(self, limit: int) -> list:
        return self._events[:limit]

    def get_leaderboard(self, limit: int) -> list:
        return self._leaderboard[:limit]

    def get_holders(self, condition_id: str, limit: int = 100) -> list:
        return self._holders.get(condition_id, [])[:limit]

    def get_closed_positions_page(self, wallet: str, limit: int, offset: int) -> list:
        all_pos = self._closed_positions.get(wallet, [])
        return all_pos[offset:offset + limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_markets_from_events(events: list) -> list[dict]:
    """Flatten Gamma events -> list of market dicts with parsed outcomes/prices/tokenIds."""
    markets = []
    for ev in events:
        for m in ev.get("markets", []):
            try:
                outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
                outcome_prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
                clob_token_ids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            condition_id = m.get("conditionId")
            if not condition_id or not outcomes or not outcome_prices:
                continue
            # An event can be active while individual sub-markets inside it have
            # already resolved (e.g. Game 1 of a best-of-5 is settled while the
            # series is live). §1 is about *active* markets only, so drop any
            # sub-market that is closed or no longer accepting orders --
            # otherwise the market list fills up with resolved games priced 0/1.
            if m.get("closed") or not m.get("active"):
                continue
            category = None
            tags = ev.get("tags") or []
            if tags:
                category = tags[0].get("label")
            markets.append({
                "condition_id": condition_id,
                "event_id": ev.get("id"),
                "question": m.get("question"),
                "slug": m.get("slug"),
                "category": category,
                "outcomes": outcomes,
                "outcome_prices": outcome_prices,
                "clob_token_ids": clob_token_ids,
                "volume": m.get("volumeNum") or m.get("volume") or 0.0,
                # Market-level only -- NOT the event's pooled figure. A prop
                # sub-market with no 24h volume of its own must not inherit its
                # event's millions and jump to the top of the list.
                "volume_24hr": m.get("volume24hr") or 0.0,
                # Prefer market-level liquidity (needs include_liquidity=true on
                # the Gamma call); fall back to the event's pooled figure.
                "liquidity": m.get("liquidityNum") or m.get("liquidity") or ev.get("liquidity") or 0.0,
                "end_date": m.get("endDate") or m.get("endDateIso"),
                "active": bool(m.get("active")),
                "closed": bool(m.get("closed")),
            })
    return markets


def _side_for_outcome_index(market: dict, idx: int) -> str:
    outcomes = market["outcomes"]
    if idx < len(outcomes) and str(outcomes[idx]).strip().lower() == "no":
        return "NO"
    return "YES" if idx == 0 else "NO"


def _price_for_outcome_index(market: dict, idx: int) -> float:
    try:
        return float(market["outcome_prices"][idx])
    except (IndexError, TypeError, ValueError):
        return 0.5


def upsert_market(conn, market: dict, now_ts: int):
    conn.execute(
        """
        INSERT INTO markets (condition_id, event_id, question, slug, category, outcomes,
                              outcome_prices, clob_token_ids, volume, volume_24hr, liquidity,
                              end_date, active, closed, updated_at)
        VALUES (:condition_id, :event_id, :question, :slug, :category, :outcomes,
                :outcome_prices, :clob_token_ids, :volume, :volume_24hr, :liquidity,
                :end_date, :active, :closed, :updated_at)
        ON CONFLICT(condition_id) DO UPDATE SET
            event_id=excluded.event_id, question=excluded.question, slug=excluded.slug,
            category=excluded.category, outcomes=excluded.outcomes,
            outcome_prices=excluded.outcome_prices, clob_token_ids=excluded.clob_token_ids,
            volume=excluded.volume, volume_24hr=excluded.volume_24hr, liquidity=excluded.liquidity,
            end_date=excluded.end_date,
            active=excluded.active, closed=excluded.closed, updated_at=excluded.updated_at
        """,
        {
            "condition_id": market["condition_id"], "event_id": market["event_id"],
            "question": market["question"], "slug": market["slug"], "category": market["category"],
            "outcomes": json.dumps(market["outcomes"]), "outcome_prices": json.dumps(market["outcome_prices"]),
            "clob_token_ids": json.dumps(market["clob_token_ids"]), "volume": market["volume"],
            "volume_24hr": market["volume_24hr"], "liquidity": market["liquidity"],
            "end_date": market.get("end_date"),
            "active": int(market["active"]), "closed": int(market["closed"]), "updated_at": now_ts,
        },
    )


def upsert_holder(conn, condition_id: str, wallet: str, side: str, position_usd: float, source: str, now_ts: int):
    # first_seen_at is set only on the INSERT branch (absent from DO UPDATE SET),
    # so an existing holder's "when we first noticed them" is never overwritten --
    # that's what insiders._cluster_factor keys off of.
    conn.execute(
        """
        INSERT INTO market_holders_cache (condition_id, wallet, side, position_usd, source, fetched_at, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(condition_id, wallet, side) DO UPDATE SET
            position_usd=excluded.position_usd, source=excluded.source, fetched_at=excluded.fetched_at
        """,
        (condition_id, wallet, side, position_usd, source, now_ts, now_ts),
    )


def fetch_all_closed_positions(source, wallet: str, now_ts: int) -> list[ClosedPosition]:
    positions = []
    offset = 0
    page_size = cfg.BACKFILL.closed_positions_page_size
    while True:
        raw_page = source.get_closed_positions_page(wallet, page_size, offset)
        if not raw_page:
            break
        for raw in raw_page:
            pos = parse_closed_position(raw)
            if pos is not None:
                positions.append(pos)
        if len(raw_page) < page_size:
            break
        offset += page_size
        if offset >= cfg.BACKFILL.max_closed_positions_per_wallet:
            break
    return positions


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def run_backfill(offline: bool = False, force: bool = False,
                  leaderboard_limit: Optional[int] = None,
                  analyze_markets: int = 0,
                  lookback_days: Optional[int] = None,
                  extra_wallets: Optional[list[str]] = None):
    """
    Bootstrap the trader cache: score the ~30 leaderboard wallets, refresh the
    market-list metadata, and (if analyze_markets > 0) pre-analyze the top N
    markets so the first page load already has verdicts.
    """
    from .pipeline import (
        fetch_wallet_positions, wallets_needing_refresh, score_and_persist_wallets,
        record_fetch_failures,
    )
    from .market_list import refresh_market_list

    now_ts = int(time.time())
    lookback_days = lookback_days or cfg.TREND.default_lookback_days
    leaderboard_limit = leaderboard_limit or cfg.BACKFILL.seed_leaderboard_limit

    db.init_db()
    source = FixtureDataSource() if offline else LiveDataSource()
    print(f"[seed] mode={'offline/fixtures' if offline else 'live'} "
          f"leaderboard_limit={leaderboard_limit} analyze_markets={analyze_markets}")

    with db.db_session() as conn:
        # 1. Leaderboard wallets -> trader cache.
        try:
            lb = source.get_leaderboard(leaderboard_limit)
        except Exception as e:
            print(f"[seed] leaderboard fetch failed: {e}")
            lb = []
        wallets = [r["proxyWallet"] for r in lb if r.get("proxyWallet")]
        if extra_wallets:
            wallets += list(extra_wallets)
        to_fetch = wallets_needing_refresh(conn, wallets, now_ts, force=force)
        print(f"[seed] {len(wallets)} leaderboard wallets, {len(to_fetch)} need fetching")
        if to_fetch:
            positions, errors = fetch_wallet_positions(source, to_fetch, now_ts, progress=print)
            score_and_persist_wallets(conn, positions, now_ts, lookback_days, progress=print)
            record_fetch_failures(conn, errors, now_ts)

        # 2. Market-list metadata (one Gamma call, no holders).
        try:
            n = refresh_market_list(conn, force=True, source=source)
            print(f"[seed] refreshed metadata for {n} markets")
        except Exception as e:
            print(f"[seed] market-list refresh failed: {e}")

    # 3. Optionally pre-analyze the top markets (each in its own session).
    analyzed = 0
    if analyze_markets:
        from .market_analysis import analyze_market
        with db.db_session() as conn:
            cids = [r["condition_id"] for r in conn.execute(
                "SELECT condition_id FROM markets ORDER BY volume DESC LIMIT ?", (analyze_markets,)
            ).fetchall()]
        for cid in cids:
            try:
                analyze_market(cid, offline=offline, lookback_days=lookback_days, progress=print)
                analyzed += 1
            except Exception as e:
                print(f"[seed] analyze {cid[:12]} failed: {e}")

    with db.db_session() as conn:
        n_traders = conn.execute("SELECT COUNT(*) c FROM traders").fetchone()["c"]
        n_rated = conn.execute("SELECT COUNT(*) c FROM traders WHERE percentile IS NOT NULL").fetchone()["c"]
        n_markets = conn.execute("SELECT COUNT(*) c FROM markets").fetchone()["c"]
    print(f"[seed] done: {n_traders} traders ({n_rated} rated), {n_markets} markets, {analyzed} pre-analyzed")
    return {"traders": n_traders, "rated": n_rated, "markets": n_markets, "analyzed": analyzed}


def main():
    parser = argparse.ArgumentParser(description="Polymarket Smart Money Dashboard - seed script")
    parser.add_argument("--offline", action="store_true", help="Use local JSON fixtures instead of live APIs")
    parser.add_argument("--force", action="store_true", help="Ignore the 24h wallet-freshness cache")
    parser.add_argument("--leaderboard-limit", type=int, default=None)
    parser.add_argument("--analyze-markets", type=int, default=0,
                        help="Also pre-analyze the top N markets by volume")
    parser.add_argument("--lookback-days", type=int, default=None)
    args = parser.parse_args()

    run_backfill(
        offline=args.offline, force=args.force,
        leaderboard_limit=args.leaderboard_limit,
        analyze_markets=args.analyze_markets,
        lookback_days=args.lookback_days,
    )


if __name__ == "__main__":
    main()
