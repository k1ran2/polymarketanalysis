"""
Point-in-time replay of the Smart Money verdict (§3.8) and Insider-score
(app.insiders) signals against already-RESOLVED markets, to see whether
either signal would actually have made money -- not just whether it looks
reasonable.

Methodology, and its honest limits:
  - At each checkpoint time T within a market's life, holders are
    reconstructed from the real trade tape up to T only (app.backtest_data.
    reconstruct_holders_at) -- never from the market's *current* holders.
  - Each holder's Smart Score at T is computed from ONLY their closed
    positions with timestamp <= T (the same trader_raw_stats/smart_score_
    from_raw functions the live app uses, just fed a truncated history) --
    no lookahead into positions that hadn't closed yet as of T.
  - The exact same overall_market_verdict() / compute_insider_score() /
    summarize_insider_rows() functions the live dashboard calls are reused
    here unmodified -- a backtest result is a result about the real
    production scoring code, not a separate re-implementation of it.
  - LIMITATION: entry_price is the last TRADED price at/near T (CLOB
    /prices-history), not a true historical bid/ask -- Polymarket doesn't
    expose historical order-book depth publicly. Real fills would have cost
    slightly more (the spread), so results here are somewhat optimistic,
    more so on thin/illiquid markets.
  - LIMITATION: the live market-maker/arbitrage exclusion (insiders.
    _is_market_maker_like) depends on the live `markets`/event tables and
    is NOT applied here -- a backtest can include a flagged position that
    the live Insiders tab would have excluded as structural liquidity
    provision rather than informed betting.
  - Only one simulated trade is taken per market per strategy per run (the
    first checkpoint whose signal clears the threshold), held to
    resolution -- no early-exit modeling. This mirrors how the live alerting
    system fires once per newly-detected flag, and is the simplest,
    least-assumption-laden way to grade a directional signal.
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from . import config as cfg
from . import db
from .backtest_data import (
    BacktestMarket, bulk_fetch_status, bulk_price_series, bulk_share_timeline,
    bulk_wallet_positions, price_at_or_before, price_at_or_before_series, persist_wallet_positions,
    reconstruct_holders_at, sync_market_meta, sync_market_trades,
    sync_price_history, wallet_positions_asof, wallets_needing_sync,
)
from .pipeline import fetch_wallet_positions
from .polymarket_client import ClobClient, DataAPIClient, GammaClient
from .scoring import Holder, smart_score_from_raw, trader_raw_stats

_lock = threading.Lock()
_current_run_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Run management (mirrors app.poller's single-flight-in-a-background-thread pattern)
# ---------------------------------------------------------------------------

def start_backtest(params: dict) -> dict:
    """
    Create a run row and kick it off in the background. Only one backtest
    runs at a time (it's a lot of network I/O) -- a request while one is
    already running returns that run's id instead of starting a second.
    """
    global _current_run_id
    with _lock:
        if _current_run_id is not None:
            return {"run_id": _current_run_id, "started": False}
        run_id = uuid.uuid4().hex
        _current_run_id = run_id

    now_ts = int(time.time())
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO backtest_runs (id, created_at, status, params) VALUES (?,?,?,?)",
            (run_id, now_ts, "pending", json.dumps(params)),
        )
    threading.Thread(target=_run, args=(run_id, params), name=f"backtest-{run_id[:8]}", daemon=True).start()
    return {"run_id": run_id, "started": True}


def get_run(run_id: str) -> Optional[dict]:
    with db.db_session() as conn:
        row = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["params"] = json.loads(out["params"])
        out["summary"] = json.loads(out["summary"]) if out["summary"] else None
        # Trades already recorded, even mid-run -- markets are processed
        # concurrently now (see market_concurrency), so a long run's equity
        # curve can build up live instead of staying blank until everything
        # finishes. summary (win rate, total P&L etc.) is still only
        # computed once, at the end.
        trades = conn.execute(
            "SELECT * FROM backtest_trades_sim WHERE run_id=? ORDER BY signal_ts ASC", (run_id,)
        ).fetchall()
        out["trades"] = [dict(t) for t in trades]
        return out


def reconcile_orphaned_runs():
    """
    Call once at process startup. `_current_run_id`'s single-flight lock is
    in-memory only, so a process restart mid-run (a crash, or --reload
    picking up an edited file) leaves that run's DB row stuck saying
    'running' forever, with no thread left actually working on it. Mark any
    such leftover rows 'error' so the UI doesn't show a phantom in-progress
    run and a new one can start immediately instead of the confusing
    "already running" response start_backtest would otherwise give for a
    run nothing is actually advancing.
    """
    now_ts = int(time.time())
    with db.db_session() as conn:
        conn.execute(
            "UPDATE backtest_runs SET status='error', completed_at=?, "
            "error='Interrupted by a server restart before completion' "
            "WHERE status IN ('running', 'pending')",
            (now_ts,),
        )


def list_runs(limit: int = 20) -> list:
    with db.db_session() as conn:
        rows = conn.execute(
            "SELECT id, created_at, completed_at, status, params, progress_done, progress_total, summary "
            "FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        out.append(d)
    return out



# ---------------------------------------------------------------------------
# Weight optimization (random search with a held-out test split)
# ---------------------------------------------------------------------------

def train_test_split_markets(condition_ids: list, test_fraction: float = 0.3, seed: Optional[int] = None) -> tuple:
    """
    Splits `condition_ids` into (train, test) -- train is what the search
    tunes weights against; test is a disjoint set the search NEVER touches
    until the very end, when the single best-found config is evaluated on
    it once. Without this split, "optimal" would only ever mean "best-fit
    to this exact history", which a large-enough random search can always
    find given enough tries regardless of whether the weights capture
    anything real (classic overfitting) -- a config that also holds up on
    markets the search never saw is the only genuine signal that it found
    something more than noise.
    """
    ids = list(dict.fromkeys(condition_ids))  # de-dupe, preserve order
    rng = random.Random(seed)
    rng.shuffle(ids)
    n_test = max(1, round(len(ids) * test_fraction)) if len(ids) > 1 else 0
    return ids[n_test:], ids[:n_test]


def start_search(params: dict) -> dict:
    """
    Kick off a random-search weight optimization in the background. Shares
    the same single-flight lock as start_backtest -- a search is many full
    backtests back to back, exactly as network-heavy as a run, so it
    shouldn't overlap with one either.
    """
    global _current_run_id
    with _lock:
        if _current_run_id is not None:
            return {"search_id": _current_run_id, "started": False}
        search_id = uuid.uuid4().hex
        _current_run_id = search_id

    now_ts = int(time.time())
    n_iterations = int(params.get("n_iterations", 10))
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO backtest_searches (id, created_at, status, params, progress_total) VALUES (?,?,?,?,?)",
            (search_id, now_ts, "pending", json.dumps(params), n_iterations + 1),
        )
    threading.Thread(target=_run_search, args=(search_id, params), name=f"search-{search_id[:8]}", daemon=True).start()
    return {"search_id": search_id, "started": True}


def get_search(search_id: str) -> Optional[dict]:
    with db.db_session() as conn:
        row = conn.execute("SELECT * FROM backtest_searches WHERE id=?", (search_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["params"] = json.loads(out["params"])
        out["history"] = json.loads(out["history"]) if out["history"] else []
        out["best_overrides"] = json.loads(out["best_overrides"]) if out["best_overrides"] else None
        out["best_train_summary"] = json.loads(out["best_train_summary"]) if out["best_train_summary"] else None
        out["test_summary"] = json.loads(out["test_summary"]) if out["test_summary"] else None
        return out


def list_searches(limit: int = 20) -> list:
    with db.db_session() as conn:
        rows = conn.execute(
            "SELECT id, created_at, completed_at, status, params, progress_done, progress_total, "
            "best_overrides, best_train_summary, test_summary "
            "FROM backtest_searches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["best_overrides"] = json.loads(d["best_overrides"]) if d["best_overrides"] else None
        d["best_train_summary"] = json.loads(d["best_train_summary"]) if d["best_train_summary"] else None
        d["test_summary"] = json.loads(d["test_summary"]) if d["test_summary"] else None
        out.append(d)
    return out


def _search_objective(summary: dict) -> float:
    """
    ROI, but a config that took zero trades is scored as strictly worse than
    any config that took a real, losing one -- otherwise the search
    trivially "wins" by picking thresholds so strict nothing ever fires,
    which is a real failure mode of naively maximizing ROI (a 0-trade config
    has undefined/None ROI, not a genuinely good one).
    """
    if summary["n_trades"] == 0 or summary["roi_pct"] is None:
        return -1e9
    return summary["roi_pct"]


def _evaluate_config(run_id: str, condition_ids: list, run_params: dict) -> dict:
    """One full backtest (all given markets, one parameter combo), reusing the
    exact same per-market logic a normal run uses -- a search result is a
    real backtest result, not a separate, unaudited code path."""
    now_ts = int(time.time())
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO backtest_runs (id, created_at, status, params, progress_total) VALUES (?,?,?,?,?)",
            (run_id, now_ts, "running", json.dumps(run_params), len(condition_ids)),
        )
    with ThreadPoolExecutor(max_workers=cfg.BACKTEST.market_concurrency) as pool:
        futures = {pool.submit(_backtest_one_market, run_id, cid, run_params): cid for cid in condition_ids}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                future.result()
            except Exception as e:  # noqa: BLE001 - one bad market must not kill the search
                print(f"[search {run_id[:8]}] {cid[:12]} failed: {e}")
            with db.db_session() as conn:
                conn.execute("UPDATE backtest_runs SET progress_done = progress_done + 1 WHERE id=?", (run_id,))
    summary = _summarize_run(run_id)
    with db.db_session() as conn:
        conn.execute(
            "UPDATE backtest_runs SET status='done', completed_at=?, summary=? WHERE id=?",
            (int(time.time()), json.dumps(summary), run_id),
        )
    return summary


def _run_search(search_id: str, params: dict):
    global _current_run_id
    try:
        with db.db_session() as conn:
            conn.execute("UPDATE backtest_searches SET status='running' WHERE id=?", (search_id,))

        strategy = params["strategy"]
        config_kind = params["config_kind"]  # "verdict" | "insider"
        param_space = params["param_space"]  # {name: [min, max]}
        n_iterations = int(params.get("n_iterations", 10))
        train_ids = params["train_condition_ids"]
        test_ids = params["test_condition_ids"]
        base = dict(params.get("base_params") or {})
        base["strategy"] = strategy
        rng = random.Random(params.get("seed"))

        overrides_key = "verdict_overrides" if config_kind == "verdict" else "insider_overrides"
        history = []
        best = None  # (objective, overrides, summary)

        for i in range(n_iterations):
            overrides = {name: round(rng.uniform(lo, hi), 4) for name, (lo, hi) in param_space.items()}
            run_params = {**base, "condition_ids": train_ids, overrides_key: overrides}
            iter_run_id = f"{search_id}-train{i}"
            summary = _evaluate_config(iter_run_id, train_ids, run_params)
            history.append({"overrides": overrides, "run_id": iter_run_id, "summary": summary})

            objective = _search_objective(summary)
            if best is None or objective > best[0]:
                best = (objective, overrides, summary)

            with db.db_session() as conn:
                conn.execute(
                    "UPDATE backtest_searches SET progress_done=?, history=? WHERE id=?",
                    (i + 1, json.dumps(history), search_id),
                )

        best_overrides = best[1] if best else {}
        best_train_summary = best[2] if best else None

        # The number that actually matters: the winning config, evaluated
        # ONCE on markets the search never touched while tuning.
        test_run_id = f"{search_id}-test"
        test_params = {**base, "condition_ids": test_ids, overrides_key: best_overrides}
        test_summary = _evaluate_config(test_run_id, test_ids, test_params) if test_ids else None

        with db.db_session() as conn:
            conn.execute(
                """
                UPDATE backtest_searches SET status='done', completed_at=?, progress_done=?,
                    best_overrides=?, best_train_summary=?, test_summary=?
                WHERE id=?
                """,
                (int(time.time()), n_iterations + 1, json.dumps(best_overrides),
                 json.dumps(best_train_summary), json.dumps(test_summary), search_id),
            )
    except Exception as e:  # noqa: BLE001 - surface a genuine crash as an errored search, not a hang
        with db.db_session() as conn:
            conn.execute(
                "UPDATE backtest_searches SET status='error', completed_at=?, error=? WHERE id=?",
                (int(time.time()), str(e)[:2000], search_id),
            )
    finally:
        with _lock:
            _current_run_id = None


def _run(run_id: str, params: dict):
    global _current_run_id
    try:
        with db.db_session() as conn:
            conn.execute("UPDATE backtest_runs SET status='running' WHERE id=?", (run_id,))
        condition_ids = params.get("condition_ids") or []
        with db.db_session() as conn:
            conn.execute(
                "UPDATE backtest_runs SET progress_total=? WHERE id=?", (len(condition_ids), run_id)
            )

        # Markets in parallel, not one at a time: almost all of a market's
        # cost is waiting on rate-limited network I/O (Data API for trades +
        # wallet history, CLOB for prices), and that waiting doesn't actually
        # need to be serialized across DIFFERENT markets -- the shared,
        # thread-safe token buckets in polymarket_client already cap real
        # throughput correctly no matter how many markets are in flight at
        # once, so running several concurrently just keeps that budget
        # continuously used instead of idle while one slow market blocks
        # every other one behind it. Each market has its own DB writes
        # (WAL + busy_timeout already handles concurrent connections), so
        # nothing here needs its own lock beyond that.
        with ThreadPoolExecutor(max_workers=cfg.BACKTEST.market_concurrency) as pool:
            futures = {pool.submit(_backtest_one_market, run_id, cid, params): cid for cid in condition_ids}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    future.result()
                except Exception as e:  # noqa: BLE001 - one bad market must not kill the whole run
                    print(f"[backtest {run_id[:8]}] {cid[:12]} failed: {e}")
                with db.db_session() as conn:
                    conn.execute(
                        "UPDATE backtest_runs SET progress_done = progress_done + 1 WHERE id=?", (run_id,)
                    )

        summary = _summarize_run(run_id)
        with db.db_session() as conn:
            conn.execute(
                "UPDATE backtest_runs SET status='done', completed_at=?, summary=? WHERE id=?",
                (int(time.time()), json.dumps(summary), run_id),
            )
    except Exception as e:  # noqa: BLE001 - surface a genuine crash as an errored run, not a hang
        with db.db_session() as conn:
            conn.execute(
                "UPDATE backtest_runs SET status='error', completed_at=?, error=? WHERE id=?",
                (int(time.time()), str(e)[:2000], run_id),
            )
    finally:
        with _lock:
            _current_run_id = None


# ---------------------------------------------------------------------------
# Per-market replay
# ---------------------------------------------------------------------------

_gamma: Optional[GammaClient] = None
_data_api: Optional[DataAPIClient] = None
_clob: Optional[ClobClient] = None
_clients_lock = threading.Lock()


def _clients():
    # Guarded: with market_concurrency > 1, several worker threads can call
    # this for the first time at once. Without the lock, a race here could
    # construct multiple independent clients, each with its OWN token
    # bucket -- silently multiplying the real rate limit instead of sharing
    # one, which is the entire point of these being module-level singletons.
    global _gamma, _data_api, _clob
    with _clients_lock:
        if _gamma is None:
            _gamma = GammaClient()
            _data_api = DataAPIClient(max_retries=cfg.BACKFILL.data_api_max_retries,
                                       backoff_cap=cfg.BACKFILL.data_api_backoff_cap)
            _clob = ClobClient()
        return _gamma, _data_api, _clob


# Global cap on concurrent Data API /closed-positions calls, shared across
# EVERY market in a run, not per-market. Without this, market_concurrency
# markets each spinning up their own fetch_concurrency-worker pool multiply
# to far more simultaneous requests than that (e.g. 4 x 10 = 40) -- the
# token bucket's average-rate accounting doesn't prevent that burst, and
# confirmed live, it caused real request failures ("rate-limited after 5
# retries", wallets silently dropped), not just slower throughput -- the
# same failure mode documented in RateLimitConfig for the original backfill
# script. A shared semaphore bounds real in-flight requests to one number
# regardless of how many markets are running, restoring the single-market
# behavior that worked.
_data_api_inflight = threading.Semaphore(cfg.BACKTEST.fetch_concurrency)


class _WalletSource:
    """
    Adapts the shared, already-rate-limited DataAPIClient to the tiny
    `.get_closed_positions_page()` interface pipeline.fetch_wallet_positions
    expects -- reusing one client (and its token bucket) across an entire
    run, instead of app.backfill.LiveDataSource's usual one-per-caller
    client, which would reset rate-limiting state on every market.
    """
    def __init__(self, data_api: DataAPIClient):
        self.data_api = data_api

    def get_closed_positions_page(self, wallet: str, limit: int, offset: int) -> list:
        with _data_api_inflight:
            return self.data_api.get_closed_positions(
                wallet, limit=limit, offset=offset, sort_by=cfg.BACKFILL.closed_positions_sort_by,
            )


def _backtest_one_market(run_id: str, condition_id: str, params: dict):
    now_ts = int(time.time())
    gamma, data_api, clob = _clients()

    with db.db_session() as conn:
        market = sync_market_meta(conn, gamma, condition_id, now_ts)
    if market is None or market.resolved_yes is None:
        return  # not resolved -- no ground truth to grade against
    if len(market.clob_token_ids) < 2:
        return

    window_start, window_end = _market_window(market)
    if window_start is None or window_end is None:
        return

    checkpoints = _build_checkpoints(market)
    yes_token, no_token = market.clob_token_ids[0], market.clob_token_ids[1]

    with db.db_session() as conn:
        sync_market_trades(conn, data_api, condition_id, now_ts)
        capped = conn.execute(
            "SELECT trades_capped FROM backtest_market_meta WHERE condition_id=?", (condition_id,)
        ).fetchone()["trades_capped"]
        if capped:
            print(f"[backtest] {condition_id[:12]} trade tape capped at Data API's 10k-offset limit -- "
                  f"early-life reconstruction for this market is incomplete")
        sync_price_history(conn, clob, condition_id, market.clob_token_ids, window_start, window_end, now_ts)

        # Bulk-load the trade tape + price series ONCE here, before wallet
        # discovery even runs, and reuse the same structures for both it and
        # the main checkpoint loop below -- wallet discovery's sample points
        # are a subset of `checkpoints`, so there is exactly one sweep of
        # this market's trade tape per run, not two (see _wallets_worth_
        # scoring's docstring for the redundant-scan this replaced).
        token_for_side = {"YES": yes_token, "NO": no_token}
        price_series_by_token = {
            yes_token: bulk_price_series(conn, yes_token),
            no_token: bulk_price_series(conn, no_token),
        }
        # Pre-extracted once, not per lookup -- see price_at_or_before_series's
        # docstring for why this matters far more than it looks like it should.
        price_timestamps_by_token = {tok: [t for t, _ in s] for tok, s in price_series_by_token.items()}
        by_checkpoint, first_seen = bulk_share_timeline(conn, condition_id, checkpoints)

        # Only wallets that can actually move a $-weighted metric get their
        # trading history fetched -- see BacktestConfig.smart_score_top_n_holders.
        # A wallet outside this set is simply never fetched, which the
        # existing Holder-building code already treats correctly as Unrated
        # (no separate "skipped" code path needed).
        insider_floor = build_insider_cfg(params.get("insider_overrides")).min_position_usd
        wallets = _wallets_worth_scoring(
            by_checkpoint, checkpoints, market, price_series_by_token, price_timestamps_by_token, insider_floor,
        )
        need = wallets_needing_sync(conn, wallets, now_ts)

    if need:
        source = _WalletSource(data_api)
        positions_by_wallet, errors = fetch_wallet_positions(
            source, need, now_ts, concurrency=cfg.BACKTEST.fetch_concurrency,
        )
        with db.db_session() as conn:
            persist_wallet_positions(conn, positions_by_wallet, errors, now_ts)

    end_date_iso = (
        datetime.fromtimestamp(market.end_ts, tz=timezone.utc).isoformat() if market.end_ts else None
    )
    stake_usd = float(params.get("stake_usd", cfg.BACKTEST.default_stake_usd))
    strategy = params.get("strategy", "smart_money")
    no_edge_threshold = float(params.get("max_no_edge_entry_price") or cfg.BACKTEST.max_no_edge_entry_price)

    with db.db_session() as conn:
        # The rest of what the checkpoint loop below needs, bulk-loaded ONCE
        # instead of the per-timestamp functions doing one SQL round-trip per
        # wallet per checkpoint (measured: ~75% of a checkpoint's cost used
        # to be DB round-trips, not the trade-tape scan or the scoring math
        # itself -- see _holders_at_fast's docstring for the full reasoning,
        # and test_backtest_golden.py for the regression check that this
        # produces byte-identical trades to the naive per-call path).
        all_wallets = list({w for (w, _outcome) in first_seen.keys()})
        # No checkpoint can ever need a wallet position closed after the LAST
        # checkpoint (see _holders_at_fast's bisect against this same prefix)
        # -- bounding the load avoids fetching a wallet's entire subsequent
        # trading history for no reason (measured on this project's real DB:
        # 84-94% of a wallet's cached rows postdate an older market's last
        # checkpoint, since the cache keeps accumulating long after any given
        # historical market resolved).
        last_checkpoint = checkpoints[-1] if checkpoints else now_ts
        positions_by_wallet = bulk_wallet_positions(conn, all_wallets, as_of_ts=last_checkpoint)
        positions_ts_by_wallet = {w: [p.timestamp for p in plist] for w, plist in positions_by_wallet.items()}
        fetch_status_by_wallet = bulk_fetch_status(conn, all_wallets)
        raw_stats_cache: dict = {}

        for t in checkpoints:
            price_yes = price_at_or_before_series(price_series_by_token[yes_token], t, timestamps=price_timestamps_by_token[yes_token])
            if price_yes is None:
                continue
            holders = _holders_at_fast(
                t, by_checkpoint.get(t, {}), first_seen, token_for_side,
                price_series_by_token, price_timestamps_by_token, positions_by_wallet,
                positions_ts_by_wallet, fetch_status_by_wallet, raw_stats_cache,
            )
            if not holders:
                continue

            if strategy == "smart_money":
                signal = _smart_money_signal(holders, price_yes, params)
            else:
                signal = _insider_signal(holders, t, end_date_iso, params)

            if signal is None:
                continue
            side, signal_value, signal_type = signal
            entry_price = price_yes if side == "YES" else price_at_or_before_series(price_series_by_token[no_token], t, timestamps=price_timestamps_by_token[no_token])
            if entry_price is None or entry_price <= 0:
                continue
            if _has_no_edge(entry_price, no_edge_threshold):
                # The signal fired, but the market's already priced this side
                # as a near-certainty -- no real edge left, just a lopsided
                # risk/reward (see BacktestConfig.max_no_edge_entry_price).
                # Skip this checkpoint, not the whole market: an earlier
                # signal on the same market may already have taken a real
                # trade, and a later checkpoint could still see the price
                # pull back to something worth trading.
                continue

            shares = stake_usd / entry_price
            won = (side == "YES") == market.resolved_yes
            payout = 1.0 if won else 0.0
            pnl_usd = shares * (payout - entry_price)
            conn.execute(
                """
                INSERT INTO backtest_trades_sim (run_id, condition_id, question, category, side,
                    signal_type, signal_ts, signal_value, entry_price, stake_usd, shares,
                    resolved_yes, won, pnl_usd, pnl_pct)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (run_id, condition_id, market.question, market.category, side, signal_type, t,
                 signal_value, entry_price, stake_usd, shares, int(market.resolved_yes), int(won),
                 pnl_usd, pnl_usd / stake_usd),
            )
            break  # one simulated trade per market per run -- see module docstring


