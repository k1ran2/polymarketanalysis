"""
On-demand market analysis -- the primary Phase 1 flow.

Instead of a big upfront backfill, the user picks a market and this runs:

  1. Make sure we have the market's metadata (Gamma lookup if not).
  2. Refresh its holder list + position sizes live via the /holders fast path
     (§6) -- the part that changes minute to minute. Cheap (~1 call).
  3. Score any holder wallets we don't already have a fresh Smart Score for
     (app.pipeline). Each wallet is scored independently, so this is just
     "fetch history, score, upsert" -- no population step.
  4. Return the full §3.5/§3.6/§3.7/§3.8 detail payload (app.market_service).

To keep the UI snappy (§6: "result in seconds ... silently upgraded"), the
router runs step 3 in the background: the first response comes back right away
with whatever scores are already cached and `analyzing: true` + a pending
count; the frontend polls until `analyzing: false`.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from . import config as cfg
from . import db
from .backfill import (
    LiveDataSource, FixtureDataSource, _extract_markets_from_events,
    _side_for_outcome_index, _price_for_outcome_index, upsert_market, upsert_holder,
)
from .pipeline import (
    fetch_wallet_positions, wallets_needing_refresh, score_and_persist_wallets,
    record_fetch_failures,
)
from .polymarket_client import PolymarketAPIError
from .market_service import build_market_detail, get_holders_for_market
from .insiders import scan_market as scan_market_for_insiders, mark_alerted
from .alerts import send_insider_alert

# Per-market lock: serialize holder-snapshot refreshes for one market.
_locks_guard = threading.Lock()
_market_locks: dict[str, threading.Lock] = {}
# condition_ids with a background scoring job currently running.
_bg_guard = threading.Lock()
_bg_running: set[str] = set()


def _lock_for(condition_id: str) -> threading.Lock:
    with _locks_guard:
        return _market_locks.setdefault(condition_id, threading.Lock())


def _json_or_none(s):
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def _p(progress, msg):
    (progress or print)(msg)


def _make_source(offline: bool):
    return FixtureDataSource() if offline else LiveDataSource()


# ---------------------------------------------------------------------------
# Holder snapshot (fast, live)
# ---------------------------------------------------------------------------

def _holder_rows_from_fast_path(source, market: dict, per_side: int) -> list[tuple]:
    """[(wallet, side, position_usd), ...] from the /holders fast path."""
    groups = source.get_holders(market["condition_id"], limit=per_side)
    rows: list[tuple] = []
    if not groups or not isinstance(groups, list) or not isinstance(groups[0], dict):
        return rows
    if "holders" not in groups[0]:
        return rows
    for group in groups:
        token = group.get("token")
        try:
            idx = market["clob_token_ids"].index(token)
        except (ValueError, AttributeError):
            idx = 0
        side = _side_for_outcome_index(market, idx)
        price = _price_for_outcome_index(market, idx)
        for h in group.get("holders", []):
            wallet = h.get("proxyWallet")
            amount = h.get("amount")
            if not wallet or amount is None:
                continue
            position_usd = float(amount) * price
            if position_usd < 1.0:
                continue  # dust (§10)
            rows.append((wallet, side, position_usd))
    return rows


def _lookup_market_on_gamma(source, condition_id: str) -> Optional[dict]:
    """Find one market's metadata via Gamma when it isn't in our DB yet (rare fallback)."""
    gamma = getattr(source, "gamma", None)
    if gamma is None:
        return None
    for params in ({"condition_ids": condition_id, "limit": 1},
                   {"clob_token_ids": condition_id, "limit": 1}):
        try:
            raw = gamma.get("/markets", params)
        except Exception:
            continue
        if isinstance(raw, list) and raw:
            wrapped = [{"markets": raw, "tags": [], "id": raw[0].get("eventId")}]
            markets = _extract_markets_from_events(wrapped)
            if markets:
                return markets[0]
    return None


def _market_dict(market_row) -> dict:
    return {
        "condition_id": market_row["condition_id"],
        "clob_token_ids": _json_or_none(market_row["clob_token_ids"]),
        "outcomes": _json_or_none(market_row["outcomes"]),
        "outcome_prices": _json_or_none(market_row["outcome_prices"]),
        "volume": market_row["volume"],
    }


def ensure_market_and_snapshot(
    conn, source, condition_id: str, now_ts: int, force_snapshot: bool = False
) -> Optional[dict]:
    """
    Make sure `markets` has this market and `market_holders_cache` has a
    reasonably fresh holder snapshot. Returns the (possibly refreshed) market
    row, or None if the market can't be found anywhere.
    """
    with _lock_for(condition_id):
        market_row = conn.execute(
            "SELECT * FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if market_row is None:
            market = _lookup_market_on_gamma(source, condition_id)
            if market is None:
                return None
            upsert_market(conn, market, now_ts)
            market_row = conn.execute(
                "SELECT * FROM markets WHERE condition_id=?", (condition_id,)
            ).fetchone()

        snap = conn.execute(
            "SELECT COUNT(*) c, MAX(fetched_at) f FROM market_holders_cache WHERE condition_id=?",
            (condition_id,),
        ).fetchone()
        fresh = snap["f"] is not None and (now_ts - snap["f"]) < cfg.ON_DEMAND.market_fresh_seconds
        if fresh and not force_snapshot:
            return market_row

        try:
            holder_rows = _holder_rows_from_fast_path(
                source, _market_dict(market_row), cfg.ON_DEMAND.analyze_holders_per_side
            )
        except PolymarketAPIError:
            holder_rows = []

        if holder_rows:
            # Upsert (preserving first_seen_at for wallets that persist -- see
            # insiders._cluster_factor) rather than delete-then-reinsert, then
            # drop only the wallets that actually exited the position.
            current_keys = {(w, s) for w, s, _u in holder_rows}
            existing_keys = {
                (r["wallet"], r["side"]) for r in conn.execute(
                    "SELECT wallet, side FROM market_holders_cache WHERE condition_id=?", (condition_id,)
                ).fetchall()
            }
            for wallet, side, usd in holder_rows:
                upsert_holder(conn, condition_id, wallet, side, usd, "holders_fast_path", now_ts)
            for wallet, side in existing_keys - current_keys:
                conn.execute(
                    "DELETE FROM market_holders_cache WHERE condition_id=? AND wallet=? AND side=?",
                    (condition_id, wallet, side),
                )
            conn.commit()
        elif snap["c"] == 0:
            # nothing live and nothing cached
            return market_row  # caller will see holder_count 0
        return market_row


def pending_wallets(conn, condition_id: str, now_ts: int) -> list[str]:
    """Holder wallets of this market that still need a (fresh) Smart Score."""
    rows = conn.execute(
        "SELECT DISTINCT wallet FROM market_holders_cache WHERE condition_id=?", (condition_id,)
    ).fetchall()
    wallets = [r["wallet"] for r in rows]
    return wallets_needing_refresh(conn, wallets, now_ts)


# ---------------------------------------------------------------------------
# Insider scan (fast, no network calls) -- safe to run inline on every open
# ---------------------------------------------------------------------------

def scan_insiders_for_market(condition_id: str, now_ts: Optional[int] = None, progress=None) -> int:
    """
    Score the market's *current* holder snapshot for insider patterns
    (app.insiders) and fire an alert for anything newly crossing the
    threshold. Pure DB read + in-memory scoring -- no Polymarket calls -- so
    this is cheap enough to run synchronously on every market open, not just
    after a (slow) wallet re-fetch.
    """
    now_ts = now_ts or int(time.time())
    with db.db_session() as conn:
        holders = get_holders_for_market(conn, condition_id)
        alertable = scan_market_for_insiders(conn, condition_id, holders, now_ts)
        question = None
        if alertable:
            row = conn.execute("SELECT question FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
            question = row["question"] if row else None

    for flag, _cid in alertable:
        send_insider_alert(flag, condition_id, question)
        with db.db_session() as conn:
            mark_alerted(conn, condition_id, flag.wallet, flag.side, now_ts)

    if alertable:
        _p(progress, f"[analyze:{condition_id[:10]}] {len(alertable)} new insider alert(s)")
    return len(alertable)


# ---------------------------------------------------------------------------
# Scoring (slow) -- run inline or in a background thread
# ---------------------------------------------------------------------------

def score_market_wallets(
    condition_id: str,
    offline: bool = False,
    force: bool = False,
    lookback_days: Optional[int] = None,
    progress=None,
) -> dict:
    """
    Fetch + score every holder wallet of `condition_id` that needs it, then
    rescan for insider-pattern positions now that scores/tenure are current.
    Own DB session.
    """
    now_ts = int(time.time())
    lookback_days = lookback_days or cfg.TREND.default_lookback_days
    source = _make_source(offline)
    summary = {"scored": 0}
    with db.db_session() as conn:
        to_fetch = pending_wallets(conn, condition_id, now_ts) if not force else [
            r["wallet"] for r in conn.execute(
                "SELECT DISTINCT wallet FROM market_holders_cache WHERE condition_id=?", (condition_id,)
            ).fetchall()
        ]
        if to_fetch:
            _p(progress, f"[analyze:{condition_id[:10]}] scoring {len(to_fetch)} wallets")
            positions, errors = fetch_wallet_positions(
                source, to_fetch, now_ts,
                concurrency=cfg.ON_DEMAND.analyze_concurrency, progress=progress,
            )
            if positions:
                summary = score_and_persist_wallets(conn, positions, now_ts, lookback_days, progress=progress)
            if errors:
                record_fetch_failures(conn, errors, now_ts)

    scan_insiders_for_market(condition_id, now_ts, progress=progress)
    return summary


def start_background_scoring(condition_id: str, offline: bool = False, lookback_days: Optional[int] = None):
    """Kick score_market_wallets in a daemon thread, unless one is already running for this market."""
    with _bg_guard:
        if condition_id in _bg_running:
            return False
        _bg_running.add(condition_id)

    def _run():
        try:
            score_market_wallets(condition_id, offline=offline, lookback_days=lookback_days)
        except Exception as e:  # noqa: BLE001 - background thread, log and move on
            print(f"[analyze:{condition_id[:10]}] background scoring failed: {e}")
        finally:
            with _bg_guard:
                _bg_running.discard(condition_id)

    threading.Thread(target=_run, name=f"score-{condition_id[:10]}", daemon=True).start()
    return True


def is_scoring(condition_id: str) -> bool:
    with _bg_guard:
        return condition_id in _bg_running


# ---------------------------------------------------------------------------
# Full synchronous analyze (used by the seed script / tests / ?wait=1)
# ---------------------------------------------------------------------------

def analyze_market(
    condition_id: str,
    offline: bool = False,
    force: bool = False,
    lookback_days: Optional[int] = None,
    progress=None,
) -> dict:
    """
    Refresh holders + score every pending wallet inline, then return the detail
    payload. Raises KeyError if the market can't be found, PolymarketAPIError if
    there are no holders live or cached.
    """
    now_ts = int(time.time())
    source = _make_source(offline)
    with db.db_session() as conn:
        market_row = ensure_market_and_snapshot(conn, source, condition_id, now_ts, force_snapshot=force)
        if market_row is None:
            raise KeyError(condition_id)
        have = conn.execute(
            "SELECT COUNT(*) c FROM market_holders_cache WHERE condition_id=?", (condition_id,)
        ).fetchone()["c"]
        if not have:
            raise PolymarketAPIError(f"no holders for {condition_id} (live fetch failed, nothing cached)")

    score_market_wallets(condition_id, offline=offline, force=force, lookback_days=lookback_days, progress=progress)

    with db.db_session() as conn:
        market_row = conn.execute("SELECT * FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
        detail = build_market_detail(conn, market_row)
        detail["analyzed_at"] = now_ts
        detail["analyzing"] = False
        detail["pending"] = 0
        return detail
