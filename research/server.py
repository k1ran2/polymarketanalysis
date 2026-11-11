"""Small API behind the research UI.

    uvicorn research.server:app --port 8000

Read-only except /api/lab/run, which runs a backtest on local files (no
network, well under a second) and returns the report.
"""
from __future__ import annotations

import json
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import engine, store, strategies

app = FastAPI(title="Polymarket research")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in (5173, 5174, 5175)],
    allow_methods=["*"], allow_headers=["*"],
)


def _result(name: str):
    path = store.RESULTS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"{name} has not been published yet. Run: python -m research.publish")
    return json.loads(path.read_text())


@app.get("/api/overview")
def overview():
    return _result("overview")


@app.get("/api/strategies")
def get_strategies():
    return _result("strategies")


@app.get("/api/scan")
def scan():
    return {"summary": _result("scan_summary"), "rules": _result("scan")}


@app.get("/api/calibration")
def calibration():
    return _result("calibration")


@app.get("/api/traders")
def traders():
    return _result("traders")


class LabRequest(BaseModel):
    """One simple rule: buy a side when its price and the market's state are in range."""
    side: str = Field(default="favourite", pattern="^(favourite|underdog|outcome0|outcome1)$")
    price_min: float = Field(default=0.80, ge=0.01, le=0.99)
    price_max: float = Field(default=0.95, ge=0.01, le=0.99)
    categories: list[str] = Field(default_factory=list)          # empty = all
    days_to_end_min: Optional[float] = None
    days_to_end_max: Optional[float] = None
    momentum_min: Optional[float] = None                         # 2-day price change of the side bought
    momentum_max: Optional[float] = None
    half_spread: float = Field(default=0.01, ge=0.0, le=0.10)
    fees: bool = True


def equity_curve(trades: pd.DataFrame, max_points: int = 300) -> list:
    if len(trades) == 0:
        return []
    daily = trades.groupby((trades["exit_ts"] // 86400).astype(int))["pnl"].sum().sort_index().cumsum()
    step = max(1, len(daily) // max_points)
    daily = pd.concat([daily.iloc[::step], daily.iloc[[-1]]])
    daily = daily[~daily.index.duplicated()]
    return [{"t": int(d) * 86400, "pnl": round(float(v), 2)} for d, v in daily.items()]


def lab_strategy(req: LabRequest):
    def f(df):
        p0 = df["p0"].to_numpy()
        if req.side == "favourite":
            side = np.where(p0 >= 0.5, 0, 1)
        elif req.side == "underdog":
            side = np.where(p0 < 0.5, 0, 1)
        else:
            side = np.full(len(df), 0 if req.side == "outcome0" else 1)
        px = np.where(side == 0, p0, 1 - p0)
        mom = np.where(side == 0, df["mom4"].to_numpy(), -df["mom4"].to_numpy())
        ok = (px >= req.price_min) & (px <= req.price_max)
        if req.categories:
            ok &= df["category"].isin(req.categories).to_numpy()
        if req.days_to_end_min is not None:
            ok &= df["days_to_end"].to_numpy() >= req.days_to_end_min
        if req.days_to_end_max is not None:
            ok &= df["days_to_end"].to_numpy() <= req.days_to_end_max
        if req.momentum_min is not None:
            ok &= mom >= req.momentum_min
        if req.momentum_max is not None:
            ok &= mom <= req.momentum_max
        return pd.Series(np.where(ok, side, -1), index=df.index)
    return strategies._named(f, "lab_rule", **req.model_dump())


@app.post("/api/lab/run")
def lab_run(req: LabRequest):
    if req.price_min >= req.price_max:
        raise HTTPException(400, "price_min must be below price_max")
    strat = lab_strategy(req)
    out = {}
    for split in ("train", "validation"):   # the test period is never reachable from the UI
        rep = engine.backtest(strat, split=split, hypothesis="strategy lab", half_spread=req.half_spread, fees=req.fees)
        out[split] = {"stats": rep.stats, "equity": equity_curve(rep.trades)}
    base = engine.backtest(strategies.random_side(), split="validation", log=False,
                           half_spread=req.half_spread, fees=req.fees)
    out["baseline_random_validation"] = base.stats
    return out
