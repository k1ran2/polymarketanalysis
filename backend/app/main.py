from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config as cfg
from . import db
from .routers import markets, search, insiders, backtest

app = FastAPI(
    title="Polymarket Smart Money Dashboard API",
    description="Read-only, informational-only. " + cfg.DISCLAIMER,
    version="0.1.0-phase1",
)

# Pinned to the actual Vite dev origins (default port + a couple of
# increments Vite falls back to if 5173 is taken), not "*" -- see
# app.local_auth's module docstring for why this alone still isn't enough
# once trading endpoints exist, and what the second layer is.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
        "http://localhost:5175", "http://127.0.0.1:5175",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(markets.router)
app.include_router(search.router)
app.include_router(insiders.router)
app.include_router(backtest.router)


@app.on_event("startup")
def on_startup():
    db.init_db()
    # Warm the market list in the background so the first page load has rows
    # even if the seed script wasn't run.
    from .market_list import maybe_refresh_async
    maybe_refresh_async()
    from . import poller
    poller.start()
    from . import backtest_engine
    backtest_engine.reconcile_orphaned_runs()

    from .local_auth import get_or_create_local_token
    token = get_or_create_local_token()
    print(
        "\n"
        "==================================================================\n"
        "Local access token (paste into the frontend's Settings page once\n"
        "trading is enabled -- required for /api/trading/* routes only):\n"
        f"  {token}\n"
        "==================================================================\n"
    )


@app.on_event("shutdown")
def on_shutdown():
    from . import poller
    poller.stop()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/disclaimer")
def disclaimer():
    return {"text": cfg.DISCLAIMER}
