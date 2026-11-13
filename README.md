# Polymarket Research

A local tool for finding and testing Polymarket trading strategies. It downloads resolved markets, prices and trades once, then answers "would this rule have made money after costs?" in about a second.

Research only. Nothing here places orders, and nothing here is financial advice.

## Run it

```bash
# one-off setup
cd backend && python3 -m venv venv && venv/bin/pip install -r ../research/requirements.txt && cd ..
cd frontend && npm install && cd ..

# download data (resumable; about an hour in total the first time)
backend/venv/bin/python -m research.ingest markets --since 2024-07-01 --min-volume 10000
backend/venv/bin/python -m research.ingest prices --sample-frac 0.12
backend/venv/bin/python -m research.ingest trades --min-cash 500 --limit 12002

# run the standard analyses and write the results the interface reads
backend/venv/bin/python -m research.publish

# start the interface
backend/venv/bin/uvicorn research.server:app --port 8000
cd frontend && npm run dev        # then open http://localhost:5173
```

## The interface

| Page | What it is for |
|---|---|
| Overview | Plain-English findings, whether prices are fair, and a scoreboard of every strategy tested |
| Strategy lab | Describe a simple rule with a few inputs and see its result on two separate periods |
| Pattern scan | Hundreds of simple rules tested automatically, with the ones that held on unseen data |
| Traders | Whether past skill carries forward, what copying skilled traders returns, and a leaderboard |

## Testing an idea in code

```python
from research import engine, strategies

rep = engine.backtest(strategies.favourite(0.85), split="validation",
                      hypothesis="strong favourites are underpriced")
print(rep.text())
```

A strategy is a function from the as-of data panel to buy outcome 0, buy outcome 1, or do nothing. See [research/strategies.py](research/strategies.py).

## Rules the engine enforces

- **No lookahead.** A price is treated as known only at the end of its bucket, and a trade fills one bucket later, before the outcome was known.
- **Costs always on.** Each trade pays a half-spread (1 cent by default) and the current Polymarket taker fee for its category.
- **Split by time.** Train is markets resolved before 2025-10-01, validation runs to 2026-04-01, and everything later is a locked test period for a final check.
- **Honest uncertainty.** Every return comes with a likely range, resampled by event so related markets are not counted as independent.
- **Unbiased markets.** Price analyses use a random sample of all markets, never markets picked by their final volume.
- **Sizing counts.** Trader skill is dollars made per dollar put at risk, using only results that were already known at the time.
- **Everything is logged.** Each backtest appends its parameters, hypothesis and result to `research/data/runs.jsonl`.

## Layout

| Path | Contents |
|---|---|
| [research/](research/) | Downloader, data panel, backtest engine, pattern scan, trader scoring, API server, tests |
| [research/DATA_NOTES.md](research/DATA_NOTES.md) | What the Polymarket APIs return, checked live |
| [frontend/](frontend/) | The React interface |
| [PLAN.md](PLAN.md) | The audit of the original dashboard and the step-by-step plan |
| `backend/app/` | The original dashboard backend. Retired: it depends on Polymarket's Data API v1, which now returns HTTP 410. Kept for reference until deleted. |

## Tests

```bash
backend/venv/bin/python -m pytest research/tests -q
```