def _has_no_edge(entry_price: float, threshold: float = cfg.BACKTEST.max_no_edge_entry_price) -> bool:
    """
    True if `entry_price` already prices this side as a near-certainty --
    see BacktestConfig.max_no_edge_entry_price. A win there pays out
    stake*(1-price)/price (~$0.20 per $100 stake at 0.998), while a loss
    still costs the full stake -- a real edge on the LOW side (price near
    $0) is the opposite case and is never filtered here, only the high side.
    `threshold` is overridable per-run (RunRequest.max_no_edge_entry_price).
    """
    return entry_price >= threshold


def _market_window(market: BacktestMarket) -> tuple:
    """(start_ts, end_ts) bounding a market's real life, with a 30-day fallback
    for start if Gamma didn't give us one -- used both to bound the CLOB price
    fetch and to lay out checkpoints, so the two always agree."""
    start = market.start_ts or (market.resolved_ts - 30 * 86400 if market.resolved_ts else None)
    end = market.resolved_ts or market.end_ts
    if start is None or end is None or end <= start:
        return None, None
    return start, end


def _evenly_spaced_indices(n_total: int, n_wanted: int) -> list:
    """
    n_wanted indices into range(n_total), evenly spaced, ALWAYS including the
    final index (n_total - 1) -- the naive `int(i * n_total / n_wanted)`
    stride never lands on it: for n_total=500, n_wanted=200, the last pick
    comes out to index 497, silently dropping the final ~1% of whatever
    `n_total` represents. For a market's checkpoint list, that ~1% is the
    checkpoints closest to resolution -- confirmed against this project's
    real cached markets: 233 of 356 (65%) exceed max_checkpoints_per_market
    and get downsampled, and the worst case found drops the final 2.25 days
    of a 455-day market entirely. That's exactly the window an insider
    position is most likely to appear in (see InsiderConfig's urgency
    factor), so this wasn't a cosmetic gap -- it was real signal coverage
    silently missing at the one place it mattered most, contradicting this
    function's own former claim of keeping "coverage across the market's
    whole life."

    Uses stride = (n_total-1)/(n_wanted-1) instead, which by construction
    maps i=n_wanted-1 to exactly n_total-1.
    """
    if n_total <= 0:
        return []
    if n_wanted <= 1 or n_total <= 1:
        return [n_total - 1]
    if n_wanted >= n_total:
        return list(range(n_total))
    stride = (n_total - 1) / (n_wanted - 1)
    # round() + dedup rather than a bare list comprehension: guards against
    # a pathological n_wanted very close to n_total producing a repeated
    # index after rounding, which would silently under-deliver rather than
    # ever duplicate or go out of range.
    indices = sorted({round(i * stride) for i in range(n_wanted)})
    return indices


