"""Backtest engine: one call per idea, costs always on, split by time.

    from research import engine, strategies
    print(engine.backtest(strategies.favourite(0.85)).text())

A strategy is a function (panel DataFrame) -> Series of {0, 1, -1} per row:
buy outcome 0, buy outcome 1, or do nothing. It sees only the as-of columns
built by research.panel. The engine handles everything else the same way for
every strategy: delayed fills, spread, fees, one position per market, the
time split, baselines and the report.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from . import panel, store

FEATURES = ["p0", "days_to_end", "age_days", "mom1", "mom4", "mom14", "vol6", "category", "neg_risk",
            "is_yesno", "n_obs", "decide_ts", "condition_id", "event_id"]


@dataclass
class Report:
    name: str
    split: str
    trades: pd.DataFrame
    params: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def text(self) -> str:
        s = self.stats
        if not s.get("n_trades"):
            return f"{self.name} [{self.split}]: no trades"
        lines = [
            f"{self.name} [{self.split}]",
            f"  trades {s['n_trades']:,} in {s['n_events']:,} independent events",
            f"  return on stake {s['roi_pct']:+.2f}%  (95% CI {s['roi_lo']:+.2f}% to {s['roi_hi']:+.2f}%, by event)",
            f"  win rate {s['win_rate']:.1%} vs avg cost {s['avg_cost']:.3f}  -> edge {s['edge_pts']:+.2f} pts",
            f"  P&L ${s['pnl']:,.0f} on ${s['staked']:,.0f} staked | return per day of capital {s['ret_per_day_bps']:+.2f} bps",
            f"  Sharpe {s['sharpe']:.2f} | max drawdown ${s['max_dd']:,.0f} | avg hold {s['avg_hold_days']:.1f} days",
        ]
        return "\n".join(lines)


def _bootstrap_roi(pnl: np.ndarray, stake: np.ndarray, groups: np.ndarray, n: int = 2000, seed: int = 7) -> tuple:
    """95% CI for sum(pnl)/sum(stake), resampling whole events (trades in one event move together)."""
    codes, _ = pd.factorize(groups)
    k = codes.max() + 1
    gp = np.bincount(codes, weights=pnl, minlength=k)
    gs = np.bincount(codes, weights=stake, minlength=k)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n, k))
    roi = gp[idx].sum(axis=1) / gs[idx].sum(axis=1)
    return float(np.percentile(roi, 2.5)), float(np.percentile(roi, 97.5))


def summarise(t: pd.DataFrame) -> dict:
    """t needs: stake, pnl, cost, payout, event_id, hold_days, exit_ts."""
    if len(t) == 0:
        return {"n_trades": 0}
    pnl, stake = t["pnl"].to_numpy(), t["stake"].to_numpy()
    lo, hi = _bootstrap_roi(pnl, stake, t["event_id"].to_numpy())
    daily = t.groupby((t["exit_ts"] // 86400).astype(int))["pnl"].sum().sort_index()
    days = np.arange(daily.index.min(), daily.index.max() + 1)
    daily = daily.reindex(days, fill_value=0.0)
    equity = daily.cumsum()
    capital_days = float((stake * t["hold_days"].clip(lower=0.25)).sum())
    avg_capital = capital_days / max(len(days), 1)
    sharpe = float(daily.mean() / daily.std() * np.sqrt(365)) if daily.std() > 0 else 0.0
    return {
        "n_trades": int(len(t)), "n_events": int(t["event_id"].nunique()),
        "pnl": float(pnl.sum()), "staked": float(stake.sum()),
        "roi_pct": float(100 * pnl.sum() / stake.sum()), "roi_lo": 100 * lo, "roi_hi": 100 * hi,
        "win_rate": float((t["payout"] > 0.5).mean()), "avg_cost": float(t["cost"].mean()),
        "edge_pts": float(100 * (t["payout"].mean() - t["cost"].mean())),
        "ret_per_day_bps": float(1e4 * pnl.sum() / capital_days) if capital_days > 0 else 0.0,
        "sharpe": sharpe, "max_dd": float((equity - equity.cummax()).min()),
        "avg_hold_days": float(t["hold_days"].mean()), "avg_capital": avg_capital,
        "significant": bool(lo > 0),
    }


def run(signal: pd.Series, df: pd.DataFrame, half_spread: float = 0.01, fees: bool = True,
        stake: float = 100.0, max_slip: float = 0.03, min_px: float = 0.03, max_px: float = 0.97,
        one_per_market: bool = True) -> pd.DataFrame:
    """Turn a per-row signal into trades. Shared by backtests, the scan and baselines."""
    side = signal.to_numpy()
    mask = (side >= 0) & df["tradable"].to_numpy()
    d = df[mask]
    side = side[mask]
    r = panel.trade_returns(d, side, half_spread=half_spread, fees=fees)
    seen = np.where(side == 0, d["p0"].to_numpy(), 1.0 - d["p0"].to_numpy())
    # A limit order: no fill if the price ran away from us, or left the tradable band.
    ok = (r["entry"].to_numpy() - half_spread <= seen + max_slip) & (r["entry"].to_numpy() >= min_px) & (r["entry"].to_numpy() <= max_px)
    d, r, side = d[ok], r[ok], side[ok]
    t = pd.DataFrame({
        "condition_id": d["condition_id"].to_numpy(), "event_id": d["event_id"].to_numpy(),
        "category": d["category"].to_numpy(), "side": side, "entry_ts": d["decide_ts"].to_numpy(),
        "exit_ts": d["resolved_ts"].to_numpy(), "hold_days": d["hold_days"].to_numpy(),
        "entry": r["entry"].to_numpy(), "cost": r["cost"].to_numpy(), "payout": r["payout"].to_numpy(),
        "ret": r["ret"].to_numpy(),
    })
    if one_per_market:
        t = t.sort_values("entry_ts", kind="stable").drop_duplicates("condition_id", keep="first")
    t["stake"] = stake
    t["pnl"] = stake * t["ret"]
    return t.reset_index(drop=True)


def _git_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=store.DATA_DIR.parent,
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def backtest(strategy: Callable, split: str = "train", name: Optional[str] = None, hypothesis: str = "",
             log: bool = True, **run_kw) -> Report:
    """Run one strategy on one period. split: 'train' | 'validation' | 'test'.

    The test period is the final exam: it works, but every use is logged, and a
    strategy should meet it exactly once, after its rules are frozen.
    """
    if split not in ("train", "validation", "test"):
        raise ValueError(split)
    full = panel.build()
    df = full[full["split"] == split]
    sig = strategy(df[FEATURES]).reindex(df.index).fillna(-1).astype(int)
    trades = run(sig, df, **run_kw)
    rep = Report(name or getattr(strategy, "__name__", "strategy"), split, trades,
                 params=dict(getattr(strategy, "params", {}), **run_kw), stats=summarise(trades))
    if log:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(store.RUN_LOG, "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "git": _git_hash(), "name": rep.name, "split": split,
                                "hypothesis": hypothesis, "params": rep.params,
                                "stats": {k: v for k, v in rep.stats.items()}}, default=str) + "\n")
    return rep


def baselines(split: str = "train", **run_kw) -> dict:
    """What 'no skill' looks like under the same costs. Every strategy must beat these."""
    from . import strategies as st
    out = {}
    for s in (st.random_side(), st.always(1), st.always(0), st.favourite(0.5)):
        out[s.__name__] = backtest(s, split=split, log=False, **run_kw)
    return out
