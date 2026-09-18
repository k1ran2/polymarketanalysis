"""
SQLite schema + thin connection helper. One file, zero setup (§8).

Tables:
  traders               - one row per wallet: raw stats + lifetime score/tier/trend
  markets                - one row per market (conditionId): metadata from Gamma
  market_holders_cache    - cached holder snapshots for a market (§5 full reconstruction
                            OR §6 on-demand fast path), tagged with `source` and `fetched_at`
  search_cache            - tracks how many times a market/query has been searched,
                            to drive the "earns full reconstruction" rule (§6)
  insider_flags           - detected insider-pattern positions (app.insiders)
  backtest_market_meta,
  backtest_market_trades,
  backtest_price_history,
  backtest_wallet_positions,
  backtest_wallet_sync,
  backtest_runs,
  backtest_trades_sim    - point-in-time backtest replay (app.backtest_data / app.backtest_engine)

Everything the API layer reads is a plain SELECT against these tables — the
API/UI never calls Polymarket live on a page request except the on-demand
search fast path (§6), which writes into market_holders_cache as it goes.
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "dashboard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    wallet TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    volume REAL NOT NULL,
    win_rate_weighted REAL,
    mean_return REAL,
    stdev_return REAL,
    sharpe_like REAL,
    brier_score REAL,
    n_calibration INTEGER,
    last_active_ts INTEGER,
    first_active_ts INTEGER,  -- earliest observed trade -- see scoring.tenure_days
    fetch_status TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error' -- see pipeline.record_fetch_failures
    fetch_error TEXT,
    tier TEXT NOT NULL,
    raw_composite REAL,
    percentile REAL,
    recent_raw_composite REAL,
    recent_percentile REAL,
    recent_n INTEGER,
    trend TEXT,
    trend_delta REAL,
    lookback_days INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    event_id TEXT,
    question TEXT,
    slug TEXT,
    category TEXT,
    outcomes TEXT,        -- JSON array, e.g. ["Yes","No"]
    outcome_prices TEXT,  -- JSON array, e.g. ["0.63","0.37"]
    clob_token_ids TEXT,  -- JSON array [yesTokenId, noTokenId]
    volume REAL,
    volume_24hr REAL,
    liquidity REAL,
    end_date TEXT,       -- ISO date from Gamma, for insiders._urgency_factor
    active INTEGER,
    closed INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS market_holders_cache (
    condition_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    side TEXT NOT NULL,          -- 'YES' | 'NO'
    position_usd REAL NOT NULL,
    source TEXT NOT NULL,        -- 'holders_fast_path' | 'trade_reconstruction'
    fetched_at INTEGER NOT NULL,
    first_seen_at INTEGER,       -- set once, on first insert -- see insiders._cluster_factor
    PRIMARY KEY (condition_id, wallet, side)
);

CREATE TABLE IF NOT EXISTS search_cache (
    condition_id TEXT PRIMARY KEY,
    search_count INTEGER NOT NULL DEFAULT 0,
    last_searched_at INTEGER,
    reconstruction_queued INTEGER NOT NULL DEFAULT 0
);

-- Detected insider-pattern positions (app.insiders): large / sudden positions
-- from thin-history or barely-active wallets. One row per (market, wallet,
-- side); re-scanned each time that market is analyzed.
CREATE TABLE IF NOT EXISTS insider_flags (
    condition_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    side TEXT NOT NULL,
    position_usd REAL NOT NULL,
    insider_score REAL NOT NULL,
    reasons TEXT NOT NULL,        -- JSON array of human-readable reason strings
    tenure_days REAL,             -- days since the wallet's earliest observed trade, if known
    n_resolved INTEGER,
    size_multiple REAL,           -- position_usd / the wallet's own historical avg bet, if known
    detected_at INTEGER NOT NULL,
    alerted_at INTEGER,           -- set once an email alert has been sent for this row
    PRIMARY KEY (condition_id, wallet, side)
);

CREATE INDEX IF NOT EXISTS idx_traders_percentile ON traders(percentile);
CREATE INDEX IF NOT EXISTS idx_traders_tier ON traders(tier);
CREATE INDEX IF NOT EXISTS idx_markets_volume ON markets(volume DESC);
CREATE INDEX IF NOT EXISTS idx_holders_condition ON market_holders_cache(condition_id);
CREATE INDEX IF NOT EXISTS idx_insider_score ON insider_flags(insider_score DESC);
CREATE INDEX IF NOT EXISTS idx_insider_detected ON insider_flags(detected_at DESC);

-- ------------------------------------------------------------------------
-- Backtesting (app.backtest_data / app.backtest_engine): point-in-time
-- replay of the Smart Money / Insider signals against already-RESOLVED
-- markets, using real historical trades + prices, never live/current data.
-- ------------------------------------------------------------------------

-- One row per resolved market we've pulled from Gamma to backtest against.
-- resolved_yes = 1/0 -- which side actually won -- is the ground truth every
-- simulated trade is graded against.
CREATE TABLE IF NOT EXISTS backtest_market_meta (
    condition_id TEXT PRIMARY KEY,
    question TEXT,
    slug TEXT,
    category TEXT,
    clob_token_ids TEXT,   -- JSON [yesTokenId, noTokenId]
    volume REAL,
    start_ts INTEGER,      -- Gamma startDate
    end_ts INTEGER,        -- Gamma endDate (scheduled)
    resolved_ts INTEGER,   -- Gamma closedTime (actual resolution)
    resolved_yes INTEGER,  -- 1 if YES won, 0 if NO won; NULL if not yet resolved (excluded from backtests)
    trades_synced_at INTEGER,   -- set once the trade tape has been pulled (a resolved market's tape is static)
    trades_capped INTEGER NOT NULL DEFAULT 0,  -- 1 if Data API's hard 10k-offset cap cut the tape short (see backtest_data.sync_market_trades)
    prices_synced_at INTEGER,
    synced_at INTEGER NOT NULL
);

-- Raw trade tape per backtested market, from Data API /trades -- the only
-- way to reconstruct who held what at a past point in time, since Polymarket
-- exposes no historical positions/holders snapshot endpoint.
CREATE TABLE IF NOT EXISTS backtest_market_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    side TEXT NOT NULL,        -- 'BUY' | 'SELL'
    outcome TEXT NOT NULL,     -- 'YES' | 'NO'
    price REAL NOT NULL,
    size REAL NOT NULL,        -- shares
    timestamp INTEGER NOT NULL,
    tx_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bt_trades_dedupe
    ON backtest_market_trades(condition_id, wallet, outcome, side, price, size, timestamp);
CREATE INDEX IF NOT EXISTS idx_bt_trades_market_ts ON backtest_market_trades(condition_id, timestamp);

-- Historical price series per outcome token, from CLOB /prices-history.
-- NOTE this is Polymarket's last-traded price at each sample, not a true
-- historical bid/ask -- Polymarket doesn't expose historical order-book
-- depth publicly, so it's the best available proxy for "the price you could
-- have executed at" (see app.backtest_engine module docstring).
CREATE TABLE IF NOT EXISTS backtest_price_history (
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (token_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_bt_prices_market_ts ON backtest_price_history(condition_id, timestamp);

-- A wallet's full closed-position history, cached once per wallet and reused
-- across every backtest checkpoint/run that touches it (the same wallet
-- shows up across many markets). Point-in-time gating happens at query time
-- (WHERE timestamp <= T) -- see app.backtest_engine.wallet_raw_stats_asof.
CREATE TABLE IF NOT EXISTS backtest_wallet_positions (
    wallet TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    avg_price REAL NOT NULL,
    total_bought REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    cur_price REAL NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (wallet, condition_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_bt_wallet_positions_wallet ON backtest_wallet_positions(wallet, timestamp);

CREATE TABLE IF NOT EXISTS backtest_wallet_sync (
    wallet TEXT PRIMARY KEY,
    synced_at INTEGER NOT NULL,
    fetch_status TEXT NOT NULL DEFAULT 'ok'
);

-- One row per backtest the user has run from the UI.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'done' | 'error'
    params TEXT NOT NULL,       -- JSON: strategy, thresholds, market filter, stake, checkpoint interval
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    summary TEXT                -- JSON: win_rate, total_pnl, n_trades, etc. -- set once status='done'
);

-- One row per simulated trade a completed run produced.
CREATE TABLE IF NOT EXISTS backtest_trades_sim (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    question TEXT,
    category TEXT,
    side TEXT NOT NULL,             -- side the strategy went long on
    signal_type TEXT,               -- 'strong_buy' | 'lean' | 'fade' (smart_money) | 'insider' -- lets a run's summary tell "the strict case works" from "only the loose fallback fired"
    signal_ts INTEGER NOT NULL,     -- checkpoint time the signal first crossed threshold
    signal_value REAL NOT NULL,     -- conviction/insider score at entry
    entry_price REAL NOT NULL,      -- last-traded price nearest signal_ts (see backtest_price_history note)
    stake_usd REAL NOT NULL,
    shares REAL NOT NULL,
    resolved_yes INTEGER NOT NULL,
    won INTEGER NOT NULL,
    pnl_usd REAL NOT NULL,
    pnl_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_sim_run ON backtest_trades_sim(run_id);

-- A weight-optimization search (app.backtest_engine.run_search): random
-- search over one strategy's parameter space, evaluated on a TRAIN market
-- set, with the single best-found config re-evaluated on a separate, never
-- touched-during-search TEST set -- the held-out validation step that makes
-- "optimal" mean something more than "happened to fit this exact history".
CREATE TABLE IF NOT EXISTS backtest_searches (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'done' | 'error'
    params TEXT NOT NULL,        -- JSON: strategy, config_kind, param_space, n_iterations, train/test condition_ids, base run params, seed
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,  -- n_iterations + 1 (the final held-out test evaluation)
    history TEXT,                 -- JSON array, appended as each iteration completes: [{overrides, run_id, summary}, ...]
    best_overrides TEXT,          -- JSON: the winning param combo found on TRAIN
    best_train_summary TEXT,      -- JSON: that combo's TRAIN performance
    test_summary TEXT,            -- JSON: the SAME combo's performance on the held-out TEST set -- the number that actually matters
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_searches_created ON backtest_searches(created_at DESC);
"""

