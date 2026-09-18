# Polymarket Smart Money Dashboard — Phase 1

Read-only research dashboard for Polymarket. Pick any active market and it
shows, for that market's current holders: a per-holder **Smart Score** (0–100),
each side's $-weighted score, net smart-money $ flow, an undervalued gap, a
headline **Overall Market Verdict**, a signed $-weighted histogram of holder
scores, and whale-concentration pie charts per side. **Not a trading bot, not
financial advice** (see the footer disclaimer in the app).

## How it works

The flow is **on-demand**, not a big nightly batch:

1. **Seed once** (`python -m app.backfill`) — scores the ~30 `/v1/leaderboard`
   wallets so the trader cache isn't empty, and pulls metadata for the top ~24
   active markets by 24h volume so the list page is browsable (~40s).
   `--analyze-markets N` also fully analyses the top N markets up front
   (~30–40s each).
2. **Open the app**, pick a market from the list (or search any live market).
3. The backend fetches that market's **current holders live** (`/holders`,
   top ~40/side), returns immediately with whatever Smart Scores are already
   cached (`analyzing: true` + a pending count), and scores the rest in a
   background thread from their `/closed-positions` history. A cold market's
   ~80 holders finish in ~30–40s; the page polls and sharpens itself.
4. Re-opening a market is instant (<50ms, served from cache). The trader pool
   warms up as you browse, so later markets are faster too.

### Smart Score (0–100) — per wallet

Computed from that wallet's own resolved-trade history only, never their current
open position. Each sub-metric is mapped to 0–1 by a **fixed curve** (no
population percentile — a deliberate simplification of the build spec's §3.2 so
each wallet is scored in isolation and the score is stable):

| Metric | From `/closed-positions` | Curve |
|---|---|---|
| mean return | `Σ realizedPnl / Σ totalBought` | logistic, +0.25 → ~73 |
| $-weighted win rate | $ share of positions with `r > 0` | linear 0.40–0.80 → 0–1 |
| consistency (Sharpe-like) | `mean(r) / stdev(r)` | logistic, +1.0 → ~73 |
| calibration (Brier) | `mean((avgPrice − outcome)²)` on resolved bets | linear 0.35–0.10 → 0–1 |

Weighted `0.35·sharpe + 0.30·return + 0.20·winrate + 0.15·calibration` (renormalized
over whatever metrics the wallet has), then **shrunk toward a neutral 50 by
sample size** — a 10-bet record can't read as a 95. All constants live in
`app/config.py` (`SmartScoreParams`).

Confidence tiers (Unrated / Bronze / Silver / Gold, §3.4) still gate the UI, and
Bronze wallets are down-weighted `min(1, n/30)` in the score-weighted market
aggregates. Trend (§3.3, Improving / Declining / Stable) compares a wallet's
recent-window score to its lifetime score.

### Market aggregates (§3.5–3.8)

`usd_weighted_score` per side, `net_smart_flow`, `smart_implied_probability`
(skill-weighted $ share on YES), `undervalued_gap` = implied − price, the signed
distribution + normal overlay (§3.6, split into YES/NO stacked bars with a
plain-English caption), whale concentration + Gini per side (§3.7), $-weighted
**wallet tenure** per side (see below), and the headline verdict — Strong Buy /
Lean / Fade / Mixed / Insufficient data — with a 0–100 conviction number (§3.8).

### Wallet tenure

"How long has this wallet been active on Polymarket" — true on-chain wallet
age was attempted via a free public Polygon RPC and turned out **not
viable**: free RPCs aren't archive nodes, so `eth_getCode` only works at the
latest block. Instead, `tenure_days` = time since a wallet's **earliest
observed trade** (the minimum timestamp across their fetched
`/closed-positions`) — free (already-fetched data, no extra API calls), and
arguably more relevant anyway: a cryptographically old address that's never
traded until today is just as much a "fresh account" signal as a literally
new one. It's a lower bound, not a hard fact — a very active wallet's true
first trade can predate the capped, newest-first page we fetch (see
`max_closed_positions_per_wallet` below).

### Insiders tab

A first-pass, **unvalidated heuristic** (`app/insiders.py`) flags positions
that look like informed/early money: large relative to the wallet's own
trading history, from a wallet with little tenure, and/or with ~no resolved
track record at all. It explicitly excludes the "hold [side] on every outcome
of one multi-outcome event" pattern (a market-maker/arbitrage signature, not
informed betting — seen directly during development as a $190M+ symmetric
NO position across every 2024 presidential-candidate market). Every
threshold lives in `config.InsiderConfig` and is meant to be tuned as real
markets are observed, not treated as ground truth — this is a lead to
investigate, never a determination of wrongdoing.

Flags run inline (fast, no network calls) on every market open, and are
picked up by a **background poller** that re-analyzes tracked markets every
`POLLER_INTERVAL_SECONDS` (default 300s) so alerts can fire even when nobody
has the dashboard open. New flags crossing `alert_threshold` (default 70)
trigger an email via `app/alerts.py` — see `.env.example` to configure SMTP;
with no SMTP configured, alerts are just logged to the console so the
detector is testable before you wire up a mail relay. The poller only runs
while the backend process is up — see Notes/limits below.

