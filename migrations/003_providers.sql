-- 003: generic API providers (anything Plaid doesn't cover). Accounts carry (provider, provider_account_id) instead of a
-- provider-specific column; transactions keep the counterparty name a provider reports (payback sender matching).
-- wise_balance_id stays as a dead column: SQLite can't drop a UNIQUE column in place.
ALTER TABLE accounts ADD COLUMN provider TEXT;
ALTER TABLE accounts ADD COLUMN provider_account_id TEXT;
UPDATE accounts SET provider='wise', provider_account_id=wise_balance_id, wise_balance_id=NULL WHERE wise_balance_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider ON accounts(provider, provider_account_id);

ALTER TABLE transactions ADD COLUMN counterparty TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (3);
