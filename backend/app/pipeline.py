"""
Reusable trader-scoring pipeline shared by the seed script (app.backfill) and
the on-demand market analyzer (app.market_analysis).

Both callers need the same thing: given a set of wallets, fetch each one's full
/closed-positions history, turn it into a raw-stats record (§3.1) + a Smart
Score (§3.2, per-wallet fixed curve) + a trend (§3.3), and upsert the `traders`
row. Because the Smart Score is computed per-wallet in isolation, there's no
population step -- scoring wallet A never depends on wallet B -- so this is just
"fetch, score, write" with a concurrency + retry wrapper around the fetch.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from . import config as cfg
from .scoring import (
    trader_raw_stats, smart_score_from_raw, compute_trend,
    split_positions_by_lookback,
)

# A wallet whose stats were refreshed within this window is not re-fetched
# (§7 "only wallets active since last run"). Their resolved-position history
# only changes when one of their markets settles, so a day-stale cache is fine.
WALLET_FRESH_SECONDS = 24 * 3600
# A wallet whose *last fetch attempt errored* (Polymarket 500s, timeouts) is
# retried much sooner than a normal 24h refresh -- the error may be transient,
# and until it clears we genuinely don't know this wallet's tier (§ "insider"
# review: an error must not be conflated with a real Unrated/thin-history read).
ERROR_RETRY_SECONDS = 2 * 3600

Progress = Optional[Callable[[str], None]]


def _emit(progress: Progress, msg: str):
    (progress or print)(msg)


# ---------------------------------------------------------------------------
# Fetch wallets' closed-position history (concurrent + single-threaded retry)
# ---------------------------------------------------------------------------

def fetch_wallet_positions(
    source,
    wallets: list[str],
    now_ts: int,
    concurrency: Optional[int] = None,
    progress: Progress = None,
) -> tuple[dict, dict]:
    """
    Returns ({wallet: [ClosedPosition, ...]}, {wallet: last_error_str}) -- the
    second dict covers every wallet that still failed after the retry pass, so
    callers can record *why* a wallet has no score (API error) rather than
    silently treating it the same as a genuinely thin-history Unrated wallet.
    """
    from .backfill import fetch_all_closed_positions  # avoid import cycle at module load

    concurrency = concurrency or cfg.BACKFILL.fetch_concurrency
    out: dict = {}
    errors: dict = {}

    def _fetch_one(wallet: str):
        return wallet, fetch_all_closed_positions(source, wallet, now_ts)

    def _run_pass(batch: list, workers: int, label: str) -> list:
        pass_failed: list = []
        completed = 0
        every = cfg.BACKFILL.progress_every_n_wallets
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, w): w for w in batch}
            for future in as_completed(futures):
                wallet = futures[future]
                completed += 1
                try:
                    _, positions = future.result()
                except Exception as e:
                    pass_failed.append(wallet)
                    errors[wallet] = str(e)
                    _emit(progress, f"  [{label}] fetch failed for {wallet}: {e}")
                else:
                    out[wallet] = positions
                    errors.pop(wallet, None)
                if completed % every == 0 or completed == len(batch):
                    elapsed = time.monotonic() - start
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta = (len(batch) - completed) / rate if rate > 0 else float("inf")
                    _emit(progress, f"  [{label}] {completed}/{len(batch)} wallets "
                                    f"({rate:.1f}/s, ~{eta:.0f}s left)")
        return pass_failed

    if not wallets:
        return out, errors
    retry = _run_pass(wallets, concurrency, "main")
    if retry:
        _emit(progress, f"  retrying {len(retry)} rate-limited wallets single-threaded...")
        _run_pass(retry, 1, "retry")
    _emit(progress, f"  fetched {len(out)}/{len(wallets)} wallets"
                     + (f", {len(errors)} still erroring" if errors else ""))
    return out, errors


# ---------------------------------------------------------------------------
# Score + persist
# ---------------------------------------------------------------------------

def wallets_needing_refresh(conn, wallets: list[str], now_ts: int, force: bool = False) -> list[str]:
    """
    Subset of `wallets` not in the DB, or stale: a normal 'ok' row is stale
    after WALLET_FRESH_SECONDS, but a row that last errored out (fetch_status=
    'error') is retried much sooner (ERROR_RETRY_SECONDS) since the failure may
    have been transient.
    """
    uniq = list(dict.fromkeys(wallets))
    if force or not uniq:
        return uniq
    fresh = set()
    rows = conn.execute(
        "SELECT wallet, updated_at, fetch_status FROM traders WHERE wallet IN ({})".format(
            ",".join("?" * len(uniq))
        ),
        tuple(uniq),
    ).fetchall()
    for r in rows:
        if r["updated_at"] is None:
            continue
        ttl = ERROR_RETRY_SECONDS if r["fetch_status"] == "error" else WALLET_FRESH_SECONDS
        if (now_ts - r["updated_at"]) < ttl:
            fresh.add(r["wallet"])
    return [w for w in uniq if w not in fresh]


def record_fetch_failures(conn, errors: dict, now_ts: int):
    """
    Persist wallets that still errored after the retry pass, so (a) they're
    surfaced as 'API error' rather than silently blank/Unrated, and (b)
    wallets_needing_refresh retries them on ERROR_RETRY_SECONDS instead of
    hammering Polymarket on every single request. Doesn't touch wallets that
    already have a real ('ok') scored row -- an error on the retry attempt of
    an already-scored wallet shouldn't erase its last-known-good score.
    """
    for wallet, err in errors.items():
        existing = conn.execute("SELECT fetch_status FROM traders WHERE wallet=?", (wallet,)).fetchone()
        if existing is not None and existing["fetch_status"] == "ok":
            continue
        conn.execute(
            """
            INSERT INTO traders (wallet, n, volume, tier, fetch_status, fetch_error, updated_at)
            VALUES (?, 0, 0, 'Unrated', 'error', ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                fetch_status='error', fetch_error=excluded.fetch_error, updated_at=excluded.updated_at
            """,
            (wallet, err[:500], now_ts),
        )


def score_and_persist_wallets(
    conn,
    positions_by_wallet: dict,
    now_ts: int,
    lookback_days: int,
    progress: Progress = None,
) -> dict:
    """
    positions_by_wallet: {wallet: [ClosedPosition]} from fetch_wallet_positions().
    Computes raw stats + Smart Score (lifetime & recent window) + trend for each
    wallet independently and upserts the `traders` table. Returns a summary.
    """
    written = 0
    rated = 0
    for wallet, positions in positions_by_wallet.items():
        life_raw = trader_raw_stats(wallet, positions, now_ts)
        recent_pos, _prior = split_positions_by_lookback(positions, now_ts, lookback_days)
        recent_raw = trader_raw_stats(wallet, recent_pos, now_ts)

        life = composite_result(wallet, life_raw)
        recent = composite_result(wallet, recent_raw)
        trend = compute_trend({wallet: life}, {wallet: recent} if recent.percentile is not None else {}).get(wallet)

        if life.percentile is not None:
            rated += 1

        conn.execute(
            """
            INSERT INTO traders (wallet, n, volume, win_rate_weighted, mean_return, stdev_return,
                sharpe_like, brier_score, n_calibration, last_active_ts, tier, raw_composite,
                percentile, recent_raw_composite, recent_percentile, recent_n, trend, trend_delta,
                lookback_days, updated_at, fetch_status, fetch_error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ok',NULL)
            ON CONFLICT(wallet) DO UPDATE SET
                n=excluded.n, volume=excluded.volume, win_rate_weighted=excluded.win_rate_weighted,
                mean_return=excluded.mean_return, stdev_return=excluded.stdev_return,
                sharpe_like=excluded.sharpe_like, brier_score=excluded.brier_score,
                n_calibration=excluded.n_calibration, last_active_ts=excluded.last_active_ts,
                tier=excluded.tier, raw_composite=excluded.raw_composite, percentile=excluded.percentile,
                recent_raw_composite=excluded.recent_raw_composite, recent_percentile=excluded.recent_percentile,
                recent_n=excluded.recent_n, trend=excluded.trend, trend_delta=excluded.trend_delta,
                lookback_days=excluded.lookback_days, updated_at=excluded.updated_at,
                fetch_status='ok', fetch_error=NULL
            """,
            (
                wallet, life_raw.n, life_raw.volume, life_raw.win_rate_weighted, life_raw.mean_return,
                life_raw.stdev_return, life_raw.sharpe_like, life_raw.brier_score, life_raw.n_calibration,
                life_raw.last_active_ts, life_raw.tier,
                life.raw_composite, life.percentile,
                recent.raw_composite, recent.percentile, len(recent_pos),
                trend.trend if trend else "Insufficient recent data",
                trend.delta if trend else None,
                lookback_days, now_ts,
            ),
        )
        written += 1

    _emit(progress, f"  scored + wrote {written} wallets ({rated} rated)")
    return {"written": written, "rated": rated}


def composite_result(wallet: str, raw):
    """One wallet's TraderRawStats -> ScoreResult (thin wrapper around smart_score_from_raw)."""
    from .scoring import ScoreResult
    sc = smart_score_from_raw(raw)
    return ScoreResult(wallet=wallet, raw_composite=sc, percentile=sc, tier=raw.tier)