def _build_checkpoints(market: BacktestMarket) -> list:
    start, end = _market_window(market)
    if start is None:
        return []
    step = max(1, int(cfg.BACKTEST.default_checkpoint_hours * 3600))
    points = list(range(start, end, step))
    if len(points) > cfg.BACKTEST.max_checkpoints_per_market:
        # Downsample evenly rather than just truncating to the first N --
        # see _evenly_spaced_indices for why a naive stride doesn't actually
        # achieve that.
        points = [points[i] for i in _evenly_spaced_indices(len(points), cfg.BACKTEST.max_checkpoints_per_market)]
    return points


def _wallets_worth_scoring(
    by_checkpoint: dict, checkpoints: list, market: BacktestMarket,
    price_series_by_token: dict, price_timestamps_by_token: dict,
    insider_floor: float = cfg.INSIDER.min_position_usd,
) -> list:
    """
    The reduced wallet set worth fetching a full trading history for --
    across BacktestConfig.wallet_discovery_checkpoints sample points spread
    over the market's life (not every signal-evaluation checkpoint -- see
    that config's docstring for why), whoever ranks in the top
    smart_score_top_n_holders by position size at that sample, unioned with
    anyone whose position ever clears `insider_floor` (needed for insider
    detection regardless of rank -- the CALLER's effective, possibly
    per-run-overridden InsiderConfig.min_position_usd, not always the
    global default: a lowered override that this function ignored would
    silently never fetch the smaller wallets it was meant to catch).

    Takes the SAME bulk_share_timeline/bulk_price_series structures the main
    checkpoint loop uses (built once by the caller) instead of re-scanning
    the trade tape itself -- this used to call reconstruct_holders_at once
    per sample point, a second full pass over the same trade rows
    bulk_share_timeline already swept for every checkpoint (sample points
    are a subset of `checkpoints`), left over from before the bulk-loading
    speed pass. No network either way -- purely local, already-synced data.

    Skips a wallet entirely rather than fetching-then-discarding it -- the
    live insiders/smart-score code already treats an unfetched wallet as
    Unrated ($0 weight), so nothing downstream needs to know the difference
    between "we checked, they're thin" and "not worth checking."
    """
    token_ids = market.clob_token_ids
    token_for_side = {"YES": token_ids[0], "NO": token_ids[1] if len(token_ids) > 1 else None}
    top_n = cfg.BACKTEST.smart_score_top_n_holders

    n_samples = cfg.BACKTEST.wallet_discovery_checkpoints
    if len(checkpoints) > n_samples:
        # Same _evenly_spaced_indices fix as _build_checkpoints -- and here
        # it matters even more: missing the market's final discovery sample
        # meant a wallet that took a large position only in the last stretch
        # before resolution (the classic insider profile the whole detector
        # exists to catch) was never added to the fetch set at all, so it
        # showed fetch_status != "ok" at EVERY checkpoint and was
        # permanently invisible to both strategies for that entire market.
        sample_points = [checkpoints[i] for i in _evenly_spaced_indices(len(checkpoints), n_samples)]
    else:
        sample_points = checkpoints

    needed: set = set()
    for t in sample_points:
        shares_by_key = by_checkpoint.get(t, {})
        sized = []
        for (wallet, outcome), shares in shares_by_key.items():
            if shares <= 0:
                continue
            token_id = token_for_side.get(outcome)
            series = price_series_by_token.get(token_id) if token_id else None
            ts_list = price_timestamps_by_token.get(token_id) if token_id else None
            price = price_at_or_before_series(series, t, timestamps=ts_list) if series is not None else None
            if price is None:
                continue
            sized.append((wallet, shares * price))
        sized.sort(key=lambda x: x[1], reverse=True)
        for wallet, position_usd in sized[:top_n]:
            needed.add(wallet)
        for wallet, position_usd in sized:
            if position_usd >= insider_floor:
                needed.add(wallet)
    return list(needed)


