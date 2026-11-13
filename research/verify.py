"""Second opinion for any rule that passes the scan: re-price its trades at
what buyers ACTUALLY paid.

The price series is a per-bucket summary whose exact meaning is undocumented.
In a thin market it can sit at a level nobody could trade at (a midpoint
between a far-apart bid and ask). So a surviving rule is re-run here using the
market's real trade tape: the entry price becomes the volume-weighted price
that takers paid to BUY the same outcome in the hours after the signal. If
nobody bought in that window, the trade is dropped as unfillable.

    python -m research.verify
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from . import engine, panel, scan, store
from .api import ApiError, Client

TAPE_DIR = store.DATA_DIR / "verify_tapes"


def _tape(client: Client, cid: str) -> pd.DataFrame:
    path = TAPE_DIR / f"{cid}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    try:
        rows, _ = client.trades(cid, both_sides=False, max_pages=60)   # taker side only: the price actually paid
    except ApiError:
        rows = []
    df = pd.DataFrame({"side": [r["side"] for r in rows], "outcome_index": [int(r.get("outcome_index", 999)) for r in rows],
                       "price": [float(r["price"]) for r in rows], "size": [float(r["size"]) for r in rows],
                       "ts": [int(r["timestamp"]) for r in rows]})
    TAPE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df


def reprice(trades: pd.DataFrame, window_s: int = 6 * 3600, fees: bool = True, stake: float = 100.0) -> pd.DataFrame:
    """Replace each trade's entry with the real average price takers paid for that outcome
    in the `window_s` after the signal. Unfillable trades are dropped."""
    client = Client()
    cids = trades["condition_id"].unique().tolist()
    with ThreadPoolExecutor(max_workers=6) as pool:
        tapes = dict(zip(cids, pool.map(lambda c: _tape(client, c), cids)))
    rows = []
    for t in trades.itertuples():
        tape = tapes[t.condition_id]
        if tape.empty:
            continue
        w = tape[(tape["ts"] >= t.entry_ts) & (tape["ts"] < min(t.entry_ts + window_s, t.exit_ts))]
        # A taker BUY of our outcome pays the ask. A taker SELL of the other outcome is the same trade seen from the other book.
        buys = w[(w["side"] == "BUY") & (w["outcome_index"] == t.side)]
        if buys.empty or buys["size"].sum() <= 0:
            continue
        px = float(np.average(buys["price"], weights=buys["size"]))
        if not (0.03 <= px <= 0.97):
            continue
        rate = panel.FEE_RATE.get(t.category, 0.05) if fees else 0.0
        cost = px + rate * px * (1 - px)
        rows.append(dict(condition_id=t.condition_id, event_id=t.event_id, category=t.category, side=t.side,
                         entry_ts=t.entry_ts, exit_ts=t.exit_ts, hold_days=t.hold_days, series_entry=t.entry,
                         entry=px, cost=cost, payout=t.payout, ret=(t.payout - cost) / cost,
                         available_usd=float((buys["price"] * buys["size"]).sum())))
    out = pd.DataFrame(rows)
    if len(out):
        out["stake"] = stake
        out["pnl"] = stake * out["ret"]
    return out


def survivors() -> list:
    """Re-test every rule the scan marked as having held on unseen data."""
    rules = pd.DataFrame(json.loads((store.RESULTS_DIR / "scan.json").read_text()))
    rules = rules[rules["survived"]]
    full = panel.build()
    inv = {v: k for k, v in scan.NAMES.items()}
    out = []
    for r in rules.itertuples():
        plo, phi = (float(x) for x in r.price.split("-"))
        feature, bucket = None, None
        if r.condition != "any":
            for name, key in inv.items():
                if r.condition.startswith(name):
                    feature = key
                    rest = r.condition[len(name):].strip()
                    for b in scan.FEATURE_BUCKETS[key]:
                        if scan._label(key, b) == r.condition:
                            bucket = b
        row = {"price": r.price, "condition": r.condition}
        for sp in ("train", "validation"):
            s = scan._two_sided(full[(full["split"] == sp) & full["tradable"]])
            mask = ((s["px_side"] >= plo) & (s["px_side"] < phi)).to_numpy() & scan._mask(s, feature, bucket)
            sig = pd.Series(np.where(mask, s["side"].to_numpy(), -1), index=s.index)
            series_trades = engine.run(sig, s)
            real = reprice(series_trades)
            row[sp] = {"series": engine.summarise(series_trades), "real": engine.summarise(real) if len(real) else {"n_trades": 0},
                       "fillable_share": len(real) / max(len(series_trades), 1),
                       "avg_price_gap": float((real["entry"] - real["series_entry"]).mean()) if len(real) else None,
                       "median_available_usd": float(real["available_usd"].median()) if len(real) else None}
        row["confirmed"] = bool(row["train"]["real"].get("significant") and row["validation"]["real"].get("significant"))
        out.append(row)
    return out


def main():
    out = survivors()
    (store.RESULTS_DIR / "verify.json").write_text(json.dumps(out, default=float))
    for r in out:
        print(f"\n{r['price']} | {r['condition']}  -> {'CONFIRMED' if r['confirmed'] else 'NOT confirmed'} at real prices")
        for sp in ("train", "validation"):
            a, b = r[sp]["series"], r[sp]["real"]
            print(f"  {sp:10s} series price: {a['roi_pct']:+.1f}% ({a['n_trades']} trades) | real prices: "
                  + (f"{b['roi_pct']:+.1f}% [{b['roi_lo']:+.1f},{b['roi_hi']:+.1f}] ({b['n_trades']} trades, "
                     f"{r[sp]['fillable_share']:.0%} fillable, paid {r[sp]['avg_price_gap']:+.3f} vs series, "
                     f"median ${r[sp]['median_available_usd']:,.0f} available)" if b.get("n_trades") else "no fillable trades"))


if __name__ == "__main__":
    main()
