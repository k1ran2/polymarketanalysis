"""Where research data lives on disk, and how to load it.

Everything is Parquet under research/data/ (git-ignored). Tables:
  markets.parquet   one row per resolved market (payout0, resolved_ts included)
  prices.parquet    (condition_id, ts, p0): price of outcome 0 over the market's life
  trades/*.parquet  fills (both sides) above a cash floor, one shard per batch
  trades_done.json  which markets have a complete tape, and at what floor
"""
from __future__ import annotations

import glob
import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
MARKETS = DATA_DIR / "markets.parquet"
PRICES = DATA_DIR / "prices.parquet"
TRADES_DIR = DATA_DIR / "trades"
TRADES_DONE = DATA_DIR / "trades_done.json"
RESULTS_DIR = DATA_DIR / "results"
RUN_LOG = DATA_DIR / "runs.jsonl"

# Split by the time each market's outcome became known. The test period is
# locked: research.engine refuses to use it unless asked explicitly, and
# every use is written to the run log.
TRAIN_END = pd.Timestamp("2025-10-01", tz="UTC").timestamp()
VALID_END = pd.Timestamp("2026-04-01", tz="UTC").timestamp()


# Analyses of price behaviour use a RANDOM sample of all markets above $10k
# volume. Picking markets by final volume instead (e.g. "over $1M") is
# hindsight: a long shot that wins attracts heavy trading, so surprise winners
# are over-represented and cheap outcomes look underpriced when they are not.
UNIVERSE_FRAC = 0.12


def in_sample(condition_ids: pd.Series, frac: float = UNIVERSE_FRAC) -> pd.Series:
    """Deterministic random sample by condition id (the id is a hash, so its tail is uniform)."""
    return condition_ids.str[-6:].apply(lambda h: int(h, 16) / 0xFFFFFF) < frac


def split_of(resolved_ts: pd.Series) -> pd.Series:
    out = pd.Series("test", index=resolved_ts.index)
    out[resolved_ts < VALID_END] = "validation"
    out[resolved_ts < TRAIN_END] = "train"
    return out


@lru_cache(maxsize=1)
def markets() -> pd.DataFrame:
    df = pd.read_parquet(MARKETS)
    df["split"] = split_of(df["resolved_ts"])
    df["in_universe"] = in_sample(df["condition_id"]) & df["resolved_ts"].notna()
    return df


@lru_cache(maxsize=1)
def prices() -> pd.DataFrame:
    return pd.read_parquet(PRICES).sort_values(["condition_id", "ts"], kind="stable").reset_index(drop=True)


def trades_done() -> dict:
    if TRADES_DONE.exists():
        return json.loads(TRADES_DONE.read_text())
    return {}


@lru_cache(maxsize=1)
def trades() -> pd.DataFrame:
    files = sorted(glob.glob(str(TRADES_DIR / "*.parquet")))
    if not files:
        return pd.DataFrame(columns=["condition_id", "wallet", "side", "outcome_index", "price", "size", "ts", "tx"])
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["wallet"] = df["wallet"].astype("category")
    df["condition_id"] = df["condition_id"].astype("category")
    return df
