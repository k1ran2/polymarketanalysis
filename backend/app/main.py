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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Phase 1: local dev only
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