# Lightweight migrations for columns added after the first schema.
MIGRATIONS = [
    # rank in the curated market list (NULL = only reachable via search / direct link)
    "ALTER TABLE markets ADD COLUMN list_rank INTEGER",
    # Distinguishes "we tried to fetch this wallet's history and Polymarket's
    # API errored" from "we fetched it fine and it's genuinely thin" -- the
    # first should be retried soon, the second is just an Unrated wallet.
    "ALTER TABLE traders ADD COLUMN fetch_status TEXT NOT NULL DEFAULT 'ok'",
    "ALTER TABLE traders ADD COLUMN fetch_error TEXT",
    # Earliest timestamp seen across a wallet's (fetched) closed positions --
    # a "how long have they been active on Polymarket" proxy, free of any
    # extra API calls since it's derived from data we already pull.
    "ALTER TABLE traders ADD COLUMN first_active_ts INTEGER",
    # Research-informed insider signals (see app/insiders.py module docstring):
    # when we first noticed a holder in a market (for wallet-clustering), and
    # a market's resolution date (for "position taken right before resolution").
    "ALTER TABLE market_holders_cache ADD COLUMN first_seen_at INTEGER",
    "ALTER TABLE markets ADD COLUMN end_date TEXT",
    # The flagged wallet's own Smart Score/tier at scan time, alongside its
    # insider_score -- these are two independent numbers (historical skill vs.
    # "is this specific bet out of character for them") and the Insiders UI
    # shows both side by side rather than making a reader cross-reference.
    "ALTER TABLE insider_flags ADD COLUMN score_percentile REAL",
    "ALTER TABLE insider_flags ADD COLUMN tier TEXT",
    # Data API's /trades hard-caps offset at 10,000 (confirmed live) -- a
    # market with a bigger trade tape than that gets its most-recent 10k
    # trades only, so early-life point-in-time reconstruction is incomplete.
    "ALTER TABLE backtest_market_meta ADD COLUMN trades_capped INTEGER NOT NULL DEFAULT 0",
    # When THIS SYSTEM first observed the flagged wallet holding this
    # position (market_holders_cache.first_seen_at) -- a far more useful
    # "time" for a flag than detected_at (which is just "when this scan
    # cycle ran," near-identical across every flag from the same scan).
    # NOT the trade's real execution time -- see app.insiders module docstring.
    "ALTER TABLE insider_flags ADD COLUMN first_seen_at INTEGER",
    # Which of Strong Buy / Lean / Fade (or, for the insider strategy, just
    # 'insider') actually fired -- without this, a run that allows all three
    # can't tell you whether its result comes from the strict, high-conviction
    # case or is being carried (or dragged down) by the loosest fallback.
    "ALTER TABLE backtest_trades_sim ADD COLUMN signal_type TEXT",
]


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The API request threadpool and the on-demand background scoring jobs each
    # open their own connection; WAL + a busy timeout lets a reader proceed
    # while a writer holds the file, and makes a concurrent writer wait rather
    # than fail with "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already applied
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized SQLite DB at {DB_PATH}")
