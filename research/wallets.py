"""Trader skill, measured the way a portfolio is: dollars made per dollar put
at risk, so a $50,000 position counts 10,000x a $5 one.

Built from the trade tape, not from Polymarket's per-wallet summaries, so it
is point-in-time by construction:
  * every fill is scored against how its market finally resolved;
  * a market counts toward a wallet's record only AFTER its outcome was known;
  * there is no cap on history length and losers are never missing.

Per fill:  BUY  -> capital = size * price,       pnl = size * (payout - price)
           SELL -> capital = size * (1 - price), pnl = size * (price - payout)
A round trip nets out correctly (buy 0.30, sell 0.60 = +0.30 a share whatever
the outcome).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from . import panel, store


@lru_cache(maxsize=1)
def fills() -> pd.DataFrame:
    t = store.trades()
    done = store.trades_done()
    complete = {cid for cid, meta in done.items() if not meta.get("truncated")}
    m = store.markets().set_index("condition_id")
    t = t[t["condition_id"].isin(complete) & t["outcome_index"].isin([0, 1])]
    cid = t["condition_id"].astype(str)
    payout0 = cid.map(m["payout0"]).to_numpy()
    resolved = cid.map(m["resolved_ts"]).to_numpy()
    price, size = t["price"].to_numpy(), t["size"].to_numpy()
    payout = np.where(t["outcome_index"].to_numpy() == 0, payout0, 1.0 - payout0)
    buy = (t["side"] == "BUY").to_numpy()
    out = pd.DataFrame({
        "condition_id": t["condition_id"].to_numpy(), "wallet": t["wallet"].to_numpy(), "ts": t["ts"].to_numpy(),
        "buy": buy, "outcome_index": t["outcome_index"].to_numpy().astype(np.int8),
        "price": price, "size": size,
        "p0": np.where(t["outcome_index"].to_numpy() == 0, price, 1.0 - price),   # price expressed for outcome 0
        "capital": np.where(buy, size * price, size * (1.0 - price)),
        "pnl": np.where(buy, size * (payout - price), size * (price - payout)),
        "resolved_ts": resolved,
    })
    out = out[np.isfinite(out["pnl"]) & (out["ts"] < out["resolved_ts"])]
    out["wallet"] = out["wallet"].astype("category")
    out["condition_id"] = out["condition_id"].astype("category")
    return out.reset_index(drop=True)


@lru_cache(maxsize=1)
def wallet_market() -> pd.DataFrame:
    """One row per (wallet, market): net pnl, capital at risk, when the result became known."""
    f = fills()
    g = f.groupby(["wallet", "condition_id"], observed=True)
    wm = g.agg(pnl=("pnl", "sum"), capital=("capital", "sum"), n_fills=("pnl", "size"),
               first_ts=("ts", "min"), resolved_ts=("resolved_ts", "first")).reset_index()
    return wm


def record(wm: pd.DataFrame) -> pd.DataFrame:
    """Per-wallet track record over the given (wallet, market) rows."""
    g = wm.groupby("wallet", observed=True)
    r = g.agg(pnl=("pnl", "sum"), capital=("capital", "sum"), n_markets=("pnl", "size"),
              mean=("pnl", "mean"), sd=("pnl", "std"), wins=("pnl", lambda s: (s > 0).sum()))
    r["roi"] = r["pnl"] / r["capital"]
    r["t"] = (r["mean"] / (r["sd"] / np.sqrt(r["n_markets"]))).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    r["win_rate"] = r["wins"] / r["n_markets"]
    return r.drop(columns=["mean", "sd", "wins"])


def persistence(min_markets: int = 20, min_capital: float = 20_000, cut_ts: float = store.TRAIN_END,
                end_ts: float = store.VALID_END, n_groups: int = 10) -> pd.DataFrame:
    """Does past skill predict future results? Rank wallets on markets resolved
    before `cut_ts`, then measure the same wallets on markets they ENTERED
    after `cut_ts` (and that resolved before `end_ts`)."""
    wm = wallet_market()
    past = record(wm[wm["resolved_ts"] < cut_ts])
    past = past[(past["n_markets"] >= min_markets) & (past["capital"] >= min_capital)]
    fut = wm[(wm["first_ts"] >= cut_ts) & (wm["resolved_ts"] < end_ts)]
    future = record(fut[fut["wallet"].isin(past.index)])
    j = past.join(future, rsuffix="_future", how="inner")
    j = j[j["n_markets_future"] >= 5]
    j["group"] = pd.qcut(j["t"].rank(method="first"), n_groups, labels=False) + 1
    out = j.groupby("group").agg(
        wallets=("pnl", "size"), past_roi=("roi", "median"), past_t=("t", "median"),
        future_pnl=("pnl_future", "sum"), future_capital=("capital_future", "sum"),
        future_winners=("pnl_future", lambda s: (s > 0).mean()))
    out["future_roi"] = out["future_pnl"] / out["future_capital"]
    return out


@lru_cache(maxsize=4)
def skill_asof() -> pd.DataFrame:
    """Running record per wallet, ordered by when each market's result became
    known. Join a fill at time t to the last row with resolved_ts < t to get
    what was knowable about that wallet at t."""
    wm = wallet_market().sort_values(["wallet", "resolved_ts"], kind="stable")
    g = wm.groupby("wallet", observed=True, sort=False)
    wm["c_pnl"] = g["pnl"].cumsum()
    wm["c_cap"] = g["capital"].cumsum()
    wm["c_n"] = g.cumcount() + 1
    wm["c_sq"] = (wm["pnl"] ** 2).groupby(wm["wallet"], observed=True, sort=False).cumsum()
    mean = wm["c_pnl"] / wm["c_n"]
    var = (wm["c_sq"] / wm["c_n"] - mean ** 2).clip(lower=0) * wm["c_n"] / (wm["c_n"] - 1).clip(lower=1)
    wm["c_t"] = (mean / np.sqrt(var / wm["c_n"])).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    wm["c_roi"] = wm["c_pnl"] / wm["c_cap"]
    return wm[["wallet", "resolved_ts", "c_pnl", "c_cap", "c_n", "c_t", "c_roi"]].sort_values("resolved_ts", kind="stable")


def signals(min_cash: float = 2_000, lo: float = 0.05, hi: float = 0.95) -> pd.DataFrame:
    """Every sizeable BUY, tagged with the buyer's record as it stood at that moment."""
    f = fills()
    s = f[f["buy"] & (f["capital"] >= min_cash) & (f["price"] >= lo) & (f["price"] <= hi)]
    s = s.sort_values("ts", kind="stable").assign(_t=lambda x: x["ts"].astype("float64"))
    sk = skill_asof().rename(columns={"resolved_ts": "known_ts"})
    sk["wallet"] = sk["wallet"].astype(s["wallet"].dtype)
    out = pd.merge_asof(s, sk, left_on="_t", right_on="known_ts", by="wallet", allow_exact_matches=False)
    for c in ("c_pnl", "c_cap", "c_n", "c_t", "c_roi"):
        out[c] = out[c].fillna(0.0)
    return out.drop(columns=["known_ts", "_t"])


