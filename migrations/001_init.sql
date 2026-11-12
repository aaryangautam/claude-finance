-- 001_init: core tables. Sign convention everywhere: amount > 0 = money INTO the account.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id              TEXT PRIMARY KEY,           -- schema id: checking, card_everyday, ... or provisional 'plaid:<account_id>'
    institution     TEXT NOT NULL,
    plaid_item_id   TEXT,
    plaid_account_id TEXT UNIQUE,
    wise_balance_id TEXT UNIQUE,
    name            TEXT,
    type            TEXT NOT NULL,              -- checking|savings|credit|loan|brokerage|ira|wise_usd|wise_eur
    currency        TEXT NOT NULL,
    role            TEXT,                       -- hub|savings|liability|credit|ira|brokerage|fx_hub (from Budget Schema)
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plaid_items (
    item_id         TEXT PRIMARY KEY,
    institution     TEXT NOT NULL,
    institution_id  TEXT,
    token_env_key   TEXT NOT NULL UNIQUE,       -- e.g. PLAID_TOKEN_MYBANK; the token itself lives in .env
    cursor          TEXT,                       -- /transactions/sync cursor
    products        TEXT,                       -- JSON list
    status          TEXT NOT NULL DEFAULT 'ok', -- ok|login_required|error
    last_synced     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          TEXT NOT NULL REFERENCES accounts(id),
    date                TEXT NOT NULL,          -- YYYY-MM-DD
    amount_native       REAL NOT NULL,          -- >0 into account
    currency            TEXT NOT NULL,
    fx_rate_to_usd      REAL,                   -- usd = native * rate
    amount_usd          REAL,
    merchant_raw        TEXT,
    merchant_norm       TEXT,
    plaid_category      TEXT,                   -- primary/detailed from Plaid PFC, e.g. FOOD_AND_DRINK/GROCERIES
    category            TEXT,                   -- schema category id
    subcategory         TEXT,
    category_source     TEXT,                   -- user|plaid|llm|rule
    category_confidence REAL,
    is_transfer         INTEGER NOT NULL DEFAULT 0,
    transfer_group_id   TEXT,
    is_reimbursement    INTEGER NOT NULL DEFAULT 0,
    needs_review        INTEGER NOT NULL DEFAULT 0,
    review_reason       TEXT,
    pending             INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL,          -- plaid|csv_backfill|<provider name>
    external_id         TEXT,                   -- provider id; NULL for backfill rows without one
    raw_json            TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_id, date);
CREATE INDEX IF NOT EXISTS idx_tx_merchant_norm ON transactions(merchant_norm);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_transfer_group ON transactions(transfer_group_id);

CREATE TABLE IF NOT EXISTS merchant_map (
    merchant_norm   TEXT PRIMARY KEY,
    category        TEXT NOT NULL,
    subcategory     TEXT,
    confidence      REAL NOT NULL,
    source          TEXT NOT NULL,              -- user|plaid|llm  (user entries win permanently)
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    date            TEXT NOT NULL,
    balance_native  REAL NOT NULL,
    currency        TEXT NOT NULL,
    balance_usd     REAL,
    taken_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, date)
);

CREATE TABLE IF NOT EXISTS holdings_snapshots (
    id              INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    date            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    name            TEXT,
    quantity        REAL NOT NULL,
    price           REAL,
    value_usd       REAL,
    UNIQUE (account_id, date, ticker)
);

CREATE TABLE IF NOT EXISTS investment_transactions (
    id              INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    date            TEXT NOT NULL,
    ticker          TEXT,
    type            TEXT NOT NULL,              -- buy|sell|dividend|contribution|withdrawal|fee|other
    quantity        REAL,
    price           REAL,
    amount_usd      REAL NOT NULL,              -- >0 into account
    source          TEXT NOT NULL,
    external_id     TEXT,
    raw_json        TEXT,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS prices (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date    TEXT NOT NULL,
    pair    TEXT NOT NULL,                      -- e.g. EURUSD (1 EUR = rate USD)
    rate    REAL NOT NULL,
    source  TEXT NOT NULL,                      -- ecb|wise
    PRIMARY KEY (date, pair, source)
);

CREATE TABLE IF NOT EXISTS review_log (
    month               TEXT PRIMARY KEY,       -- YYYY-MM
    unclear_count       INTEGER,
    corrections_made    INTEGER,
    notes               TEXT,
    reviewed_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,                  -- plaid:<item>|wise|snapshot|backfill:<inst>|categorize
    started     TEXT NOT NULL,
    finished    TEXT,
    added       INTEGER DEFAULT 0,
    modified    INTEGER DEFAULT 0,
    removed     INTEGER DEFAULT 0,
    error       TEXT
);

-- Spending view: where money went. Excludes transfers; reimbursements net against their category.
CREATE VIEW IF NOT EXISTS v_spending AS
SELECT
    t.id, t.account_id, t.date, substr(t.date, 1, 7) AS month,
    t.category, t.subcategory, t.merchant_norm, t.merchant_raw,
    -- spending is positive when money leaves; reimbursements come in as negative spend
    -t.amount_usd AS spend_usd,
    t.amount_native, t.currency, t.is_reimbursement, t.category_source, t.category_confidence
FROM transactions t
WHERE t.is_transfer = 0
  AND t.pending = 0;

-- Budget view: spending plus savings/investing allocations (the transfer legs the plan counts).
CREATE VIEW IF NOT EXISTS v_budget AS
SELECT
    t.id, t.account_id, t.date, substr(t.date, 1, 7) AS month,
    t.category, t.subcategory, t.merchant_norm,
    -t.amount_usd AS spend_usd,
    t.is_transfer, t.is_reimbursement
FROM transactions t
WHERE t.pending = 0
  AND (t.is_transfer = 0 OR (t.is_transfer = 1 AND t.category IS NOT NULL));

-- Net worth: latest snapshot per account per date; liabilities carry negative balances from ingest.
CREATE VIEW IF NOT EXISTS v_net_worth AS
SELECT
    b.date,
    SUM(b.balance_usd) AS net_worth_usd,
    SUM(CASE WHEN a.type IN ('credit','loan') THEN b.balance_usd ELSE 0 END) AS liabilities_usd,
    SUM(CASE WHEN a.type NOT IN ('credit','loan') THEN b.balance_usd ELSE 0 END) AS assets_usd
FROM balance_snapshots b
JOIN accounts a ON a.id = b.account_id
GROUP BY b.date;

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
