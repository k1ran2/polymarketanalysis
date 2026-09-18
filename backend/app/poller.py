"""
Background poller (app.config.PollerConfig): while the backend process is
running, periodically re-analyzes every tracked market so insider alerts can
fire even when nobody has the dashboard open. Stops when the process stops --
this is not a real always-on service, just enough to make "alert on insider
positions" true for as long as `uvicorn` is up. A real 24/7 deployment would
run this as its own process/cron instead of a thread inside the API server.
"""
from __future__ import annotations

import threading
import time

from . import config as cfg
from . import db
from .alerts import send_insider_alert
from .insiders import scan_market, mark_alerted
from .market_analysis import analyze_market
from .market_service import get_holders_for_market

_stop = threading.Event()
_thread: threading.Thread | None = None
_scanning = threading.Event()   # true while any cycle (scheduled or manual) is running
_start_guard = threading.Lock()  # closes the check-then-set race in start_manual_scan
_last_completed_at: int | None = None


def _tracked_condition_ids(conn) -> list:
    rows = conn.execute(
        """
        SELECT DISTINCT condition_id FROM markets WHERE list_rank IS NOT NULL
        UNION
        SELECT DISTINCT condition_id FROM market_holders_cache
        """
    ).fetchall()
    return [r["condition_id"] for r in rows]


def poll_once(progress=None) -> int:
    """Re-analyze every tracked market once; return how many insider alerts fired."""
    global _last_completed_at
    log = progress or print
    _scanning.set()
    try:
        with db.db_session() as conn:
            condition_ids = _tracked_condition_ids(conn)
        log(f"[poller] cycle: {len(condition_ids)} tracked markets")

        fired = 0
        for cid in condition_ids:
            if _stop.is_set():
                break
            try:
                analyze_market(cid)  # refresh holders + score anything new/stale, inline
            except Exception as e:  # noqa: BLE001 - one bad market must not kill the cycle
                log(f"[poller]   analyze {cid[:12]} failed: {e}")
                continue

            with db.db_session() as conn:
                holders = get_holders_for_market(conn, cid)
                alertable = scan_market(conn, cid, holders)
                if not alertable:
                    continue
                row = conn.execute("SELECT question FROM markets WHERE condition_id=?", (cid,)).fetchone()
                question = row["question"] if row else None
                for flag, _cid in alertable:
                    send_insider_alert(flag, cid, question)  # logs if SMTP isn't configured
                    mark_alerted(conn, cid, flag.wallet, flag.side)  # once per newly-detected flag either way
                    fired += 1
        log(f"[poller] cycle done, {fired} new alerts")
        return fired
    finally:
        _last_completed_at = int(time.time())
        _scanning.clear()


def start_manual_scan() -> bool:
    """Kick one poll cycle in the background now, unless one's already running. Returns True if started."""
    with _start_guard:
        if _scanning.is_set():
            return False
        _scanning.set()  # claim immediately so a second click can't slip in before the thread starts
    threading.Thread(target=poll_once, name="manual-insider-scan", daemon=True).start()
    return True


def status() -> dict:
    return {
        "poller_enabled": cfg.POLLER.enabled,
        "poller_interval_seconds": cfg.POLLER.interval_seconds,
        "scanning": _scanning.is_set(),
        "last_scan_completed_at": _last_completed_at,
    }


def _loop():
    _stop.wait(10)  # let the app finish starting up first
    while not _stop.is_set():
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001 - the loop must survive any single failure
            print(f"[poller] cycle crashed: {e}")
        _stop.wait(cfg.POLLER.interval_seconds)


def start():
    global _thread
    if not cfg.POLLER.enabled:
        print("[poller] disabled (POLLER_ENABLED=false)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="insider-poller", daemon=True)
    _thread.start()
    print(f"[poller] started, interval={cfg.POLLER.interval_seconds}s")


def stop():
    _stop.set()