@lru_cache(maxsize=1)
def _market_tape() -> dict:
    """condition_id -> (ts array, p0 array) from every fill, for pricing a delayed entry."""
    f = fills()[["condition_id", "ts", "p0"]].sort_values(["condition_id", "ts"], kind="stable")
    out = {}
    for cid, g in f.groupby("condition_id", observed=True, sort=False):
        out[cid] = (g["ts"].to_numpy(), g["p0"].to_numpy())
    return out


def copy_trades(sig: pd.DataFrame, delay_s: int = 600, half_spread: float = 0.01, fees: bool = True,
                stake: float = 100.0, max_slip: float = 0.03, max_wait_s: int = 6 * 3600,
                one_per: str = "condition_id") -> pd.DataFrame:
    """Follow each signal: buy the same outcome `delay_s` later at the next
    traded price (plus spread and fee), hold to resolution. Returns trades in
    the shape research.engine.summarise expects."""
    tape = _market_tape()
    m = store.markets().set_index("condition_id")
    rows = []
    cids = sig["condition_id"].astype(str).to_numpy()
    ts, oi, seen_px, res = sig["ts"].to_numpy(), sig["outcome_index"].to_numpy(), sig["price"].to_numpy(), sig["resolved_ts"].to_numpy()
    entry = np.full(len(sig), np.nan)
    entry_ts = np.full(len(sig), np.nan)
    for i in range(len(sig)):
        tt, pp = tape[cids[i]]
        j = np.searchsorted(tt, ts[i] + delay_s, side="left")
        if j >= len(tt) or tt[j] > ts[i] + delay_s + max_wait_s or tt[j] >= res[i]:
            continue
        entry[i] = pp[j] if oi[i] == 0 else 1.0 - pp[j]
        entry_ts[i] = tt[j]
    ok = np.isfinite(entry) & (entry <= seen_px + max_slip) & (entry >= 0.03) & (entry <= 0.97)
    s = sig[ok]
    px = entry[ok] + half_spread
    cat = pd.Series(cids[ok]).map(m["category"]).to_numpy()
    rate = pd.Series(cat).map(panel.FEE_RATE).fillna(0.05).to_numpy() if fees else 0.0
    cost = px + rate * px * (1 - px)
    payout0 = pd.Series(cids[ok]).map(m["payout0"]).to_numpy()
    payout = np.where(oi[ok] == 0, payout0, 1 - payout0)
    t = pd.DataFrame({
        "condition_id": cids[ok], "event_id": pd.Series(cids[ok]).map(m["event_id"]).to_numpy(), "category": cat,
        "side": oi[ok], "wallet": s["wallet"].astype(str).to_numpy(), "entry_ts": entry_ts[ok],
        "exit_ts": res[ok], "hold_days": (res[ok] - entry_ts[ok]) / 86400.0, "entry": px, "cost": cost,
        "payout": payout, "ret": (payout - cost) / cost,
    })
    if one_per:
        t = t.sort_values("entry_ts", kind="stable").drop_duplicates(one_per, keep="first")
    t["stake"] = stake
    t["pnl"] = stake * t["ret"]
    return t.reset_index(drop=True)


def leaderboard(as_of: float = None, min_markets: int = 20, min_capital: float = 20_000, top: int = 200) -> pd.DataFrame:
    """Current best traders by consistency of dollar profit (t-stat), with portfolio return."""
    wm = wallet_market()
    if as_of:
        wm = wm[wm["resolved_ts"] < as_of]
    r = record(wm)
    r = r[(r["n_markets"] >= min_markets) & (r["capital"] >= min_capital)]
    return r.sort_values("t", ascending=False).head(top)
