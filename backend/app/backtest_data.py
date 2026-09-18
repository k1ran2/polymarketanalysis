"""
Fetch + cache the historical data app.backtest_engine needs to replay the
Smart Money / Insider signals against an already-RESOLVED market: the trade
tape (to reconstruct point-in-time holders), a price series (to price a
simulated entry), the resolution outcome (ground truth), and each involved
wallet's closed-position history (to compute their Smart Score as of a past
point in time, not today).

Every fetch here is idempotent and cache-first: a resolved market's trade
tape and outcome never change, so once synced they're never re-fetched. Only
a wallet's closed-position cache expires (BacktestConfig.wallet_fresh_days),
since new wallets keep trading after we first see them.
"""
from __future__ import annotations

import bisect
import json
import time
from dataclasses import dataclass
from typing import Optional

from . import config as cfg
from .polymarket_client import PolymarketAPIError
from .scoring import ClosedPosition


class BacktestDataError(Exception):
    pass


@dataclass(frozen=True)
class BacktestMarket:
    condition_id: str
    question: Optional[str]
    slug: Optional[str]
    category: Optional[str]
    clob_token_ids: list
    volume: float
    start_ts: Optional[int]
    end_ts: Optional[int]
    resolved_ts: Optional[int]
    resolved_yes: Optional[bool]


def _parse_iso_ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        from datetime import datetime
        s = s.replace("Z", "+00:00").strip()
        # Gamma's closedTime looks like "2026-09-09 14:25:04+00" -- normalize
        # the bare "+00" offset (datetime.fromisoformat wants "+00:00").
        if s.endswith("+00") and "+00:00" not in s:
            s = s[:-3] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Market metadata + resolution outcome
# ---------------------------------------------------------------------------

def sync_market_meta(conn, gamma, condition_id: str, now_ts: Optional[int] = None) -> Optional[BacktestMarket]:
    """
    Fetch + cache one market's metadata and resolution outcome from Gamma.
    Returns None (and caches nothing) if the market doesn't exist or hasn't
    resolved yet -- an unresolved market has no ground truth to grade a
    backtest against.
    """
    now_ts = now_ts or int(time.time())
    row = conn.execute(
        "SELECT * FROM backtest_market_meta WHERE condition_id=?", (condition_id,)
    ).fetchone()
    if row is not None:
        return _row_to_market(row)

    raw = gamma.get_market_by_condition_id(condition_id)
    if raw is None or not raw.get("closed"):
        return None

    try:
        outcome_prices = json.loads(raw["outcomePrices"]) if isinstance(raw.get("outcomePrices"), str) else raw.get("outcomePrices")
        clob_token_ids = json.loads(raw["clobTokenIds"]) if isinstance(raw.get("clobTokenIds"), str) else raw.get("clobTokenIds")
        resolved_yes = float(outcome_prices[0]) >= 0.5
    except (KeyError, ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None

    category = None
    events = raw.get("events") or []
    if events:
        tags = events[0].get("tags") or []
        if tags:
            category = tags[0].get("label")

    conn.execute(
        """
        INSERT INTO backtest_market_meta (condition_id, question, slug, category, clob_token_ids,
            volume, start_ts, end_ts, resolved_ts, resolved_yes, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(condition_id) DO NOTHING
        """,
        (
            condition_id, raw.get("question"), raw.get("slug"), category, json.dumps(clob_token_ids),
            float(raw.get("volumeNum") or raw.get("volume") or 0.0),
            _parse_iso_ts(raw.get("startDate")), _parse_iso_ts(raw.get("endDate")),
            _parse_iso_ts(raw.get("closedTime")), int(resolved_yes), now_ts,
        ),
    )
    row = conn.execute("SELECT * FROM backtest_market_meta WHERE condition_id=?", (condition_id,)).fetchone()
    return _row_to_market(row)


def _row_to_market(row) -> BacktestMarket:
    return BacktestMarket(
        condition_id=row["condition_id"], question=row["question"], slug=row["slug"],
        category=row["category"], clob_token_ids=json.loads(row["clob_token_ids"] or "[]"),
        volume=row["volume"] or 0.0, start_ts=row["start_ts"], end_ts=row["end_ts"],
        resolved_ts=row["resolved_ts"],
        resolved_yes=bool(row["resolved_yes"]) if row["resolved_yes"] is not None else None,
    )


# ---------------------------------------------------------------------------
# Trade tape (point-in-time holder reconstruction source)
# ---------------------------------------------------------------------------

def sync_market_trades(conn, data_api, condition_id: str, now_ts: Optional[int] = None) -> int:
    """
    Pull a resolved market's trade tape once and cache it -- it's static once
    resolved, so a market already marked trades_synced_at is never
    re-fetched. Returns the number of newly-inserted rows.

    LIMITATION, confirmed live: Data API's /trades hard-caps `offset` at
    10,000 (a 400 past that) -- trades come back newest-first, so a market
    with a bigger tape than that only yields its most-recent ~10k trades.
    Point-in-time reconstruction for checkpoints early in that market's life
    will then be missing real holders (backtest_market_meta.trades_capped
    marks this rather than silently under-counting).

    Commits after every page rather than once at the end, so hitting that
    cap (or any other mid-pagination failure) doesn't roll back pages
    already fetched -- those are real API calls worth keeping.
    """
    now_ts = now_ts or int(time.time())
    row = conn.execute(
        "SELECT trades_synced_at FROM backtest_market_meta WHERE condition_id=?", (condition_id,)
    ).fetchone()
    if row is not None and row["trades_synced_at"] is not None:
        return 0

    inserted = 0
    offset = 0
    capped = False
    page_size = cfg.BACKTEST.trades_page_size
    for _ in range(cfg.BACKTEST.max_trade_pages_per_market):
        try:
            page = data_api.get_trades(condition_id, limit=page_size, offset=offset)
        except PolymarketAPIError as e:
            if "offset" in str(e).lower():
                capped = True
                break
            raise
        if not page:
            break
        for raw in page:
            try:
                wallet = raw["proxyWallet"]
                side = raw["side"]
                outcome = "YES" if raw.get("outcome") == "Yes" else "NO"
                price = float(raw["price"])
                size = float(raw["size"])
                timestamp = int(raw["timestamp"])
                tx_hash = raw.get("transactionHash")
            except (KeyError, ValueError, TypeError):
                continue
            if side not in ("BUY", "SELL"):
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO backtest_market_trades
                    (condition_id, wallet, side, outcome, price, size, timestamp, tx_hash)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (condition_id, wallet, side, outcome, price, size, timestamp, tx_hash),
            )
            inserted += cur.rowcount
        conn.commit()
        if len(page) < page_size:
            break
        offset += page_size
    else:
        capped = True  # exhausted max_trade_pages_per_market without reaching the end

    conn.execute(
        "UPDATE backtest_market_meta SET trades_synced_at=?, trades_capped=? WHERE condition_id=?",
        (now_ts, int(capped), condition_id),
    )
    conn.commit()
    return inserted


