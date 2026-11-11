# Finance (data layer)

Local-first personal finance. Plaid (+ optional Wise) → `finance.db` (SQLite, ground truth). Deterministic scripts do
transfer netting, categorization, aggregation. Claude reasons over the notes in the vault Finance folder
(`[paths] vault_finance` in `config.toml`: Budget Schema, Goals, Investment Strategy, Reviews), never over the raw log.

## Standing rules

- **Read-only everywhere.** No code path can move money or place a trade. Keep it that way.
- **Fail loudly.** Missing .env key, unreachable Plaid/Wise, malformed Budget Schema → raise. No silent fallbacks.
- **Token cost stays flat.** For Q&A, run SQL over the views and explain the aggregates. Don't read `raw_json`.
  Don't page through `transactions`; `GROUP BY` first.
- **Nothing lives only in chat.** A correction goes through `finance categorize correct`; a plan change goes through
  the `finance-budget-schema-ingestion` skill; a review goes to `Finance/Reviews/YYYY-MM.md`.
- The Budget Schema's category ids are the only allowed categories. Never invent one.
- No investment advice. Describe the user's own plan and numbers; never recommend securities or trades.

## Commands (run from this directory)

```
uv run finance init                      # migrate db, create the vault folder + starter schema, validate notes
uv run finance status                    # items, accounts, counts, last runs
uv run finance schema                    # parsed Budget Schema
uv run finance app                       # localhost:8765 — Plaid Link, account mapping, dashboard, unclear queue
uv run finance link-sandbox              # PLAID_ENV=sandbox: link a test Item without the browser
uv run finance sync all|plaid|wise       # ingest (plaid: --item <label>; wise: --days 90; `all` skips Wise without a token)
uv run finance snapshot                  # balances + holdings (launchd runs this weekly)
uv run finance categorize run            # matcher → normalize → rules → merchant_map → work/uncategorized.json
   /finance-categorize                   # (Claude Code skill) → work/categorized.json
uv run finance categorize apply          # validate + write back
uv run finance categorize unclear [--month YYYY-MM]
uv run finance categorize correct "<Merchant Norm>" <category_id>   # user entry, wins permanently
uv run finance categorize correct --txn <id> <category_id>          # one transaction only (Zelle, split paybacks)
uv run finance categorize transfer --txn <id>                       # one-off move between own accounts (survives --rematch)
uv run finance accounts map plaid:<id> checking                     # once per account after first link
uv run finance backfill generic exports/bank.csv --account checking --dry-run   # optional, history beyond Plaid's window
uv run finance plan [--month YYYY-MM]    # plan vs actual per category (EUR at month ECB rate, pace, paid flags)
uv run finance forecast [--months 6]     # cash flow (blend), projected hub balance, waterfall, goal projections
uv run finance review YYYY-MM [--force]  # writes Finance/Reviews/YYYY-MM.md + review_log row; refuses to overwrite
   /finance-review-commentary YYYY-MM    # (Claude Code skill) fills ## Commentary from the review's tables only
uv run finance install-schedule [--dry-run|--uninstall|--cron]   # daily sync+categorize, weekly snapshot
uv run pytest
```

## Data model (sign convention: amount > 0 = money INTO the account)

- `accounts` — id is the schema id (`checking`, `card_everyday`, `wise_eur`…) once mapped; `plaid:<id>` until then. `role` from the schema drives the matcher.
- `transactions` — one row per posted line. `amount_usd` after FX. `is_transfer` rows are internal moves (both legs share `transfer_group_id`).
  `category` is a schema id; `category_source` ∈ user|rule|llm|plaid; `needs_review` + `review_reason` = unmatched transfer legs.
- `merchant_map` — `merchant_norm → category`. `source='user'` rows are never overwritten.
- `balance_snapshots` / `holdings_snapshots` — weekly; liabilities stored negative. Net worth history exists only from the first snapshot on.
- `investment_transactions`, `prices`, `fx_rates` (`EURUSD`, source ecb|wise), `review_log`, `plaid_items`, `sync_runs`.

