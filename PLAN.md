# PLAN: from dashboard to a research setup we can trust

Audit date: 2026-11-11. Nothing in the code was changed. Numbers below come from reading every source file and from read-only queries against `backend/data/dashboard.db` (490 resolved markets, 3.9M trades, 3.3M wallet positions, 45,044 wallets, 134 completed backtest runs).

## 0. The short version

1. **The two existing strategies show no edge.** Across all 1,771 simulated trades ever run (227 distinct markets), pooled P&L is **−$2,508**, before any fees or spread. In every price bucket the win rate equals the entry price, which is what "no better than the market" looks like:

   | Entry price | Trades | Win rate | Avg entry | ROI |
   |---|---|---|---|---|
   | < 0.10 | 141 | 0.0% | 0.016 | −100% |
   | 0.10–0.30 | 60 | 31.7% | 0.184 | +87.7% |
   | 0.30–0.70 | 427 | 57.4% | 0.523 | +11.5% |
   | 0.70–0.90 | 498 | 84.5% | 0.813 | +2.7% |
   | ≥ 0.90 | 645 | 95.5% | 0.955 | +0.1% |

   The two positive middle buckets are 487 trades on a backtest that leaks future information (section 1.2), so they are not evidence.
2. **Both weight searches overfit.** Smart money: +46.5% on train, −19.2% on test. Insider: +14.3% on train, −11.1% on test.
3. **The backtest is biased in the optimistic direction and still loses.** It leaks outcomes into wallet scores, uses future prices, charges no costs, and rebuilds holders from a trade tape that is missing most of each big market's life.
4. **The underlying ideas are not disproved.** The data layer is too broken to say whether copying good wallets or spotting insiders works. That is the thing to fix first.
5. **About half the code serves features with no proven value** (live dashboard, email alerts, trading UI, wallet storage). Freeze it now, delete it once the research setup replaces it.

## 1. Audit

### 1.1 Keep, delete, merge

**Backend (`backend/app/`)**

| File | Verdict | Reason |
|---|---|---|
| [polymarket_client.py](backend/app/polymarket_client.py) | Keep | Rate limiting and the API quirks in its comments were learned the hard way. Remove unused `get_open_positions`, `get_prices`, `get_midpoints`, `ClobClient.get_trades`. |
| [backtest_data.py](backend/app/backtest_data.py) | Rewrite into the new data layer | Sync logic is reusable. Delete `reconstruct_holders_at`, `price_at_or_before`, `wallet_positions_asof` (only tests and a dead slow path use them). |
| [backtest_engine.py](backend/app/backtest_engine.py) | Delete once replaced | Mixes run management, threading, network fetching, random search and trade simulation in one file. `_holders_at` (lines 734–778) is dead outside tests. |
| [scoring.py](backend/app/scoring.py) | Keep ~150 lines, delete the rest | Keep `ClosedPosition`, `trader_raw_stats`, `smart_score_from_raw` as one feature among several. Delete dead code: `gap_rank_score`, `fade_pool_eligible`, `smart_pool_eligible`, `composite_and_percentile` (its name is false: there is no percentile). Whale buckets, Gini and signed distribution are display-only and go with the dashboard. |
| [insiders.py](backend/app/insiders.py) | Keep the six factor functions as features | Delete persistence, alerting and listing (lines 291–552) until the signal is proven. |
| [signals.py](backend/app/signals.py) | Merge into new strategy modules | Parses verdict label strings with regexes to get a side. Signals should be numbers, not labels. |
| [config.py](backend/app/config.py) | Split | 442 lines, mostly commentary. About 50 hand-set constants. `cfg.FILTERS` and `cfg.WEIGHTS` are unused. Strategy parameters should live beside each strategy. |
| [db.py](backend/app/db.py) | Split | One 4.6 GB SQLite file holds research cache, app state and trading tables. Research data moves to Parquet/DuckDB. |
| [pipeline.py](backend/app/pipeline.py), [backfill.py](backend/app/backfill.py), [market_analysis.py](backend/app/market_analysis.py), [market_service.py](backend/app/market_service.py), [market_list.py](backend/app/market_list.py), [search_service.py](backend/app/search_service.py) | Freeze, delete at step 11 | The live dashboard path. `backfill.py` is also a grab-bag of helpers imported by four other modules. `search_cache` feeds a "Phase 2" that was never built. |
| [poller.py](backend/app/poller.py), [alerts.py](backend/app/alerts.py) | Delete (turn off now) | Emails alerts from an unvalidated detector: 4,543 flags, 169 alerted. It also hits the API every 5 minutes whenever the server is up. |
| [risk.py](backend/app/risk.py), [trade_tracker.py](backend/app/trade_tracker.py) | Keep | Small, pure, well tested. Needed when something is worth trading. |
| [trade_watcher.py](backend/app/trade_watcher.py) | Park | `MockTrader` fills any size at the requested price with zero fees (lines 55–65), so its P&L means nothing. Replace with paper trading against real order books. |
| [trading_credentials.py](backend/app/trading_credentials.py), [crypto_store.py](backend/app/crypto_store.py), [local_auth.py](backend/app/local_auth.py), [routers/trading.py](backend/app/routers/trading.py) | Park on a branch | Private-key storage for a bot with no order code and no edge is risk with no benefit. No key is stored today (0 rows). |
| `tests/` | Keep tests for kept modules | 204 tests, but `test_backtest.py` (55) mostly pins the old engine, including its fast path matching a slow path with the same bugs. |
| `data/dashboard.db.bak-*`, `data/dev.db` | Delete | Stale backup and an empty file. |
| [README.md](README.md) | Rewrite at step 11 | Describes a "Phase 1 read-only dashboard, not a trading bot". |

