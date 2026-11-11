"""Strategies: each is a function (as-of panel columns) -> Series of
0 (buy outcome 0), 1 (buy outcome 1) or -1 (no trade), one value per row.

Factories return a named function carrying its parameters, so the run log
records exactly what was tested.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _named(fn, name: str, **params):
    fn.__name__ = name
    fn.params = params
    return fn


# ---- baselines (no skill) ------------------------------------------------

def random_side(seed: int = 1):
    def f(df):
        rng = np.random.default_rng(seed)
        return pd.Series(rng.integers(0, 2, len(df)), index=df.index)
    return _named(f, "baseline_random_side", seed=seed)


def always(side: int):
    def f(df):
        return pd.Series(side, index=df.index)
    return _named(f, f"baseline_always_outcome{side}", side=side)


# ---- statistical biases --------------------------------------------------

def favourite(min_price: float = 0.85, max_price: float = 0.97, min_days: float = 0.0, max_days: float = 1e9,
              categories=None):
    """Buy whichever side the market already prices at or above `min_price`."""
    def f(df):
        p = df["p0"]
        fav_p = np.maximum(p, 1 - p)
        sig = np.where(p >= 0.5, 0, 1)
        ok = (fav_p >= min_price) & (fav_p <= max_price) & (df["days_to_end"] >= min_days) & (df["days_to_end"] <= max_days)
        if categories:
            ok &= df["category"].isin(categories)
        return pd.Series(np.where(ok, sig, -1), index=df.index)
    return _named(f, f"favourite_{min_price:.2f}", min_price=min_price, max_price=max_price, min_days=min_days,
                  max_days=max_days, categories=categories)


def longshot(max_price: float = 0.15, min_price: float = 0.03, categories=None):
    """Buy whichever side is priced at or below `max_price`."""
    def f(df):
        p = df["p0"]
        dog_p = np.minimum(p, 1 - p)
        sig = np.where(p < 0.5, 0, 1)
        ok = (dog_p <= max_price) & (dog_p >= min_price)
        if categories:
            ok &= df["category"].isin(categories)
        return pd.Series(np.where(ok, sig, -1), index=df.index)
    return _named(f, f"longshot_{max_price:.2f}", max_price=max_price, min_price=min_price, categories=categories)


def momentum(col: str = "mom4", threshold: float = 0.08, follow: bool = True, lo: float = 0.10, hi: float = 0.90):
    """Follow (or fade) a recent price move of at least `threshold`."""
    def f(df):
        m = df[col]
        up, down = m >= threshold, m <= -threshold
        side_up, side_down = (0, 1) if follow else (1, 0)
        sig = np.where(up, side_up, np.where(down, side_down, -1))
        ok = (df["p0"] >= lo) & (df["p0"] <= hi)
        return pd.Series(np.where(ok, sig, -1), index=df.index)
    return _named(f, f"{'follow' if follow else 'fade'}_{col}_{threshold:.2f}", col=col, threshold=threshold,
                  follow=follow, lo=lo, hi=hi)


def rule(conditions: dict, side: int, name: str = "rule"):
    """Generic rule from the scan: {column: (lo, hi)} ranges or {column: [values]} sets, all must hold."""
    def f(df):
        ok = pd.Series(True, index=df.index)
        for col, cond in conditions.items():
            if isinstance(cond, tuple):
                ok &= (df[col] >= cond[0]) & (df[col] < cond[1])
            else:
                ok &= df[col].isin(cond)
        return pd.Series(np.where(ok, side, -1), index=df.index)
    return _named(f, name, conditions={k: list(v) for k, v in conditions.items()}, side=side)
