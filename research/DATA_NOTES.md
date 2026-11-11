# Data notes

Facts about Polymarket's public APIs, checked live on 2026-11-11. These answer step 1 of [PLAN.md](../PLAN.md).

## Data API v1 is retired

`https://data-api.polymarket.com/trades` (and every other v1 path) returns HTTP 410. Everything under `backend/app/` that fetched trades, holders or wallet history no longer works. The replacement is `https://data-api.polymarket.com/v2/`, spec at `/v2/openapi.json`. No authentication.

## Answers to the six questions

| Question | Answer |
|---|---|
| Does `/trades` return taker fills only? | Yes by default. `taker_only=false` adds the maker rows, so each fill appears once per side. We always request both sides. |
| How do we get a full tape past the old 10,000 cap? | v2 pages with a cursor and has no depth cap. 1,000 fills per page, about 0.35 s per page. `filter_type=CASH&filter_amount=N` keeps only fills of at least $N. |
| What does the old `/closed-positions` timestamp mean? Are losers included? | No longer needed. Wallet records are built from the trade tape plus resolution times, so they are point-in-time and complete by construction. v2 `/positions?status=CLOSED` and `status=REDEEMABLE_LOST` exist if ever required. |
| When did the outcome become known? | `/v2/resolutions` gives the oracle's last lifecycle time per condition. It is never later than Gamma's `closedTime`; we use the earlier of the two. The real-world event is often earlier still, so strategies never enter above 97c. |
| What is the payout? | The last point of `/v2/prices-history?interval=max` for a resolved market is the on-chain payout (0, 1, or a fraction for void/split resolutions) at `resolution_seconds: 0`. It matched Gamma's `outcomePrices` on all 12,002 markets checked. 18 of those resolved 50/50. |
| What fees apply? | Taker only: `shares x rate x p x (1 - p)`. Rates: Crypto 0.07; Sports, Economics, Culture, Weather, Other 0.05; Finance, Politics, Tech, Mentions 0.04; Geopolitics 0. Makers pay nothing. Source: docs.polymarket.com/trading/fees. |

## Price history

- `interval=max` serves the whole life. 3-hour and 12-hour buckets are kept forever; markets from before roughly mid-2025 only have 12-hour data even when 3-hour is requested.
- Finer grains expire: 1-minute after about 7 days, 5-minute after about 60, 30-minute after about 90. Anything needing minute data must be recorded going forward.
- A point's `timestamp` is its bucket start. The panel treats it as known only at the bucket's end and fills one bucket later.
- What the bucket price is (mid, last trade or average) is not documented. The cost model adds a half-spread on top to stay conservative.

## Market listing (Gamma)

- `/markets` caps `limit` at 100 and rejects deep offsets. `/markets/keyset` walks everything with `after_cursor`.
- `include_tag=true` returns tags, which give the category.
- 488,392 closed markets since 2024-07-01 have at least $10k volume: about 209k crypto, 202k sports, 30k weather, 25k politics.

## Selection bias, measured

Restricting to markets with over $1M final volume makes cheap outcomes look underpriced by about 7 points (an outcome priced 14.5c won 21.6% of the time). That is hindsight: a long shot that wins attracts heavy trading and so passes the volume filter. Price analyses therefore use a random 12% sample of all markets above $10k. Trader analyses use the over-$1M tapes and are always compared with a same-universe baseline.

## What is on disk

| File | Contents |
|---|---|
| `data/markets.parquet` | Every closed market since 2024-07 with at least $10k volume |
| `data/prices.parquet` | Outcome-0 price series for the random sample plus all markets over $1M |
| `data/trades/*.parquet` | Both sides of every fill of $500 or more in markets over $1M |
| `data/runs.jsonl` | One line per backtest: git hash, parameters, hypothesis, results |
| `data/results/*.json` | What the interface shows |
