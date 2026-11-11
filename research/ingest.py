"""Download research data once. Every stage is resumable and skips what it has.

    python -m research.ingest markets  [--since 2024-07-01] [--min-volume 1000000]
    python -m research.ingest prices   [--sample-frac 0.2]   (all >= $1M, random share of the rest)
    python -m research.ingest trades   [--min-cash 500] [--limit 2000] [--max-pages 300]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from . import store
from .api import ApiError, Client

# First matching tag wins, so order is priority.
CATEGORY_TAGS = [
    ("weather", "Weather"), ("sports", "Sports"), ("esports", "Sports"), ("crypto", "Crypto"),
    ("politics", "Politics"), ("elections", "Politics"), ("geopolitics", "World"), ("world", "World"),
    ("economy", "Economy"), ("finance", "Economy"), ("business", "Economy"), ("tech", "Tech"),
    ("ai", "Tech"), ("science", "Science"), ("pop-culture", "Culture"), ("culture", "Culture"),
    ("entertainment", "Culture"),
]


def _ts(s) -> float:
    if not s:
        return float("nan")
    s = str(s).strip().replace("Z", "+00:00").replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # Gamma mixes fractional-second widths that Python 3.9 rejects.
        try:
            head, _, tail = s.partition(".")
            tz = "+" + tail.split("+")[1] if "+" in tail else "+00:00"
            return datetime.fromisoformat(head + tz).timestamp()
        except (ValueError, IndexError):
            return float("nan")


def _category(tags: list) -> str:
    s = set(tags)
    for slug, label in CATEGORY_TAGS:
        if slug in s:
            return label
    return "Other"


def _market_row(m: dict):
    try:
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
        tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
        final = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
    except (TypeError, ValueError):
        return None
    if not outcomes or not tokens or len(outcomes) != 2 or len(tokens) != 2 or not m.get("conditionId"):
        return None
    try:
        payout0 = float(final[0])
    except (TypeError, ValueError, IndexError):
        payout0 = float("nan")
    ev = (m.get("events") or [{}])[0]
    tags = [t.get("slug") for t in (m.get("tags") or []) if t.get("slug")]
    return dict(
        condition_id=m["conditionId"], market_id=str(m.get("id")), question=m.get("question"), slug=m.get("slug"),
        event_id=str(ev.get("id") or m["conditionId"]), event_slug=ev.get("slug"), series_slug=ev.get("seriesSlug"),
        tags="|".join(tags), category=_category(tags),
        outcome0=str(outcomes[0]), outcome1=str(outcomes[1]), token0=str(tokens[0]), token1=str(tokens[1]),
        neg_risk=bool(m.get("negRisk")), fees_enabled=bool(m.get("feesEnabled")), fee_type=m.get("feeType"),
        taker_fee_bps=float(m.get("takerBaseFee") or 0.0), volume=float(m.get("volumeNum") or 0.0),
        start_ts=_ts(m.get("startDate")), end_ts=_ts(m.get("endDate")), closed_ts=_ts(m.get("closedTime")),
        created_ts=_ts(m.get("createdAt")), payout0_gamma=payout0,
    )


def ingest_markets(since: str, min_volume: float):
    c = Client()
    rows, seen = [], set()
    for m in c.closed_markets(since, min_volume):
        row = _market_row(m)
        if row and row["condition_id"] not in seen:
            seen.add(row["condition_id"])
            rows.append(row)
            if len(rows) % 1000 == 0:
                print(f"  {len(rows)} markets...", flush=True)
    df = pd.DataFrame(rows)
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Resolution time + payout get filled in by the prices stage.
    df["payout0"] = float("nan")
    df["resolved_ts"] = float("nan")
    df["price_tried"] = False
    if store.MARKETS.exists():
        old = pd.read_parquet(store.MARKETS)
        if "price_tried" not in old:
            old["price_tried"] = old["resolved_ts"].notna()
        old = old[["condition_id", "payout0", "resolved_ts", "price_tried"]]
        df = df.drop(columns=["payout0", "resolved_ts", "price_tried"]).merge(old, on="condition_id", how="left")
        df["price_tried"] = df["price_tried"].fillna(False).astype(bool)
    df.to_parquet(store.MARKETS, index=False)
    print(f"markets: {len(df)} written -> {store.MARKETS}")


def ingest_prices(workers: int = 8, sample_frac: float = 1.0, full_above: float = 1_000_000):
    """Fetch prices for every market at or above `full_above` volume and a random
    `sample_frac` of the rest. Sampling is random, so it adds no selection bias;
    store.markets() carries the weight that undoes it."""
    c = Client()
    mk = pd.read_parquet(store.MARKETS)
    have = pd.read_parquet(store.PRICES) if store.PRICES.exists() else pd.DataFrame(columns=["condition_id", "ts", "p0"])
    done = set(have["condition_id"].unique()) | set(mk.loc[mk["price_tried"], "condition_id"])
    want = (mk["volume"] >= full_above) | store.in_sample(mk["condition_id"], sample_frac)
    todo = mk[want & ~mk["condition_id"].isin(done)]
    print(f"prices: {len(todo)} markets to fetch ({len(done)} cached)")

    def one(cid, token):
        pts = c.price_history(token, bucket_seconds=10800)   # 3h: finest grain kept forever
        return cid, pts

    parts, settle, t0, n = [have], {}, time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, r.condition_id, r.token0) for r in todo.itertuples()]
        for f in as_completed(futs):
            n += 1
            try:
                cid, pts = f.result()
            except ApiError as e:
                print("  price fetch failed:", str(e)[:150])
                continue
            if not pts:
                continue
            last = pts[-1]
            if last.get("resolution_seconds") == 0:
                settle[cid] = float(last["price"])
            # Zero-width points are the settlement payout and the final live tick: answers, not tradable prices.
            pts = [p for p in pts if p.get("resolution_seconds")]
            if pts:
                parts.append(pd.DataFrame({"condition_id": cid, "ts": [p["timestamp"] for p in pts],
                                           "p0": [float(p["price"]) for p in pts]}))
            if n % 500 == 0:
                print(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    pd.concat([x for x in parts if len(x)], ignore_index=True).to_parquet(store.PRICES, index=False)
    mk["price_tried"] = mk["price_tried"] | mk["condition_id"].isin(set(todo["condition_id"]))

    # Payout: on-chain settlement point when present, else Gamma's final price.
    if settle:
        s = pd.Series(settle)
        idx = mk["condition_id"].map(s)
        mk["payout0"] = idx.where(idx.notna(), mk["payout0"])
    mk["payout0"] = mk["payout0"].where(mk["payout0"].notna(), mk["payout0_gamma"])

    # When the outcome became known: the oracle's last lifecycle update, else Gamma's close time.
    need = mk[mk["resolved_ts"].isna() & mk["price_tried"]]["condition_id"].tolist()
    print(f"resolutions: {len(need)} markets")
    res = {}
    for i in range(0, len(need), 400):
        try:
            for r in c.resolutions(need[i:i + 400]):
                cid = r.get("condition_id")
                t = r.get("resolved_at") or r.get("last_update_timestamp")
                if not cid or not t or r.get("status") != "resolved":
                    continue
                ts = float(t) if str(t).isdigit() else _ts(t)
                if ts == ts:
                    res[cid] = min(ts, res.get(cid, ts))
        except ApiError as e:
            print("  resolutions failed:", str(e)[:150])
    r = mk["condition_id"].map(pd.Series(res, dtype=float))
    mk["resolved_ts"] = mk["resolved_ts"].where(mk["resolved_ts"].notna(), r)
    mk["resolved_ts"] = mk["resolved_ts"].where(mk["resolved_ts"].notna(), mk["closed_ts"])
    # The oracle time can never be later than Gamma's close; take the earlier.
    mk["resolved_ts"] = mk[["resolved_ts", "closed_ts"]].min(axis=1)
    mk.to_parquet(store.MARKETS, index=False)
    print(f"prices: done. settlement points for {len(settle)} markets, oracle times for {len(res)}")


def ingest_trades(min_cash: float, limit: int, max_pages: int, workers: int = 4, shard_every: int = 40):
    c = Client()
    mk = pd.read_parquet(store.MARKETS).sort_values("volume", ascending=False)
    done = store.trades_done()
    todo = [cid for cid in mk["condition_id"] if cid not in done][:limit]
    store.TRADES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"trades: {len(todo)} markets to fetch ({len(done)} done), floor ${min_cash:.0f}")

    def one(cid):
        rows, truncated = c.trades(cid, min_cash=min_cash, max_pages=max_pages)
        df = pd.DataFrame({
            "condition_id": cid,
            "wallet": [r["proxy_wallet"] for r in rows],
            "side": [r["side"] for r in rows],
            "outcome_index": [int(r.get("outcome_index", 999)) for r in rows],
            "price": [float(r["price"]) for r in rows],
            "size": [float(r["size"]) for r in rows],
            "ts": [int(r["timestamp"]) for r in rows],
            "tx": [r.get("transaction_hash") for r in rows],
        })
        return cid, df, truncated

    buf, buf_meta, n, t0, total = [], {}, 0, time.time(), 0

    def flush():
        nonlocal buf, buf_meta
        if not buf_meta:
            return
        if buf:
            pd.concat(buf, ignore_index=True).to_parquet(
                store.TRADES_DIR / f"shard_{int(time.time() * 1000)}.parquet", index=False, compression="zstd")
        done.update(buf_meta)
        store.TRADES_DONE.write_text(json.dumps(done))
        buf, buf_meta = [], {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, cid) for cid in todo]
        for f in as_completed(futs):
            n += 1
            try:
                cid, df, truncated = f.result()
            except ApiError as e:
                print("  trades failed:", str(e)[:150])
                continue
            if len(df):
                buf.append(df)
            total += len(df)
            buf_meta[cid] = {"floor": min_cash, "truncated": truncated, "n": len(df)}
            if n % shard_every == 0:
                flush()
                print(f"  {n}/{len(todo)} markets, {total:,} fills ({time.time() - t0:.0f}s)", flush=True)
    flush()
    print(f"trades: done, {total:,} fills from {n} markets")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["markets", "prices", "trades"])
    ap.add_argument("--since", default="2024-07-01")
    ap.add_argument("--min-volume", type=float, default=1_000_000)
    ap.add_argument("--min-cash", type=float, default=500)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--max-pages", type=int, default=300)
    ap.add_argument("--sample-frac", type=float, default=1.0)
    a = ap.parse_args()
    if a.stage == "markets":
        ingest_markets(a.since, a.min_volume)
    elif a.stage == "prices":
        ingest_prices(sample_frac=a.sample_frac)
    else:
        ingest_trades(a.min_cash, a.limit, a.max_pages)


if __name__ == "__main__":
    main()
