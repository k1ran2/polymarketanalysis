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

-- ---------------------------------------------------------------------
-- Live automated trading (app.trade_watcher / app.clob_trader / app.
-- trade_tracker). Built and tested against a mock exchange first -- see
-- those modules' docstrings for the phased build order and the hard rule
-- that no code here ever submits a real order except the user's own
-- unattended, explicitly-opted-into bot (POLY_LIVE_TRADING env var).
-- ---------------------------------------------------------------------

-- Single row (id=1). Operating settings, caps, and the kill switch --
-- `enabled` is the user's own sticky on/off; it is NEVER auto-flipped by
-- the watcher (see trading_day_ledger.halted for the auto-halt, a
-- deliberately separate concept: a bad day's loss cap should clear itself
-- at UTC midnight, but a human's kill switch should only ever be undone by
-- a human).
CREATE TABLE IF NOT EXISTS trading_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT 'smart_money',   -- 'smart_money' | 'insider' | 'both'
    -- hard caps -- deliberately tiny defaults (see app.risk / the trading
    -- docs): neither strategy has a demonstrated edge on held-out data as
    -- of this build, so "live" should mean "real fills at negligible size
    -- to see if the signal survives contact with a real order book", not
    -- "confident-sized bets on an unproven signal".
    max_stake_usd REAL NOT NULL DEFAULT 1.0,
    max_open_positions INTEGER NOT NULL DEFAULT 3,
    max_open_exposure_usd REAL NOT NULL DEFAULT 5.0,
    max_position_per_market_usd REAL NOT NULL DEFAULT 1.0,
    max_daily_loss_usd REAL NOT NULL DEFAULT 10.0,
    max_daily_spend_usd REAL NOT NULL DEFAULT 25.0,
    max_trades_per_day INTEGER NOT NULL DEFAULT 5,
    -- signal gates -- mirror app.signals' params dict field-for-field
    min_conviction REAL NOT NULL DEFAULT 70.0,
    min_signal_strength TEXT NOT NULL DEFAULT 'strong_buy',
    max_entry_price REAL NOT NULL DEFAULT 0.90,     -- tighter than the backtest's 0.98 default -- see BacktestConfig.max_no_edge_entry_price's docstring for why near-certain entries are poor risk/reward, doubly so live
    min_entry_price REAL NOT NULL DEFAULT 0.05,
    max_slippage_pct REAL NOT NULL DEFAULT 2.0,
    min_market_liquidity_usd REAL NOT NULL DEFAULT 5000.0,
    min_hours_to_resolution REAL NOT NULL DEFAULT 6.0,
    order_type TEXT NOT NULL DEFAULT 'FOK',
    -- watcher
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    universe_size INTEGER NOT NULL DEFAULT 40,
    resignal_cooldown_hours REAL NOT NULL DEFAULT 24.0,
    updated_at INTEGER NOT NULL
);

-- Single row (id=1). NOTHING in this table is ever returned by any API
-- response -- see app.trading_credentials.
CREATE TABLE IF NOT EXISTS trading_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ciphertext BLOB NOT NULL,          -- Fernet(private key) -- never plaintext
    key_source TEXT NOT NULL,          -- 'keyring' | 'keyfile' | 'passphrase'
    signature_type INTEGER NOT NULL,   -- 0 EOA | 1 POLY_PROXY | 2 POLY_GNOSIS_SAFE
    signer_address TEXT NOT NULL,      -- derived locally from the key; public
    funder_address TEXT NOT NULL,      -- address holding the money; public
    chain_id INTEGER NOT NULL DEFAULT 137,
    api_key_ciphertext BLOB,           -- derived L2 creds, also encrypted
    api_secret_ciphertext BLOB,
    api_passphrase_ciphertext BLOB,
    created_at INTEGER NOT NULL,
    last_verified_at INTEGER,
    last_error TEXT
);

