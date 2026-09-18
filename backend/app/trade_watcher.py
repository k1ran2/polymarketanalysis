"""
Live trading watcher: periodically scans currently-tracked, still-open
markets, computes THIS cycle's signal using the exact same functions the
backtest engine uses (app.signals -- see that module's docstring for why
this matters), and -- only if trading_settings.enabled, the day isn't
auto-halted, and app.risk.check_caps allows it -- places a trade through
the injected `trader`.

Mirrors app.poller's structural pattern (_stop/_scanning Events, a
_start_guard Lock, poll_once()/start_manual_scan()/status()) deliberately,
since it's the same "background thread inside the API process" shape.

Built and tested against MockTrader, which records what it would have
submitted and fabricates a plausible fill -- not against a real exchange.
There is no real trader in this codebase yet: that requires stored
credentials (app.trading_credentials, not built yet) and a CLOB client
wrapper whose submit half refuses to run without the user's own
POLY_LIVE_TRADING=I_UNDERSTAND environment variable. Until that exists,
every trade this module ever creates is, by construction, a MockTrader
trade -- there is no path to a real order.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import db
from . import risk
from . import signals
from .market_analysis import analyze_market
from .market_service import get_holders_for_market

_stop = threading.Event()
_thread: threading.Thread | None = None
_scanning = threading.Event()
_start_guard = threading.Lock()
_last_completed_at: Optional[int] = None


class MockTrader:
    """Records intended orders without ever submitting anything to a real
    exchange -- the only trader implementation that exists in this codebase.
    Fabricates a full fill at the requested price so the rest of the
    pipeline (day-ledger updates, cap enforcement across cycles, P&L once a
    market resolves) can be exercised and tested end-to-end even though
    nothing real happened."""

    def __init__(self):
        self.calls: list = []

    def submit_order(self, condition_id: str, token_id: str, side: str, price: float, stake_usd: float) -> dict:
        shares = stake_usd / price if price > 0 else 0.0
        call = {
            "condition_id": condition_id, "token_id": token_id, "side": side,
            "price": price, "stake_usd": stake_usd, "shares": shares,
        }
        self.calls.append(call)
        return {
            "status": "filled", "exchange_order_id": f"mock-{uuid.uuid4().hex[:12]}",
            "filled_shares": shares, "avg_fill_price": price, "fees_usd": 0.0, "cost_usd": stake_usd,
        }


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_settings(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM trading_settings WHERE id=1").fetchone()
    return dict(row) if row else None


def get_or_create_day_ledger(conn, day: str) -> dict:
    row = conn.execute("SELECT * FROM trading_day_ledger WHERE day=?", (day,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO trading_day_ledger (day, updated_at) VALUES (?, ?)", (day, int(time.time())))
        row = conn.execute("SELECT * FROM trading_day_ledger WHERE day=?", (day,)).fetchone()
    return dict(row)


def _open_positions(conn) -> list:
    rows = conn.execute(
        "SELECT condition_id, cost_usd, requested_stake_usd FROM trades "
        "WHERE status IN ('pending','submitted','filled','partial')"
    ).fetchall()
    return [dict(r) for r in rows]


def _already_signaled_recently(conn, condition_id: str, strategy: str, side: str, cooldown_hours: float) -> bool:
    cutoff = int(time.time()) - int(cooldown_hours * 3600)
    row = conn.execute(
        "SELECT 1 FROM trade_signals WHERE condition_id=? AND strategy=? AND side=? "
        "AND action='traded' AND detected_at >= ? LIMIT 1",
        (condition_id, strategy, side, cutoff),
    ).fetchone()
    return row is not None


def _hours_to_resolution(end_date: Optional[str], now_ts: int) -> Optional[float]:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return (dt.timestamp() - now_ts) / 3600.0
    except (ValueError, TypeError):
        return None


def _record_signal(
    conn, condition_id: str, token_id: Optional[str], side: str, strategy: str, signal_type: Optional[str],
    conviction: float, market_price: Optional[float], action: str, skip_reason: Optional[str],
    trade_id: Optional[int] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO trade_signals (condition_id, token_id, side, strategy, signal_type, conviction,
            market_price, detected_at, action, skip_reason, trade_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (condition_id, token_id, side, strategy, signal_type, conviction, market_price,
         int(time.time()), action, skip_reason, trade_id),
    )


def _tracked_open_markets(conn, limit: int) -> list:
    """Currently open (not yet closed) markets this app already tracks,
    highest volume first -- the same discovery source app.poller uses for
    insider scanning, just excluding already-resolved ones (no point
    signal-evaluating a market that's already closed)."""
    rows = conn.execute(
        """
        SELECT DISTINCT m.condition_id FROM markets m
        WHERE m.closed = 0 AND (m.list_rank IS NOT NULL OR EXISTS (
            SELECT 1 FROM market_holders_cache h WHERE h.condition_id = m.condition_id
        ))
        ORDER BY m.volume DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r["condition_id"] for r in rows]


def evaluate_market(conn, cid: str, settings: dict, ledger: dict, trader, now_ts: int) -> dict:
    """One market's full evaluate -> gate -> (maybe) trade pass. Returns a
    small dict describing what happened, for poll_once's counters/logging."""
    row = conn.execute(
        "SELECT question, category, outcome_prices, clob_token_ids, liquidity, end_date, closed "
        "FROM markets WHERE condition_id=?", (cid,),
    ).fetchone()
    if row is None or row["closed"]:
        return {"action": "skipped", "reason": "not_found_or_closed"}

    outcome_prices = json.loads(row["outcome_prices"]) if row["outcome_prices"] else None
    token_ids = json.loads(row["clob_token_ids"]) if row["clob_token_ids"] else None
    if not outcome_prices or not token_ids or len(token_ids) < 2:
        return {"action": "skipped", "reason": "incomplete_market_data"}
    price_yes = float(outcome_prices[0])

    if (row["liquidity"] or 0.0) < settings["min_market_liquidity_usd"]:
        _record_signal(conn, cid, None, "YES", settings["strategy"], None, 0.0, price_yes, "skipped", "liquidity")
        return {"action": "skipped", "reason": "liquidity"}

    hours_left = _hours_to_resolution(row["end_date"], now_ts)
    if hours_left is not None and hours_left < settings["min_hours_to_resolution"]:
        _record_signal(conn, cid, None, "YES", settings["strategy"], None, 0.0, price_yes, "skipped", "too_close_to_resolution")
        return {"action": "skipped", "reason": "too_close_to_resolution"}

    holders = get_holders_for_market(conn, cid)
    if not holders:
        return {"action": "skipped", "reason": "no_holders"}

    params = {
        "min_signal_strength": settings["min_signal_strength"],
        "min_conviction": settings["min_conviction"],
    }
    if settings["strategy"] == "insider":
        end_date_iso = row["end_date"]
        signal = signals.insider_signal(holders, now_ts, end_date_iso, params)
    else:
        signal = signals.smart_money_signal(holders, price_yes, params)

    if signal is None:
        return {"action": "skipped", "reason": "no_signal"}
    side, conviction, signal_type = signal
    token_id = token_ids[0] if side == "YES" else token_ids[1]
    entry_price = price_yes if side == "YES" else (1.0 - price_yes)

    if entry_price > settings["max_entry_price"] or entry_price < settings["min_entry_price"]:
        _record_signal(conn, cid, token_id, side, settings["strategy"], signal_type, conviction, entry_price, "skipped", "price_out_of_band")
        return {"action": "skipped", "reason": "price_out_of_band"}

    if _already_signaled_recently(conn, cid, settings["strategy"], side, settings["resignal_cooldown_hours"]):
        _record_signal(conn, cid, token_id, side, settings["strategy"], signal_type, conviction, entry_price, "skipped", "cooldown")
        return {"action": "skipped", "reason": "cooldown"}

    open_positions = _open_positions(conn)
    candidate = {"condition_id": cid, "stake_usd": settings["max_stake_usd"]}
    decision = risk.check_caps(settings, ledger, open_positions, candidate)
    if not decision.allow:
        _record_signal(conn, cid, token_id, side, settings["strategy"], signal_type, conviction, entry_price, "skipped", decision.reason)
        return {"action": "skipped", "reason": decision.reason}

    fill = trader.submit_order(cid, token_id, side, entry_price, settings["max_stake_usd"])
    cur = conn.execute(
        """
        INSERT INTO trades (source, condition_id, token_id, question, category, side, strategy,
            signal_type, signal_conviction, requested_price, requested_size_shares, requested_stake_usd,
            order_type, client_order_id, status, exchange_order_id, filled_shares, avg_fill_price,
            fees_usd, cost_usd, submitted_at, filled_at, created_at)
        VALUES ('bot',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cid, token_id, row["question"], row["category"], side, settings["strategy"], signal_type, conviction,
            entry_price, settings["max_stake_usd"] / entry_price if entry_price > 0 else 0.0, settings["max_stake_usd"],
            settings["order_type"], uuid.uuid4().hex, fill["status"], fill["exchange_order_id"],
            fill["filled_shares"], fill["avg_fill_price"], fill["fees_usd"], fill["cost_usd"],
            now_ts, now_ts if fill["status"] == "filled" else None, now_ts,
        ),
    )
    trade_id = cur.lastrowid
    _record_signal(conn, cid, token_id, side, settings["strategy"], signal_type, conviction, entry_price, "traded", None, trade_id=trade_id)
    conn.execute(
        "UPDATE trading_day_ledger SET spent_usd = spent_usd + ?, n_trades = n_trades + 1, updated_at=? WHERE day=?",
        (fill["cost_usd"], now_ts, _today_utc()),
    )
    return {"action": "traded", "trade_id": trade_id, "condition_id": cid, "side": side}


def poll_once(trader, progress=None) -> dict:
    """One watcher cycle. Returns {"evaluated", "traded", "skipped"} counts."""
    log = progress or print
    global _last_completed_at
    _scanning.set()
    counts = {"evaluated": 0, "traded": 0, "skipped": 0}
    try:
        with db.db_session() as conn:
            settings = get_settings(conn)
        if settings is None or not settings["enabled"]:
            log("[trade_watcher] disabled -- skipping cycle")
            return counts

        day = _today_utc()
        with db.db_session() as conn:
            ledger = get_or_create_day_ledger(conn, day)
        if ledger["halted"]:
            log(f"[trade_watcher] halted for {day} ({ledger.get('halted_reason')}) -- skipping cycle")
            return counts

        with db.db_session() as conn:
            condition_ids = _tracked_open_markets(conn, settings["universe_size"])
        log(f"[trade_watcher] cycle: {len(condition_ids)} candidate markets")

        now_ts = int(time.time())
        for cid in condition_ids:
            if _stop.is_set():
                break
            counts["evaluated"] += 1
            try:
                analyze_market(cid)  # refresh holders + scores inline, same as app.poller
                with db.db_session() as conn:
                    outcome = evaluate_market(conn, cid, settings, ledger, trader, now_ts)
            except Exception as e:  # noqa: BLE001 - one bad market must not kill the cycle
                log(f"[trade_watcher]   {cid[:12]} failed: {e}")
                continue
            if outcome["action"] == "traded":
                counts["traded"] += 1
                log(f"[trade_watcher]   TRADED {cid[:12]} {outcome['side']}")
            else:
                counts["skipped"] += 1
        log(f"[trade_watcher] cycle done: evaluated={counts['evaluated']} traded={counts['traded']} skipped={counts['skipped']}")
        return counts
    finally:
        _last_completed_at = int(time.time())
        _scanning.clear()


def start_manual_scan(trader) -> bool:
    with _start_guard:
        if _scanning.is_set():
            return False
        _scanning.set()
    threading.Thread(target=poll_once, args=(trader,), name="manual-trade-scan", daemon=True).start()
    return True


def status() -> dict:
    return {"scanning": _scanning.is_set(), "last_cycle_completed_at": _last_completed_at}