Views:
- `v_spending` — where money went. Excludes transfers; `spend_usd` positive = outflow; reimbursements appear as negative spend in their category.
- `v_budget` — spending + allocation legs (`investing_*`, `saving_*` on the hub outflow). "Did the plan happen."
- `v_net_worth` — per snapshot date: assets, liabilities, net.

Open read-only for Q&A: `sqlite3 "file:finance.db?mode=ro"` or `python -c "from finance.db import open_db; ..."` with `ro=True`.

## Money-flow rules the matcher encodes

Exactly one `hub` account (the checking account pay lands in). Every pair below has the hub on one side; transfers
between two non-hub accounts are not matched (`finance categorize transfer --txn <id>` marks them one at a time).
hub → `credit` / `savings` / `fx_hub` accounts: netted (both legs hidden). `fx_hub` accounts (Wise balances) also net with each other.
hub → `ira` / `brokerage`: netted, hub leg carries the allocation category (from the schema category's `account:` field).
hub → `liability`: **not** netted — the hub leg is the `debt_*` expense; only the loan-side receipt is hidden.
Wise legs use Wise's own rate (recorded in `fx_rates` as source=wise); everything else ECB via frankfurter.
Wise payments from senders in `config.toml [reimbursements] senders` → `is_reimbursement=1`, category `reimbursement_category`, never income.
Zelle into the hub from senders in `config.toml [reimbursements.zelle]` → `is_reimbursement=1`, netted against the mapped category;
an empty mapping leaves the category NULL with `needs_review=1` for a per-transaction pick.
Merchants matching `[categorizer] per_transaction_patterns` (Zelle) are never merchant-mapped or sent to the LLM; they wait in
`categorize unclear` by transaction id. `category_source='user_txn'` marks a one-transaction correction; merchant-level corrections skip it.

## Example Q&A queries

```sql
-- spend by category this month
SELECT category, ROUND(SUM(spend_usd),2) FROM v_spending WHERE month='2026-09' GROUP BY 1 ORDER BY 2 DESC;
-- planned vs actual comes from joining that with the Budget Schema (finance.vault.load_budget_schema)
-- month over month
SELECT month, ROUND(SUM(spend_usd),2) FROM v_spending WHERE category='groceries' GROUP BY 1 ORDER BY 1 DESC LIMIT 6;
-- emergency fund
SELECT date, balance_usd FROM balance_snapshots WHERE account_id='savings' ORDER BY date DESC LIMIT 8;
-- observed net income
SELECT substr(date,1,7) m, SUM(amount_usd) FROM transactions WHERE category='income_pay' GROUP BY 1 ORDER BY 1 DESC;
-- unmatched transfer legs
SELECT date, account_id, amount_usd, merchant_raw, review_reason FROM transactions WHERE needs_review=1;
```

## Layout

`finance/` package: `config.py` `db.py` `vault.py` `plaid_client.py` `wise_client.py` `fx.py` `accounts.py` `linking.py` `snapshot.py` `schedule.py` `cli.py`;
`ingest/plaid_sync.py` `ingest/wise_sync.py` `ingest/csv_backfill/`; `pipeline/normalize.py` `matcher.py` `categorize.py`; `app/`;
`planner/month.py` (plan vs actual, used by `/dashboard`) `forecast.py` (blend: planned until 3 complete months of actuals,
then trailing 3-month avg) `goals.py` (waterfall simulation + goal projections; `progress_source` = account id or allocation
category id; null = "not yet trackable") `review.py`.
`migrations/*.sql` applied in order by `db.migrate`. Schema changes = new migration file, never edit 001.
`work/` holds the categorization handoff files; `exports/` the one-time CSVs. Gitignored: those, `.env`, `config.toml`, `vault/` and the db.
Skills live in `.claude/skills/`: `finance-categorize`, `finance-review-commentary`, `finance-budget-schema-ingestion`, `finance-investment-strategy-ingestion`.