def reconstruct_holders_at(conn, condition_id: str, as_of_ts: int) -> dict:
    """
    Net (wallet, outcome) -> shares held, from every cached trade up to
    as_of_ts (BUY adds, SELL subtracts). Requires sync_market_trades() to
    have run first. Also returns each wallet+outcome's earliest trade
    timestamp *in this market* (for the insider detector's clustering
    factor, which needs "when did this wallet first show up here").
    """
    rows = conn.execute(
        "SELECT wallet, outcome, side, size, timestamp FROM backtest_market_trades "
        "WHERE condition_id=? AND timestamp<=? ORDER BY timestamp ASC",
        (condition_id, as_of_ts),
    ).fetchall()
    shares: dict = {}       # (wallet, outcome) -> net shares
    first_seen: dict = {}   # (wallet, outcome) -> first trade ts in this market
    for r in rows:
        key = (r["wallet"], r["outcome"])
        delta = r["size"] if r["side"] == "BUY" else -r["size"]
        shares[key] = shares.get(key, 0.0) + delta
        if key not in first_seen:
            first_seen[key] = r["timestamp"]
    return {"shares": shares, "first_seen": first_seen}


# ---------------------------------------------------------------------------
# Price history (entry pricing)
# ---------------------------------------------------------------------------

_MAX_PRICE_HISTORY_SPAN_SECONDS = 14 * 86400  # CLOB confirmed live: exactly 15 days is the max startTs/endTs span; 14 is a safe margin