def _holders_at(conn, condition_id: str, t: int) -> list:
    recon = reconstruct_holders_at(conn, condition_id, t)
    shares_by_key = recon["shares"]
    first_seen = recon["first_seen"]
    yes_token_row = conn.execute(
        "SELECT clob_token_ids FROM backtest_market_meta WHERE condition_id=?", (condition_id,)
    ).fetchone()
    token_ids = json.loads(yes_token_row["clob_token_ids"]) if yes_token_row else []
    token_for_side = {"YES": token_ids[0] if token_ids else None, "NO": token_ids[1] if len(token_ids) > 1 else None}

    # Group by wallet so each wallet's position history is fetched once even
    # if they hold both sides (rare, but not impossible pre-merge).
    by_wallet: dict = {}
    for (wallet, outcome), shares in shares_by_key.items():
        if shares <= 0:
            continue
        by_wallet.setdefault(wallet, []).append((outcome, shares))

    holders = []
    for wallet, sides in by_wallet.items():
        positions = wallet_positions_asof(conn, wallet, t)
        raw = trader_raw_stats(wallet, positions, t)
        score = smart_score_from_raw(raw)
        sync_row = conn.execute(
            "SELECT fetch_status FROM backtest_wallet_sync WHERE wallet=?", (wallet,)
        ).fetchone()
        fetch_status = sync_row["fetch_status"] if sync_row else "error"
        avg_bet_usd = (raw.volume / raw.n) if raw.n else None

        for outcome, shares in sides:
            token_id = token_for_side.get(outcome)
            price = price_at_or_before(conn, token_id, t) if token_id else None
            if price is None:
                continue
            position_usd = shares * price
            if position_usd < cfg.BACKTEST.min_holder_position_usd:
                continue
            holders.append(Holder(
                wallet=wallet, side=outcome, position_usd=position_usd,
                score_percentile=score, tier=raw.tier, n_trades=raw.n,
                first_active_ts=raw.first_active_ts, fetch_status=fetch_status,
                avg_bet_usd=avg_bet_usd, win_rate_weighted=raw.win_rate_weighted,
                first_seen_at=first_seen.get((wallet, outcome)),
            ))
    return holders