-- Every signal the watcher evaluated on a live market, traded or not -- the
-- shadow log. Lets you see what the bot skipped and why, and doubles as the
-- dedupe key (don't re-signal the same market+strategy+side within
-- resignal_cooldown_hours).
CREATE TABLE IF NOT EXISTS trade_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT NOT NULL,                -- 'YES' | 'NO'
    strategy TEXT NOT NULL,
    signal_type TEXT,                  -- 'strong_buy' | 'lean' | 'fade' | 'insider'
    conviction REAL NOT NULL,
    market_price REAL,
    detected_at INTEGER NOT NULL,
    action TEXT NOT NULL,              -- 'traded' | 'skipped'
    skip_reason TEXT,                  -- 'paused' | 'cooldown' | 'cap_open_positions' | 'cap_daily_loss' |
                                        -- 'cap_daily_spend' | 'price_out_of_band' | 'slippage' | 'liquidity' |
                                        -- 'insufficient_collateral' | 'order_rejected' | 'already_open'
    snapshot TEXT,                     -- JSON verdict/insider summary, for later forensics
    trade_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trade_signals_key ON trade_signals(condition_id, strategy, side, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_signals_ts ON trade_signals(detected_at DESC);

-- One row per position opened, by the bot or logged by hand -- also the
-- trade tracker's data model (see app.trade_tracker's module docstring for
-- why this is ONE table, not a separate "tracker" data model: a bot-only
-- P&L view and a hand-logged one disagreeing with each other would be the
-- worst possible failure for a tool whose whole job is telling you whether
-- a strategy works).
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'bot',    -- 'bot' | 'manual' | 'imported'
    condition_id TEXT NOT NULL,
    token_id TEXT,                         -- nullable: a manually-logged trade may not have a known CLOB token id
    question TEXT,
    category TEXT,
    side TEXT NOT NULL,
    strategy TEXT,
    signal_type TEXT,
    signal_conviction REAL,
    -- intent
    requested_price REAL NOT NULL,
    requested_size_shares REAL NOT NULL,
    requested_stake_usd REAL NOT NULL,
    order_type TEXT NOT NULL,
    client_order_id TEXT UNIQUE,           -- idempotency key, set BEFORE submitting -- see app.trade_watcher
    -- result
    status TEXT NOT NULL,                  -- pending | submitted | filled | partial | rejected | failed | cancelled | resolved
    exchange_order_id TEXT,
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    fees_usd REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,      -- what actually left the wallet
    error TEXT,
    submitted_at INTEGER,
    filled_at INTEGER,
    -- lifecycle
    exit_kind TEXT,                        -- 'resolution' | 'sold' | NULL while open
    exit_price REAL,
    resolved_yes INTEGER,
    realized_pnl_usd REAL,
    closed_at INTEGER,
    last_mark_price REAL,
    last_marked_at INTEGER,
    notes TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(condition_id);

-- Append-only audit log. Never contains secrets -- this is what you read
-- to understand what the bot did and why, after the fact.
CREATE TABLE IF NOT EXISTS trading_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,     -- info | warn | error
    kind TEXT NOT NULL,      -- watcher_cycle | preflight | order_submitted | order_filled |
                              -- cap_breached | halted | resumed | creds_set | creds_cleared | reconcile
    condition_id TEXT,
    trade_id INTEGER,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_trading_events_ts ON trading_events(ts DESC);

-- One row per UTC day. Survives process restarts, so the day's budget
-- can't be reset just by restarting the server. `halted` is the auto-halt
-- (max_daily_loss_usd breached) -- distinct from trading_settings.enabled,
-- the user's own sticky kill switch; this one clears itself at UTC
-- midnight, that one only a human clears.
CREATE TABLE IF NOT EXISTS trading_day_ledger (
    day TEXT PRIMARY KEY,               -- 'YYYY-MM-DD' UTC
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    spent_usd REAL NOT NULL DEFAULT 0,
    n_trades INTEGER NOT NULL DEFAULT 0,
    halted INTEGER NOT NULL DEFAULT 0,
    halted_reason TEXT,
    updated_at INTEGER NOT NULL
);
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