def sync_price_history(conn, clob, condition_id: str, clob_token_ids: list,
                        start_ts: int, end_ts: int, now_ts: Optional[int] = None) -> int:
    """
    `start_ts`/`end_ts` should bound the market's real life (its start_ts and
    resolved_ts/end_ts) -- CLOB's /prices-history silently returns an empty
    series for an older market when asked for `interval=max` (confirmed
    live), so explicit bounds are required, not optional tuning.

    CLOB also hard-caps how long a single startTs/endTs window can be (a
    400 past ~15 days, confirmed live, independent of fidelity) -- a
    long-lived market's price history is fetched in
    _MAX_PRICE_HISTORY_SPAN_SECONDS-sized chunks and unioned, not one call.
    """
    now_ts = now_ts or int(time.time())
    row = conn.execute(
        "SELECT prices_synced_at FROM backtest_market_meta WHERE condition_id=?", (condition_id,)
    ).fetchone()
    if row is not None and row["prices_synced_at"] is not None:
        return 0

    inserted = 0
    # A little padding either side so a checkpoint right at market open/close
    # still has a same-side sample within price_lookback_hours.
    pad = int(cfg.BACKTEST.price_lookback_hours * 3600)
    window_start, window_end = start_ts - pad, end_ts + pad
    for token_id in clob_token_ids or []:
        chunk_start = window_start
        while chunk_start < window_end:
            chunk_end = min(chunk_start + _MAX_PRICE_HISTORY_SPAN_SECONDS, window_end)
            history = clob.get_price_history(token_id, chunk_start, chunk_end)
            for point in history:
                try:
                    ts = int(point["t"])
                    price = float(point["p"])
                except (KeyError, ValueError, TypeError):
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO backtest_price_history (token_id, condition_id, timestamp, price) "
                    "VALUES (?,?,?,?)",
                    (token_id, condition_id, ts, price),
                )
                inserted += cur.rowcount
            conn.commit()
            chunk_start = chunk_end

    # Only mark synced if at least one token actually returned data -- an
    # empty result (e.g. a transient CLOB error) must be retried next time,
    # not cached as "no prices exist for this market" forever.
    if inserted > 0:
        conn.execute(
            "UPDATE backtest_market_meta SET prices_synced_at=? WHERE condition_id=?",
            (now_ts, condition_id),
        )
        conn.commit()
    return inserted


def price_at_or_before(conn, token_id: str, ts: int,
                        lookback_hours: float = cfg.BACKTEST.price_lookback_hours) -> Optional[float]:
    """
    The most recent cached price at/before `ts`, or the nearest one after it
    if nothing at/before falls within `lookback_hours` -- markets are sparse
    right after opening, and this makes an early checkpoint still pricable.
    None if nothing is close enough either way (checkpoint gets skipped).
    """
    window = int(lookback_hours * 3600)
    row = conn.execute(
        "SELECT price, timestamp FROM backtest_price_history "
        "WHERE token_id=? AND timestamp<=? ORDER BY timestamp DESC LIMIT 1",
        (token_id, ts),
    ).fetchone()
    if row is not None and (ts - row["timestamp"]) <= window:
        return row["price"]

    row = conn.execute(
        "SELECT price, timestamp FROM backtest_price_history "
        "WHERE token_id=? AND timestamp>? ORDER BY timestamp ASC LIMIT 1",
        (token_id, ts),
    ).fetchone()
    if row is not None and (row["timestamp"] - ts) <= window:
        return row["price"]
    return None


# ---------------------------------------------------------------------------
# Wallet closed-position history (point-in-time Smart Score inputs)
# ---------------------------------------------------------------------------

def wallets_needing_sync(conn, wallets: list, now_ts: Optional[int] = None) -> list:
    """Subset of `wallets` with no cached history, or one older than wallet_fresh_days."""
    now_ts = now_ts or int(time.time())
    uniq = list(dict.fromkeys(wallets))
    if not uniq:
        return uniq
    fresh = set()
    rows = conn.execute(
        "SELECT wallet, synced_at FROM backtest_wallet_sync WHERE wallet IN ({})".format(
            ",".join("?" * len(uniq))
        ),
        tuple(uniq),
    ).fetchall()
    ttl = cfg.BACKTEST.wallet_fresh_days * 86400
    for r in rows:
        if (now_ts - r["synced_at"]) < ttl:
            fresh.add(r["wallet"])
    return [w for w in uniq if w not in fresh]