def _holders_at_fast(
    t: int, shares_at_t: dict, first_seen: dict, token_for_side: dict,
    price_series_by_token: dict, price_timestamps_by_token: dict, positions_by_wallet: dict,
    positions_ts_by_wallet: dict, fetch_status_by_wallet: dict, raw_stats_cache: dict,
) -> list:
    """
    Exactly _holders_at()'s output (see test_holders_at_fast_matches_holders_at_
    on_random_data for a direct equivalence check), built from precomputed
    bulk structures instead of one SQL query per wallet/token per checkpoint
    -- the dominant cost in a backtest before this fix (measured: ~75% of a
    checkpoint's wall-clock time was DB round-trips, not the trade-tape scan
    or the scoring math itself). See bulk_share_timeline / bulk_wallet_positions
    / bulk_fetch_status / bulk_price_series in backtest_data.py for how the
    inputs here are built (once per market, not once per checkpoint).

    `raw_stats_cache` is a plain dict the caller creates ONCE per market and
    passes through every checkpoint call: keyed by (wallet, prefix_len) where
    prefix_len is how many of that wallet's positions have closed by t. A
    wallet's trader_raw_stats()/smart_score_from_raw() output is a pure
    function of exactly that prefix, not of t itself -- the one exception is
    confidence_tier()'s Gold-vs-Silver split (which depends on how long ago
    the wallet was last active RELATIVE TO t), but nothing anywhere reads
    that distinction: only `tier == "Unrated"` (smart_score_from_raw's gate)
    and `tier == "Bronze"` (scoring.effective_position_usd's down-weight) are
    ever checked, and both are fully determined by n/volume alone, never by
    t. So caching by (wallet, prefix_len) is exactly equivalent to
    recomputing fresh every checkpoint -- and since most wallets don't close
    a new position every single checkpoint, most checkpoints hit the cache.
    """
    by_wallet: dict = {}
    for (wallet, outcome), shares in shares_at_t.items():
        if shares <= 0:
            continue
        by_wallet.setdefault(wallet, []).append((outcome, shares))

    holders = []
    for wallet, sides in by_wallet.items():
        positions = positions_by_wallet.get(wallet, [])
        ts_list = positions_ts_by_wallet.get(wallet, [])
        prefix_len = bisect.bisect_right(ts_list, t)
        cache_key = (wallet, prefix_len)
        cached = raw_stats_cache.get(cache_key)
        if cached is None:
            raw = trader_raw_stats(wallet, positions[:prefix_len], t)
            score = smart_score_from_raw(raw)
            cached = (raw, score)
            raw_stats_cache[cache_key] = cached
        raw, score = cached
        fetch_status = fetch_status_by_wallet.get(wallet, "error")
        avg_bet_usd = (raw.volume / raw.n) if raw.n else None

        for outcome, shares in sides:
            token_id = token_for_side.get(outcome)
            series = price_series_by_token.get(token_id) if token_id else None
            ts_list = price_timestamps_by_token.get(token_id) if token_id else None
            price = price_at_or_before_series(series, t, timestamps=ts_list) if series is not None else None
            if price is None:
                continue
            position_usd = shares * price
            if position_usd < cfg.BACKTEST.min_holder_position_usd:
                continue
            holders.append(Holder(
                wallet=wallet, side=outcome, position_usd=position_usd,
                score_percentile=score, tier=raw.tier, n_trades=raw.n,
                first_active_ts=raw.first_active_ts, fetch_status=fetch_status,
                avg_bet_usd=avg_bet_usd, win_rate_weighted=raw.win_rate_weighted,
                first_seen_at=first_seen.get((wallet, outcome)),
            ))
    return holders


