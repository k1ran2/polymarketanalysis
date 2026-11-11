"""The observation panel: one row per (market, price point), with only
information that was knowable at that moment, plus the price a trade placed
then would actually have got.

Timing rules (the no-lookahead contract):
  * A price point stamped `ts` summarises the bucket [ts, ts + width). It is
    treated as known only at the END of that bucket (`decide_ts`).
  * A trade decided on point i fills at point i+1's price, i.e. one whole
    bucket later, and only if that bucket ended before the outcome was known.
  * Features use the scheduled end date (`end_ts`), never the real resolution
    time, and never the market's final volume.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from . import store

# Taker fee = shares * rate * p * (1 - p)  (docs.polymarket.com/trading/fees, Nov 2026).
FEE_RATE = {"Crypto": 0.07, "Sports": 0.05, "Economy": 0.04, "Politics": 0.04, "Tech": 0.04,
            "Culture": 0.05, "Weather": 0.05, "Other": 0.05, "Science": 0.05, "World": 0.0}


def _change_over(key: np.ndarray, market: np.ndarray, p0: np.ndarray, ts: np.ndarray, seconds: float) -> np.ndarray:
    """p0 now minus p0 at the last observation at least `seconds` ago, same market (NaN if none)."""
    j = np.searchsorted(key, key - seconds, side="right") - 1
    ok = (j >= 0) & (market[np.clip(j, 0, None)] == market) & (ts - ts[np.clip(j, 0, None)] <= 3 * seconds)
    return np.where(ok, p0 - p0[np.clip(j, 0, None)], np.nan)


@lru_cache(maxsize=2)
def build(universe: str = "sample") -> pd.DataFrame:
    """universe='sample': the unbiased random sample (use for anything about prices).
    universe='all': every market with prices, including the volume-selected ones."""
    m = store.markets()
    if universe == "sample":
        m = m[m["in_universe"]]
    p = store.prices()
    df = p.merge(m[["condition_id", "event_id", "category", "neg_risk", "start_ts", "end_ts", "resolved_ts",
                    "payout0", "split", "outcome0"]], on="condition_id", how="inner")
    df = df.sort_values(["condition_id", "ts"], kind="stable").reset_index(drop=True)
    g = df.groupby("condition_id", sort=False)

    width = (g["ts"].shift(-1) - df["ts"]).fillna(43200).clip(upper=43200)
    df["decide_ts"] = df["ts"] + width
    df["fill_p0"] = g["p0"].shift(-1)
    fill_ts = g["ts"].shift(-1)
    fill_end = fill_ts + (g["ts"].shift(-2) - fill_ts).fillna(43200).clip(upper=43200)
    # Usable only if the fill bucket closed before the outcome was known.
    df["tradable"] = df["fill_p0"].notna() & (fill_end <= df["resolved_ts"]) & df["payout0"].isin([0.0, 1.0, 0.5])

    day = 86400.0
    df["days_to_end"] = (df["end_ts"] - df["decide_ts"]) / day
    df["age_days"] = (df["decide_ts"] - df["start_ts"]) / day
    # Price changes over fixed time spans (bucket width varies between markets).
    mkt = pd.factorize(df["condition_id"])[0].astype(np.int64)
    ts, p0 = df["ts"].to_numpy(dtype=np.int64), df["p0"].to_numpy()
    key = mkt * 10_000_000_000 + ts
    df["mom1"] = _change_over(key, mkt, p0, ts, 12 * 3600)     # last 12 hours
    df["mom4"] = _change_over(key, mkt, p0, ts, 2 * 86400)     # last 2 days
    df["mom14"] = _change_over(key, mkt, p0, ts, 7 * 86400)    # last week
    df["vol6"] = g["p0"].transform(lambda s: s.diff().rolling(6, min_periods=3).std())
    df["n_obs"] = g.cumcount()
    df["fee_rate"] = df["category"].map(FEE_RATE).fillna(0.05)
    df["is_yesno"] = df["outcome0"].str.lower().eq("yes")
    df["hold_days"] = (df["resolved_ts"] - df["decide_ts"]) / day
    return df


def trade_returns(df: pd.DataFrame, side: np.ndarray, half_spread: float = 0.01, fees: bool = True) -> pd.DataFrame:
    """Per-row result of buying one side at the delayed fill price, held to resolution.

    side: 0 = buy outcome 0 (usually YES), 1 = buy outcome 1. Returns cost per
    share (price + half spread + fee) and profit per $1 staked.
    """
    q = np.where(side == 0, df["fill_p0"].to_numpy(), 1.0 - df["fill_p0"].to_numpy())
    payout = np.where(side == 0, df["payout0"].to_numpy(), 1.0 - df["payout0"].to_numpy())
    px = np.clip(q + half_spread, 0.001, 0.999)
    fee = df["fee_rate"].to_numpy() * px * (1.0 - px) if fees else 0.0
    cost = px + fee
    out = pd.DataFrame({"entry": px, "cost": cost, "payout": payout, "ret": (payout - cost) / cost}, index=df.index)
    return out