def persist_wallet_positions(conn, positions_by_wallet: dict, errors: dict, now_ts: Optional[int] = None):
    """
    Write the result of pipeline.fetch_wallet_positions() (concurrent fetch
    across many wallets at once -- see app.backtest_engine) into the
    backtest wallet cache. Split from the fetch itself so backtest_engine can
    reuse the same concurrency/retry machinery the live pipeline uses instead
    of fetching one wallet at a time.
    """
    now_ts = now_ts or int(time.time())
    for wallet, positions in positions_by_wallet.items():
        conn.execute("DELETE FROM backtest_wallet_positions WHERE wallet=?", (wallet,))
        for p in positions:
            conn.execute(
                """
                INSERT OR IGNORE INTO backtest_wallet_positions
                    (wallet, condition_id, avg_price, total_bought, realized_pnl, cur_price, timestamp)
                VALUES (?,?,?,?,?,?,?)
                """,
                (p.wallet, p.condition_id, p.avg_price, p.total_bought, p.realized_pnl, p.cur_price, p.timestamp),
            )
        conn.execute(
            "INSERT INTO backtest_wallet_sync (wallet, synced_at, fetch_status) VALUES (?,?,'ok') "
            "ON CONFLICT(wallet) DO UPDATE SET synced_at=excluded.synced_at, fetch_status='ok'",
            (wallet, now_ts),
        )
    for wallet in errors:
        if wallet in positions_by_wallet:
            continue  # succeeded on the retry pass despite an earlier failure
        conn.execute(
            "INSERT INTO backtest_wallet_sync (wallet, synced_at, fetch_status) VALUES (?,?,'error') "
            "ON CONFLICT(wallet) DO UPDATE SET synced_at=excluded.synced_at, fetch_status='error'",
            (wallet, now_ts),
        )


def wallet_positions_asof(conn, wallet: str, as_of_ts: int) -> list:
    """A wallet's cached closed positions with timestamp <= as_of_ts -- the no-lookahead gate."""
    rows = conn.execute(
        "SELECT * FROM backtest_wallet_positions WHERE wallet=? AND timestamp<=?",
        (wallet, as_of_ts),
    ).fetchall()
    out = []
    for r in rows:
        out.append(ClosedPosition(
            wallet=wallet, condition_id=r["condition_id"], avg_price=r["avg_price"],
            total_bought=r["total_bought"], realized_pnl=r["realized_pnl"],
            cur_price=r["cur_price"], timestamp=r["timestamp"],
        ))
    return out


# ---------------------------------------------------------------------------
# Bulk loaders -- for a caller evaluating MANY point-in-time timestamps
# against the SAME market/wallet set (a backtest's checkpoint loop: up to
# max_checkpoints_per_market repeated point-in-time reads of a stable
# underlying dataset). The per-timestamp functions above each cost one SQL
# round-trip; calling them once per wallet per checkpoint is what actually
# dominated backtest wall-clock time (measured: ~75% of a checkpoint's cost
# was DB round-trips, not the trade-tape scan or the scoring math itself).
# These load everything the whole checkpoint loop will ever need ONCE, so
# each checkpoint only does in-memory bisects afterward. Kept alongside the
# per-timestamp functions (not replacing them) -- callers that only need one
# point in time (e.g. the live dashboard) have no reason to bulk-load.
# ---------------------------------------------------------------------------

def bulk_price_series(conn, token_id: str) -> list:
    """All cached (timestamp, price) points for one token, sorted ascending --
    pairs with price_at_or_before_series() for O(log n) bisect lookups
    instead of price_at_or_before()'s two SQL queries per call."""
    rows = conn.execute(
        "SELECT timestamp, price FROM backtest_price_history WHERE token_id=? ORDER BY timestamp ASC",
        (token_id,),
    ).fetchall()
    return [(r["timestamp"], r["price"]) for r in rows]


def price_at_or_before_series(
    series: list, ts: int, lookback_hours: float = cfg.BACKTEST.price_lookback_hours,
    timestamps: Optional[list] = None,
) -> Optional[float]:
    """Exactly price_at_or_before()'s semantics (nearest at/before within the
    lookback window, else nearest after within it, else None), against an
    already-loaded sorted (timestamp, price) list from bulk_price_series().

    Pass `timestamps` (the same list's timestamps, pre-extracted once) when
    calling this many times against the same series -- e.g. once per
    checkpoint in a backtest -- otherwise every call rebuilds that list from
    scratch, which for a market with a long price history turns "O(log n)
    bisect" back into an O(n) scan in practice (measured: this was the
    single biggest remaining cost after the initial bulk-loading pass)."""
    if not series:
        return None
    window = int(lookback_hours * 3600)
    if timestamps is None:
        timestamps = [t for t, _ in series]
    idx = bisect.bisect_right(timestamps, ts) - 1
    if idx >= 0:
        t_at, p_at = series[idx]
        if (ts - t_at) <= window:
            return p_at
    if idx + 1 < len(series):
        t_after, p_after = series[idx + 1]
        if (t_after - ts) <= window:
            return p_after
    return None