# Shared with the (future) live trading watcher -- see app.signals' module
# docstring for why this lives there now instead of here. Re-exported under
# their original names so every existing call site/import in this module and
# in tests keeps working unchanged.
from .signals import (  # noqa: E402
    STRENGTH_RANK as _STRENGTH_RANK,
    build_insider_cfg, build_verdict_cfg,
    insider_signal as _insider_signal,
    parse_verdict_side as _parse_verdict_side,
    smart_money_signal as _smart_money_signal,
)


def _summarize_run(run_id: str) -> dict:
    with db.db_session() as conn:
        trades = conn.execute("SELECT * FROM backtest_trades_sim WHERE run_id=?", (run_id,)).fetchall()
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": None, "total_pnl_usd": 0.0, "total_staked_usd": 0.0,
            "roi_pct": None, "avg_pnl_usd": None, "by_category": {}, "by_signal_type": {},
        }
    wins = sum(1 for t in trades if t["won"])
    total_pnl = sum(t["pnl_usd"] for t in trades)
    total_staked = sum(t["stake_usd"] for t in trades)
    by_category: dict = {}
    by_signal_type: dict = {}
    for t in trades:
        cat = t["category"] or "Uncategorized"
        c = by_category.setdefault(cat, {"n": 0, "wins": 0, "pnl_usd": 0.0})
        c["n"] += 1
        c["wins"] += 1 if t["won"] else 0
        c["pnl_usd"] += t["pnl_usd"]

        # Which exact case fired (Strong Buy vs. Lean vs. Fade, or 'insider')
        # -- a run allowing all three otherwise hides whether the result
        # comes from the strict, high-conviction case or is being carried
        # (or dragged down) by the loosest fallback lumped in with it.
        sig = t["signal_type"] or "unknown"
        s = by_signal_type.setdefault(sig, {"n": 0, "wins": 0, "pnl_usd": 0.0, "staked_usd": 0.0})
        s["n"] += 1
        s["wins"] += 1 if t["won"] else 0
        s["pnl_usd"] += t["pnl_usd"]
        s["staked_usd"] += t["stake_usd"]

    return {
        "n_trades": n,
        "win_rate": round(wins / n, 4),
        "total_pnl_usd": round(total_pnl, 2),
        "total_staked_usd": round(total_staked, 2),
        "roi_pct": round(100.0 * total_pnl / total_staked, 2) if total_staked > 0 else None,
        "avg_pnl_usd": round(total_pnl / n, 2),
        "by_category": {
            cat: {**v, "win_rate": round(v["wins"] / v["n"], 4), "pnl_usd": round(v["pnl_usd"], 2)}
            for cat, v in by_category.items()
        },
        "by_signal_type": {
            sig: {
                **v, "win_rate": round(v["wins"] / v["n"], 4), "pnl_usd": round(v["pnl_usd"], 2),
                "roi_pct": round(100.0 * v["pnl_usd"] / v["staked_usd"], 2) if v["staked_usd"] > 0 else None,
            }
            for sig, v in by_signal_type.items()
        },
    }