`GET /api/insiders` lists flags; `POST /api/insiders/scan-now` kicks a scan
immediately instead of waiting for the next cycle.

## Stack (§8)

Python + FastAPI + SQLite (one file, `backend/data/dashboard.db`), React (Vite) +
Recharts + plain `fetch`. No Docker, no scheduler, no Postgres, no websockets.

```
backend/app/
  config.py           # every threshold / weight / curve constant
  scoring.py          # pure functions: §3.1 stats, §3.2 Smart Score, §3.3 trend, §3.5-3.8 aggregates, tenure
  pipeline.py         # fetch closed-positions -> score -> upsert `traders` (concurrent + retry pass)
  market_analysis.py  # on-demand: refresh a market's holders, score the new ones, scan for insiders, return detail
  market_list.py      # curated top-markets metadata refresh (background, cached)
  backfill.py         # `python -m app.backfill` seed script + the shared data-source classes
  polymarket_client.py# rate-limited Gamma / CLOB / Data API clients
  market_service.py   # DB rows -> scoring.Holder -> market payloads
  search_service.py   # §6 live search
  insiders.py          # heuristic insider-pattern detector + persistence
  alerts.py             # email alerting (logs instead, if SMTP isn't configured)
  poller.py             # background loop: re-analyze tracked markets so alerts fire even when idle
  db.py               # SQLite schema + WAL connection helper + tiny migrations
  main.py, routers/   # FastAPI app (routers/insiders.py: GET /api/insiders, POST /scan-now)
  tests/              # 48 unit + integration tests
frontend/src/
  pages/InsidersPage.jsx  # the Insiders tab
  useChartWidth.js         # shared Recharts-3 sizing workaround (see Notes/limits)
```

Adding a new signal later is: a pure function in `scoring.py`, a column via
`db.MIGRATIONS`, and a field in the `market_service` payload — the pipeline
and API don't change. Queued ideas (not built): a combined per-market "edge
score" blending verdict + insider flags + concentration + trend; cross-market
wallet clustering (the same wallet flagged across several related markets at
once); surfacing the already-implemented fade-pool (§3.3) as its own view;
coordinated new-wallet cluster detection.

## Running it

**Backend**
```
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # optional -- fill in SMTP settings for real insider-alert emails
python -m app.backfill --analyze-markets 5     # seed leaderboard + pre-analyze the top 5 markets
uvicorn app.main:app --reload --port 8000
```

**Frontend** (separate terminal)
```
cd frontend
npm install
npm run dev            # http://localhost:5173 — expects the API on :8000
```

**Tests**
```
cd backend && source venv/bin/activate && python -m pytest app/tests/ -q
```

### Seed script options

| | |
|---|---|
| `python -m app.backfill` | score the ~30 leaderboard wallets + refresh market-list metadata |
| `python -m app.backfill --analyze-markets N` | also fully analyze the top N markets now (so the list page opens with verdicts) |
| `python -m app.backfill --offline` | use `app/fixtures/*.json` (captured real API responses) instead of live APIs |
| `python -m app.backfill --force` | ignore the 24h wallet-freshness cache |

## Notes / limits

- **CLOB `/data/trades` needs an API key** — so full trade-reconstruction of
  holder lists (spec §5) is Phase 2. Phase 1 uses the `/holders` fast path
  (top ~40/side), which is enough for the whales that move the aggregates.
- The API calls Polymarket live on market open (the on-demand fetch) — a
  deliberate change from spec §10's "API only reads from SQLite". Everything is
  still cached; re-opens don't re-fetch.
- Rate limiting: the unauthenticated Data API tolerates only a few req/s in
  practice (far below its documented ceiling). Fetches run 3–4 concurrent with a
  single-threaded retry pass that recovers the handful that still 429.
- `/closed-positions` is paged newest-first (`sortBy=TIMESTAMP`) and capped at
  300 per wallet — the score is stable well before that and a whale's last 300
  bets read current skill better than 5,000 all-time.
- Filter panel (§4), dumb-money/fade view (§3.3 is implemented + tested in
  `scoring.py`, not yet its own UI), and the undervalued-market leaderboard are
  Phase 2/3.
- **The background poller (insider alerts) only runs while the backend
  process is up** — closing the terminal/stopping `uvicorn` stops it. A real
  24/7 deployment would run `app/poller.py`'s loop as its own process/cron
  job instead of a thread inside the API server.
- Recharts 3 renders charts at 0×0 inside a `ResponsiveContainer` in some of
  this app's layouts (a known-flaky interaction with React 19 + CSS grid).
  Worked around with `frontend/src/useChartWidth.js` (measure the wrapper with
  a `ResizeObserver`, pass explicit pixel dimensions) instead of
  `ResponsiveContainer` — a real workaround, not a cosmetic one, and worth
  keeping in mind if a future chart goes blank.
- Backtesting and any real-money trade execution are explicitly **not
  built** — see the project memory / dev discussion for the queued
  architecture (point-in-time replay, no lookahead, paper vs. live
  execution tradeoffs) before starting either.