def bulk_wallet_positions(conn, wallets: list, as_of_ts: Optional[int] = None) -> dict:
    """{wallet: [ClosedPosition, ...]} sorted by timestamp ascending, for
    every wallet in `wallets`, in ONE query -- pairs with a bisect on each
    wallet's list to get the no-lookahead prefix as-of any timestamp,
    instead of one wallet_positions_asof() query per wallet per checkpoint.

    `as_of_ts`, when given, excludes any position closed after it -- a
    backtest's caller passes its LAST checkpoint here, since no checkpoint
    can ever need a position that closed later (see bisect_right against
    each wallet's prefix in _holders_at_fast). This isn't just skipping
    unreachable rows in Python: a wallet's cache keeps accumulating new
    closed positions long after any given historical market resolved
    (measured on this project's real DB: 84-94% of a wallet's cached rows
    postdate an older market's last checkpoint), and the WHERE clause turns
    that into an index range restriction (backtest_wallet_positions has a
    (wallet, timestamp) index), so SQLite skips those rows' B-tree pages
    entirely rather than fetching-then-discarding them. Optional and
    default-None so any other caller (or a wallet_positions_asof-style
    single lookup with no timestamp anchor) is unaffected."""
    uniq = list(dict.fromkeys(wallets))
    out: dict = {w: [] for w in uniq}
    if not uniq:
        return out
    if as_of_ts is not None:
        query = (
            "SELECT * FROM backtest_wallet_positions WHERE wallet IN ({}) AND timestamp<=? "
            "ORDER BY wallet, timestamp ASC".format(",".join("?" * len(uniq)))
        )
        args = tuple(uniq) + (as_of_ts,)
    else:
        query = "SELECT * FROM backtest_wallet_positions WHERE wallet IN ({}) ORDER BY wallet, timestamp ASC".format(
            ",".join("?" * len(uniq))
        )
        args = tuple(uniq)
    rows = conn.execute(query, args).fetchall()
    for r in rows:
        out[r["wallet"]].append(ClosedPosition(
            wallet=r["wallet"], condition_id=r["condition_id"], avg_price=r["avg_price"],
            total_bought=r["total_bought"], realized_pnl=r["realized_pnl"],
            cur_price=r["cur_price"], timestamp=r["timestamp"],
        ))
    return out


def bulk_fetch_status(conn, wallets: list) -> dict:
    """{wallet: fetch_status} for every wallet in `wallets`, in ONE query."""
    uniq = list(dict.fromkeys(wallets))
    if not uniq:
        return {}
    rows = conn.execute(
        "SELECT wallet, fetch_status FROM backtest_wallet_sync WHERE wallet IN ({})".format(
            ",".join("?" * len(uniq))
        ),
        tuple(uniq),
    ).fetchall()
    return {r["wallet"]: r["fetch_status"] for r in rows}


def bulk_share_timeline(conn, condition_id: str, checkpoints: list) -> tuple:
    """
    ({t: {(wallet,outcome): net_shares}}, {(wallet,outcome): first_seen_ts})
    for every t in `checkpoints`, from a SINGLE forward sweep of the cached
    trade tape -- O(trades + checkpoints) total, instead of
    reconstruct_holders_at()'s O(trades) *per checkpoint* (O(trades *
    checkpoints) across a whole market, the same trade rows rescanned from
    the beginning every time). `checkpoints` must be sorted ascending, which
    _build_checkpoints() already produces.

    Each checkpoint's entry is a snapshot (shallow dict copy) of the running
    share balance at that moment -- cheap (proportional to the market's
    current holder count, not its trade history), and safe for a caller to
    hold onto/mutate independently of later checkpoints.
    """
    rows = conn.execute(
        "SELECT wallet, outcome, side, size, timestamp FROM backtest_market_trades "
        "WHERE condition_id=? ORDER BY timestamp ASC",
        (condition_id,),
    ).fetchall()

    shares: dict = {}
    first_seen: dict = {}
    by_checkpoint: dict = {}
    row_idx = 0
    n_rows = len(rows)
    for t in checkpoints:
        while row_idx < n_rows and rows[row_idx]["timestamp"] <= t:
            r = rows[row_idx]
            key = (r["wallet"], r["outcome"])
            delta = r["size"] if r["side"] == "BUY" else -r["size"]
            shares[key] = shares.get(key, 0.0) + delta
            if key not in first_seen:
                first_seen[key] = r["timestamp"]
            row_idx += 1
        by_checkpoint[t] = dict(shares)
    return by_checkpoint, first_seen