**Frontend (`frontend/src/`)**: 2,556 lines across 6 pages. Remove all pages listed in section 5. [BacktestPage.jsx](frontend/src/pages/BacktestPage.jsx) is one 1,185-line component and is the main reason backtesting feels complicated.

### 1.2 Ways the backtest misleads, worst first

**A. Wallet scores see the future (confirmed in the data).**
Wallet history is gated by `timestamp <= T` ([backtest_data.py:404](backend/app/backtest_data.py#L404), [:498](backend/app/backtest_data.py#L498), [backtest_engine.py:820](backend/app/backtest_engine.py#L820)). That timestamp is not the resolution time. Of 232,031 cached positions in markets where we know the resolution time, 60,085 (26%) are stamped before the market resolved, 54,287 by more than a day. At least 26,514 of those carry a P&L that matches holding to resolution. So a wallet's Smart Score at time T includes wins and losses nobody knew yet. Brier also uses final `cur_price` for every position ([scoring.py:180–186](backend/app/scoring.py#L180-L186)).

**B. Wallet history is cut to the newest 300 positions, fetched today.**
[config.py:156](backend/app/config.py#L156), [backfill.py:220](backend/app/backfill.py#L220). For an old checkpoint, an active wallet's newest 300 positions can all postdate T. That wallet then looks like it has no history: "Unrated" for smart money, and "brand-new thin wallet" for the insider detector ([insiders.py:85–86](backend/app/insiders.py#L85-L86) gives age factor 1.0). 708 wallets sit exactly at the cap and 2,797 are within 10 of it. These are the most active wallets, which matter most. Also unverified: 70% of cached positions settled as winners (2.25M vs 0.99M losers), which suggests `/closed-positions` may omit losing positions that were never redeemed. If so, every win rate is inflated.

**C. Holder reconstruction is wrong for most markets.**
- 313 of 490 markets (64%) hit the Data API's 10,000-offset cap and keep only their newest trades. That is 212 of the 248 markets over $10M. For capped markets, on average the first **78%** of the market's life has no trades at all, and 106 markets are missing over 90%.
- 15.8% of reconstructed (market, wallet, side) balances end negative, which is impossible. On uncapped markets it is still 9.3%. Likely causes: `/trades` may return taker-side fills only by default, and splits, merges, transfers and redemptions never appear in a trade tape.
- Negative balances are silently dropped ([backtest_engine.py:812](backend/app/backtest_engine.py#L812)).

**D. Entry price can come from the future.**
If no price exists at or before T, the code uses the next price up to 48 hours later ([backtest_data.py:466–469](backend/app/backtest_data.py#L466-L469), same in [:332–338](backend/app/backtest_data.py#L332-L338)).

**E. No costs, no fill limits.**
Entry is the last traded price with zero spread, slippage or fees, at any size, in the same instant as the signal ([backtest_engine.py:574–577](backend/app/backtest_engine.py#L574-L577)). 645 trades (36%) entered at ≥ 0.90 and earned +0.1% before costs. Any realistic cost makes that bucket a loser.

**F. Outcomes are mislabelled.**
- `resolved_yes = outcome_prices[0] >= 0.5` ([backtest_data.py:106](backend/app/backtest_data.py#L106)) pays a 50/50 resolution as a full YES win.
- Any outcome not literally named "Yes" is stored as NO ([backtest_data.py:194](backend/app/backtest_data.py#L194)). Five markets ("Jazz vs. Lakers", "Spain vs. Argentina: Team to Advance"…) have every trade on one side. Sports shows 0 YES wins in 28 markets.

**G. Trading after the result is known.**
Checkpoints run until Gamma's `closedTime` ([backtest_engine.py:604–612](backend/app/backtest_engine.py#L604-L612)), often hours or days after the real-world event. 171 of 1,626 trades entered after the scheduled end date.

**H. Market selection is not reproducible and uses hindsight.**
`auto_select_markets` uses unseeded `random` ([routers/backtest.py:143–159](backend/app/routers/backtest.py#L143-L159)), so two identical runs test different markets. It picks from the top 100 by final volume, which nobody knew at the time. 272 of 490 cached markets have no category.

**I. The train/test split is random, not by time.**
[backtest_engine.py:153–169](backend/app/backtest_engine.py#L153-L169). Train and test share the same period, the same wallets and sibling markets from the same event. The objective is raw ROI over 9–37 trades ([:229–239](backend/app/backtest_engine.py#L229-L239)). Nothing stops re-running until the test number looks good.

**J. Thin statistics.**
- One trade per market, first signal wins ([:589](backend/app/backtest_engine.py#L589)). Flat $100. ROI and win rate only.
- No Sharpe, drawdown, confidence interval or baseline. Only 21% of cached markets resolved YES, so "always buy NO" wins 79% of the time and nothing is compared against it.
- A few long shots dominate: the top 10 trades made +$5,297 while the total is −$2,508.
- "Strong Buy" shows +33.6% ROI, but that is 124 trades on only **32 markets** re-run many times.
- Markets in the same event are counted as independent.

**K. Too many knobs.**
Smart Score has 11 constants, tiers 9, verdict 7 plus hard-coded 0.15, 0.30 and conviction weights 0.45/0.35/0.20 ([scoring.py:729](backend/app/scoring.py#L729), [:753](backend/app/scoring.py#L753), [:789](backend/app/scoring.py#L789)), insider about 20. Roughly 50 settings against about 200 independent markets.

**L. Live and backtest are not the same strategy.**
- Live uses the top 40 holders per side from `/holders`. The backtest uses reconstructed holders.
- Live applies trend and a market-maker exclusion. The backtest applies neither ([scoring.py:382](backend/app/scoring.py#L382), [signals.py:87–94](backend/app/signals.py#L87-L94)).
- `first_seen_at` means "when we first polled" live and "first real trade" in the backtest, so the cluster factor differs.
- Live prices NO as `1 − YES` ([trade_watcher.py:191](backend/app/trade_watcher.py#L191)). The backtest uses the NO token's own series.

**M. Slow by design.**
A run is network fetching inside a web-server thread, one at a time, with results polled from a browser. Testing an idea takes minutes to hours.

### 1.3 Further problems (second pass)

**Strategy design**

- **The Smart Score rewards buying favourites, not skill.** It scores win rate, mean return and return/volatility per position ([scoring.py:170–178](backend/app/scoring.py#L170-L178), [:247–256](backend/app/scoring.py#L247-L256)). A wallet that only buys 0.90 favourites wins about 90% of the time with low volatility and scores near the top without beating the market at all. Skill has to be measured against the price paid: did the wallet win more often than its entry prices implied?
- **Every position counts equally.** `mean_return` is an unweighted mean, so a $5 bet weighs the same as a $50,000 one.
- **The "undervalued gap" is partly mechanical.** YES and NO shares are created in pairs, so across all holders the dollar share on YES is close to the YES price by construction. The gap then mostly reflects which top-40 holders happen to have a score, not a mispricing.
- **Signals favour markets where big holders have history.** Wallets outside the 16 discovery samples default to fetch status "error" ([backtest_engine.py:829](backend/app/backtest_engine.py#L829)) and are invisible to the insider detector; markets with over 70% unrated dollars give no signal at all. Results describe that subset only.

**Data**

- **Real trades are dropped as duplicates.** The unique index on (market, wallet, outcome, side, price, size, timestamp) ([db.py:158–159](backend/app/db.py#L158-L159)) collapses two genuine identical fills in the same second into one. This is another source of wrong balances.
- **Neg-risk conversions** move holdings between outcomes of an event without any trade, so the tape cannot see them.
- **Coarse time grid.** Prices are hourly, checkpoints six-hourly and capped at 200 per market, so a year-long market is checked about every two days. Fast-moving signals (insiders, news) cannot be tested at that resolution.

**Measurement and process**

- **Return ignores time.** A 5% gain locked up for nine months is treated the same as 5% in a day. Reports need return per day of capital used.
- **Insider overrides are only half applied.** `summarize_insider_rows` reads the global config for `activity_scale_usd` and `balanced_imbalance` ([insiders.py:427–430](backend/app/insiders.py#L427-L430)), so a run with overrides mixes two configurations.
- **No record of what was tried.** 134 runs with no written hypothesis or decision. With enough attempts one will look good by chance, and there is no count of attempts to correct for it. The run log in section 2 must include the hypothesis, stated before the run.

**Not yet checkable from the code alone** (step 1 settles these): whether hourly prices are mids or last trades, whether `closedTime` is the true resolution time, and whether the wallet endpoint omits losers.

## 2. Research setup

A new top-level `research/` package, separate from the web app. No server, no threads, no UI.

**Data, downloaded once into Parquet (queried with DuckDB or pandas):**

| Table | Contents | Notes |
|---|---|---|
| `markets` | Every resolved market: ids, event id, question, outcomes, token ids, tags, start/end/closed times, neg-risk flag, fee flag | Thousands, not 490 |
| `resolutions` | Payout per token (1, 0 or 0.5) and the time the outcome became known | Mapped by token id, never by the word "Yes" |
| `prices` | Hourly price per token | Have this for 490 markets |
| `trades` | Full trade tape, both sides of each fill | Needs a source without the 10k cap (step 1) |
| `wallet_stats` | Built from our own `trades` + `resolutions` | A position counts only after its market resolved, so point-in-time by construction, and no 300 cap |
| `books` | Live order-book snapshots, recorded from now on | Cannot be backfilled |

**One call per idea:**

```python
from research import backtest
from research.strategies import favourite_longshot

report = backtest(favourite_longshot(min_price=0.85), split="validation")
print(report)
```

**Rules built into the harness, not left to each strategy:**

- **As-of data only.** A strategy receives a snapshot object that can only return data stamped ≤ t. A test feeds a "cheating" strategy and must fail it.
- **Same code live and in backtests.** A strategy is a function from snapshot to desired position. The live scanner builds the snapshot from current data and calls the same function.
- **Costs always on.** Fill at the next price after a delay (default one hour), plus half a spread that depends on price and liquidity, plus the market's fee. Size capped at a fraction of the volume that traded next. 50/50 resolutions pay 0.5.
- **Split by time.** Proposed from the cache (74 markets resolved in 2024, 212 in 2025, 203 in 2026): train = resolved before 2025-07-01, validation = 2025-07-01 to 2025-12-31, test = 2026 onward. The test set is locked: every run on it is logged, and it is used once per frozen strategy.
- **Standard report.** Trades, independent events, P&L, return on capital, Sharpe, max drawdown, win rate against average entry price, a calibration table, P&L by price bucket, category and time to resolution, and a bootstrap confidence interval resampled by event.
- **Baselines in every report.** Random side at market price, always NO, always the favourite, and follow the biggest holders with no scoring.
- **Run log.** Every run appends git hash, parameters, split and results to `research/runs.jsonl`.
- **Speed target.** 2,000 markets in under 10 seconds from local files.

## 3. Strategy families

| Family | Data needed | Have it? | Simple first version | How it could fool us |
|---|---|---|---|---|
| **Statistical biases** | Prices, resolutions | Yes for 490, cheap to extend | Buy favourites above a price threshold with N days left; test long shots, new markets, by category | Edge smaller than the spread; one bad loss wipes many small wins; bias differs by era and category |
| **External-data pricing (weather)** | Forecast ensembles as issued at the time, exact resolution source and station | No | Daily temperature markets: probability from the forecast distribution, trade when it differs from price by more than costs | Using revised forecasts instead of what was published; wrong station or rounding rule; tiny liquidity |
| **Arbitrage and consistency** | Simultaneous bid/ask on linked markets | No, needs live recording | Outcome sets whose asks sum below 1; pairs where A implies B but A is priced higher | Last-trade prices show gaps that never existed at the bid/ask; bots take real ones in seconds; legs fill separately |
| **Copy successful wallets** | Full tape, point-in-time wallet stats | No (current data is biased, 1.2 A–C) | Rank wallets on train-period results only; follow new positions with a delay | Luck looks like skill with thousands of wallets; price moves before we enter; skill decays; market makers and hedgers are not making bets |
| **Fade unsuccessful wallets** | Same | No | Trade against the bottom group's new positions | Bad traders are small, so the signal is weak; losers at long shots are just paying the long-shot bias |
| **Insider / unusual activity** | Full tape with true first-trade times, wallet age | Partly | New wallet + large size + close to resolution; measure return after the flagged trade | Very few true cases; price has usually moved already; whales splitting across fresh wallets look the same |
| **ML** | Outputs of the families above | No | See below | Overfits small noisy data easily |

**ML, later and only as a combiner.**
- Features: signal values from families that already work, price, time to resolution, liquidity, category.
- Label: resolved outcome. The model predicts a probability and must beat the market price on Brier score out of sample.
- Start with logistic regression, then gradient boosting with few features and strong regularisation.
- Validate walk-forward in time with a gap between train and test, grouped by event.
- Worth trying only with at least two families showing an edge on validation and about 1,000 independent resolved events carrying those signals.

## 4. Ranking and what to test first

| Rank | Family | Expected edge | Ease of testing | Data |
|---|---|---|---|---|
| 1 | Statistical biases | Small to medium | Easy | Have |
| 2 | External-data pricing (weather) | Medium to high, low capacity | Medium | Must collect |
| 3 | Copy successful wallets | Unknown | Medium, after the tape is fixed | Must rebuild |
| 4 | Arbitrage and consistency | Small, heavily competed | Hard without book history | Must record |
| 5 | Fade unsuccessful wallets | Low | Free once 3 exists | Same as 3 |
| 6 | Insider | High per event, very rare | Hard | Same as 3 |
| 7 | ML | Depends on the above | Last | None yet |

**Test first:**
1. **Statistical biases.** Needs only data we have, proves the harness, and gives the baseline every other family must beat.
2. **Weather pricing.** The only family where we would know something the price might not.
3. **Copy wallets, rebuilt on clean data.** The original idea deserves one honest test, and the same data serves fade and insider.

Start the order-book recorder at the same time as step 3 below. It costs little and that data can never be recovered later.

## 5. Interface

Build nothing until a strategy survives validation. Then three screens:

- **Opportunities:** live signals from surviving strategies with side, price, estimated edge after costs and suggested size.
- **Positions and risk:** open positions, exposure, P&L, caps (reuses `risk.py` and `trade_tracker.py`).
- **Research results:** read-only view of the latest report per strategy.

**Remove:**
- Pages: `OverviewPage`, `MarketList`, `MarketDetail`, `InsidersPage`, `BacktestPage`, and the wallet section of `TradingPage`.
- Components: `SignedHistogram`, `WhalePie`, `VerdictBadge`, `InsiderSummary`, `Disclaimer`.
- Keep `Sidebar`, `TopBar`, `chartTheme.js`, `format.js`, `api.js` (trimmed).

## 6. Step-by-step plan

Each step is one or a few commits. Do not start a step until the previous "done when" is met.

| # | Step | Done when |
|---|---|---|
| 0 | **Freeze.** Tag the current state `dashboard-v1`. Set the poller off by default. Delete `dashboard.db.bak-*` and `dev.db`. | Tag exists; server starts without polling; tests still pass. |
| 1 | **Data facts spike.** Answer with saved sample responses: (a) does `/trades` return taker fills only, and is there a flag for all fills; (b) what `/closed-positions` `timestamp` means; (c) are unredeemed losing positions included; (d) how to get a full tape past the 10k cap (time-window paging, the public subgraph, or on-chain logs); (e) current fee schedule by market type; (f) what `/prices-history` returns (mid or last trade). | `research/DATA_NOTES.md` answers all six with evidence. |
| 2 | **Data store v1.** Downloader for `markets`, `resolutions`, `prices` for all resolved markets since 2024. Outcomes mapped by token id; 50/50 handled. | One command rebuilds it; a quality report shows counts per year and category, zero unmapped outcomes, and 50/50 markets listed. |
| 3 | **Harness core.** Strategy interface, as-of snapshot, cost model, time split, report, baselines, run log. Start the order-book recorder as a separate small script. | Baselines run on 2,000+ markets in under 10 s; random-side baseline loses about the modelled cost; the cheating-strategy test fails as intended; recorder writes snapshots. |
| 4 | **Family 1: statistical biases.** 3–5 simple variants on train and validation. | `research/FINDINGS.md` records each variant with report and a keep or kill decision. |
| 5 | **Full trade tape and wallet stats.** Using the source chosen in step 1. | Negative-balance rate under 1%; reconstructed holders match `/holders` on 20 recent markets; wallet stats use only resolved-by-T positions. |
| 6 | **Family 3: copy wallets, then fade and insider** on the clean data. | Findings entry for each, with entry-delay sensitivity (1 min, 1 h, 6 h). |
| 7 | **Family 2: weather.** Collect as-issued forecasts and market rules for one market type; backtest. | Findings entry; resolution rule verified against 20 past markets by hand. |
| 8 | **Arbitrage check** on recorded books (needs about 4 weeks of data from step 3). | Findings entry with opportunities counted at real bid/ask and size. |
| 9 | **Test set, once.** Run frozen survivors on the locked test period. | Logged in `runs.jsonl`; survivors named in `FINDINGS.md`. |
| 10 | **Paper trading.** Live scanner calls the same strategy code, logs signals and would-be fills against real books for 4+ weeks. | Paper results sit inside the backtest's confidence interval. |
| 11 | **Minimal UI and deletion.** Build the three screens; delete everything marked delete in 1.1; rewrite README. | Old pages and modules gone; tests pass; README matches reality. |
| 12 | **ML combiner**, only if two or more families survived. | Beats the best single family on validation, then on test. |
| 13 | **Small real money**, only for strategies that passed steps 9 and 10. Real order code behind `risk.py` caps. | First real fills reconciled to the cent against the exchange. |

**Kill rule for any strategy:** dropped if, on validation with at least 200 independent events, the confidence interval for return after costs includes zero. Fewer than 200 events means "not enough data", not "works".

## 7. Open questions for you

1. **Capital and trade size.** How much would you deploy in total and per trade? Several edges here only work at tens to hundreds of dollars per trade.
2. **Access.** Can you legally trade on Polymarket from where you live, and on which venue? This decides fees and whether steps 10–13 are worth building.
3. **Holding period.** Are you happy locking money up for months, or only for days to weeks?
4. **The live dashboard.** Do you use the Markets, Insiders or Overview pages day to day? If not, they go at step 11.
5. **Automation.** Do you want alerts that you trade by hand, or a fully automated bot?
6. **Paid data.** Is there a budget for historical order-book data or a paid API tier? It would shorten steps 5 and 8.
7. **External data.** Which markets do you know well (weather, sport, crypto, politics)? That should pick the first target for family 2.
8. **The cache.** Is the 4.6 GB `dashboard.db` backed up, and may it be treated as disposable once the new store exists?
9. **Insider-following.** Are you comfortable trading on signs that someone else has inside information? Check the platform rules and your local law before step 13.
