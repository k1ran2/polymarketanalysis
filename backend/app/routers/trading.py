"""
Live trading API -- every route here requires the X-Local-Token header (see
app.local_auth for why CORS alone isn't enough once money-shaped endpoints
exist). Trading build order: schema -> risk caps -> watcher against a mock
trader -> credential storage (this router now covers all of that) -> real
order construction/signing, which does NOT exist yet -- see clob_trader.py
(not built). Storing a private key here has no effect on any trade: the
watcher only ever talks to MockTrader regardless of whether credentials are
connected, until real order-signing code exists to use them.

Every trade reachable through this router today is, by construction, fake
-- app.trade_watcher.MockTrader is the only trader implementation that
exists. GET /status's `mock_mode: true` is not a display choice, it's a
fact, independent of `credentials_configured`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from .. import trading_credentials as creds
from ..local_auth import require_local_token
from ..trade_tracker import summarize_trades
from ..trade_watcher import MockTrader, get_or_create_day_ledger, get_settings, start_manual_scan, status as watcher_status

router = APIRouter(prefix="/api/trading", tags=["trading"], dependencies=[Depends(require_local_token)])


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TradingSettingsIn(BaseModel):
    enabled: bool = False
    strategy: str = Field(default="smart_money", pattern="^(smart_money|insider|both)$")
    max_stake_usd: float = Field(default=1.0, gt=0)
    max_open_positions: int = Field(default=3, ge=0)
    max_open_exposure_usd: float = Field(default=5.0, ge=0)
    max_position_per_market_usd: float = Field(default=1.0, gt=0)
    max_daily_loss_usd: float = Field(default=10.0, gt=0)
    max_daily_spend_usd: float = Field(default=25.0, gt=0)
    max_trades_per_day: int = Field(default=5, ge=0)
    min_conviction: float = Field(default=70.0, ge=0, le=100)
    min_signal_strength: str = Field(default="strong_buy", pattern="^(strong_buy|lean|fade)$")
    max_entry_price: float = Field(default=0.90, ge=0.5, le=1.0)
    min_entry_price: float = Field(default=0.05, ge=0.0, le=0.5)
    max_slippage_pct: float = Field(default=2.0, ge=0)
    min_market_liquidity_usd: float = Field(default=5000.0, ge=0)
    min_hours_to_resolution: float = Field(default=6.0, ge=0)
    order_type: str = Field(default="FOK", pattern="^(FOK|GTC)$")
    poll_interval_seconds: int = Field(default=300, ge=30)
    universe_size: int = Field(default=40, ge=1, le=500)
    resignal_cooldown_hours: float = Field(default=24.0, ge=0)


_DEFAULT_SETTINGS = TradingSettingsIn().model_dump()


@router.get("/status")
def get_status():
    """
    Operating state: whether trading is on, whether today is auto-halted
    (and why), the watcher's own scanning state, today's spend/pnl/count,
    open position count/exposure -- everything the top-bar status chip and
    the Trading page need. `mock_mode` is always true right now (see this
    module's docstring) -- there is no credential storage or real trader in
    this codebase yet.
    """
    with db.db_session() as conn:
        settings = get_settings(conn) or _DEFAULT_SETTINGS
        ledger = get_or_create_day_ledger(conn, _today_utc())
        open_rows = conn.execute(
            "SELECT cost_usd, requested_stake_usd FROM trades WHERE status IN ('pending','submitted','filled','partial')"
        ).fetchall()
        cred_status = creds.get_status(conn)
    open_exposure = sum((r["cost_usd"] or r["requested_stake_usd"] or 0.0) for r in open_rows)
    return {
        "enabled": bool(settings.get("enabled", False)),
        "mock_mode": True,  # no real order-signing code exists yet -- see module docstring
        "credentials_configured": cred_status["configured"],
        "halted_today": bool(ledger["halted"]),
        "halted_reason": ledger["halted_reason"],
        "today": {
            "day": _today_utc(), "spent_usd": ledger["spent_usd"],
            "realized_pnl_usd": ledger["realized_pnl_usd"], "n_trades": ledger["n_trades"],
        },
        "open_positions": len(open_rows),
        "open_exposure_usd": open_exposure,
        **watcher_status(),
    }


@router.get("/settings")
def get_trading_settings():
    with db.db_session() as conn:
        settings = get_settings(conn)
    return settings or _DEFAULT_SETTINGS


@router.put("/settings")
def put_trading_settings(body: TradingSettingsIn):
    now_ts = int(time.time())
    data = body.model_dump()
    cols = list(data.keys())
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO trading_settings (id, {cols}, updated_at) VALUES (1, {qs}, ?) "
            "ON CONFLICT(id) DO UPDATE SET {sets}, updated_at=excluded.updated_at".format(
                cols=", ".join(cols),
                qs=", ".join("?" * len(cols)),
                sets=", ".join(f"{c}=excluded.{c}" for c in cols),
            ),
            tuple(data[c] for c in cols) + (now_ts,),
        )
        settings = get_settings(conn)
    return settings


class ConnectCredentialsIn(BaseModel):
    private_key: str = Field(min_length=32, description="Your wallet's private key -- paste it here, never anywhere else")
    funder_address: Optional[str] = Field(default=None, description="Only needed for a Polymarket proxy/Gnosis Safe wallet -- leave blank for a plain wallet")
    signature_type: int = Field(default=0, ge=0, le=2, description="0 = plain EOA wallet, 1 = Polymarket proxy, 2 = Gnosis Safe")


@router.get("/credentials")
def get_credentials_status():
    """Public info only -- signer/funder address, whether a key is stored.
    NEVER the key itself, encrypted or not -- see app.trading_credentials'
    module docstring."""
    with db.db_session() as conn:
        return creds.get_status(conn)


@router.post("/credentials")
def connect_credentials(body: ConnectCredentialsIn):
    """
    Stores your private key, encrypted, on this machine only -- see
    app.crypto_store and app.trading_credentials for exactly how, and the
    Trading page's connect-wallet copy for why a DEDICATED wallet (funded
    only with money you'd accept losing) is the real safety control here,
    not the encryption.

    This alone does not enable any trade: there is no order-signing code in
    this codebase yet (see this router's module docstring) -- the watcher
    still only ever talks to MockTrader regardless of whether credentials
    are connected.
    """
    try:
        with db.db_session() as conn:
            result = creds.save_credentials(
                conn, body.private_key, funder_address=body.funder_address, signature_type=body.signature_type,
            )
    except creds.InvalidPrivateKeyError as e:
        raise HTTPException(400, str(e)) from e
    return result


@router.delete("/credentials")
def disconnect_credentials():
    """Deletes the stored credentials and wipes the encryption key -- even
    a surviving backup of the database becomes permanently undecryptable
    after this."""
    with db.db_session() as conn:
        creds.clear_credentials(conn)
    return {"configured": False}


@router.post("/pause")
def pause_trading():
    """The kill switch. Independent of the daily auto-halt -- see
    trading_day_ledger's docstring for why those are kept separate."""
    with db.db_session() as conn:
        settings = get_settings(conn)
        if settings is None:
            raise HTTPException(400, "Configure trading settings before pausing/resuming.")
        conn.execute("UPDATE trading_settings SET enabled=0, updated_at=? WHERE id=1", (int(time.time()),))
    return {"enabled": False}


@router.post("/resume")
def resume_trading():
    with db.db_session() as conn:
        settings = get_settings(conn)
        if settings is None:
            raise HTTPException(400, "Configure trading settings before pausing/resuming.")
        conn.execute("UPDATE trading_settings SET enabled=1, updated_at=? WHERE id=1", (int(time.time()),))
    return {"enabled": True}


@router.post("/scan-now")
def scan_now():
    """
    One watcher cycle right now, in the background, against MockTrader (see
    module docstring -- no real trader exists). Returns immediately; poll
    GET /status until scanning is false, then GET /trades or /signals for
    what happened.
    """
    started = start_manual_scan(MockTrader())
    return {"started": started, **watcher_status()}


@router.get("/signals")
def get_signals(limit: int = Query(50, le=500)):
    """The shadow log: every signal the watcher evaluated, traded or
    skipped, with why -- most recent first."""
    with db.db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_signals ORDER BY detected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"signals": [dict(r) for r in rows]}


@router.get("/trades")
def get_trades(
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    query = "SELECT * FROM trades WHERE 1=1"
    args: list = []
    if status:
        query += " AND status=?"
        args.append(status)
    if source:
        query += " AND source=?"
        args.append(source)
    query += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db.db_session() as conn:
        rows = conn.execute(query, tuple(args)).fetchall()
    return {"trades": [dict(r) for r in rows]}


@router.get("/performance")
def get_performance():
    """Same shape as a backtest run's summary (n_trades, win_rate,
    total_pnl_usd, roi_pct, by_category, by_signal_type) -- see
    app.trade_tracker's module docstring for why, so the frontend's
    existing results components render this with no new code."""
    with db.db_session() as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    return summarize_trades([dict(r) for r in rows])


class ManualTradeIn(BaseModel):
    condition_id: str
    question: Optional[str] = None
    category: Optional[str] = None
    side: str = Field(pattern="^(YES|NO)$")
    entry_price: float = Field(gt=0, le=1)
    stake_usd: float = Field(gt=0)
    notes: Optional[str] = None


@router.post("/trades")
def log_manual_trade(body: ManualTradeIn):
    """Log a trade you placed yourself (outside this bot) so the tracker's
    P&L view reflects your real activity, not just the bot's -- see
    app.trade_tracker's module docstring for why manual and bot trades share
    one table instead of two views that could disagree."""
    now_ts = int(time.time())
    shares = body.stake_usd / body.entry_price
    with db.db_session() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (source, condition_id, question, category, side, requested_price,
                requested_size_shares, requested_stake_usd, order_type, status, filled_shares,
                avg_fill_price, cost_usd, submitted_at, filled_at, notes, created_at)
            VALUES ('manual', ?, ?, ?, ?, ?, ?, ?, 'manual', 'filled', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.condition_id, body.question, body.category, body.side, body.entry_price,
                shares, body.stake_usd, shares, body.entry_price, body.stake_usd,
                now_ts, now_ts, body.notes, now_ts,
            ),
        )
        trade_id = cur.lastrowid
    return {"trade_id": trade_id}
