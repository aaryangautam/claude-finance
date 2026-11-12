# claude-finance

A local-first personal finance system run from [Claude Code](https://claude.com/claude-code).

Plaid pulls your transactions into a SQLite file on your own machine. Sources Plaid doesn't cover (Wise is bundled; any
other API takes a small provider class) plug in alongside it. Deterministic Python then nets
transfers between your own accounts, categorizes what it already knows and aggregates spending against a plan. Claude
handles only the parts that need judgment: categorizing merchants it hasn't seen before, interviewing you to build the
budget plan, and writing a short commentary on each monthly review. It reads small handoff files and the review
tables, never your raw transaction log.

> **Not financial advice.** This is a bookkeeping and planning tool. It records *your* plan and shows how your actual
> numbers compare to it. Nothing in this repo, including the bundled Claude skills, recommends securities, trades or
> asset allocations, and it isn't a substitute for a licensed professional. You alone are responsible for your
> financial decisions and for checking the numbers it produces.

**Read-only by design.** No code path can move money or place a trade. It only requests Plaid's read products
(`transactions`, with `investments` and `liabilities` optional), and provider tokens should be created read-only. The
provider contract has no method that could write anything back.

## How it fits together

```
Plaid + providers ──sync──▶ finance.db (SQLite: ground truth)
                          │  matcher → normalize → rules → merchant_map      (deterministic, `finance categorize run`)
                          ├──▶ work/uncategorized.json ──/finance-categorize──▶ work/categorized.json ──apply──┘
                          │
vault/Finance/  ◀── plan ─┤  Budget Schema.md   categories, planned amounts, account roles, savings waterfall
 (Obsidian or a           │  Goals/*.md         targets, priorities, progress source
  plain folder)           │  Reviews/YYYY-MM.md `finance review` writes the tables, /finance-review-commentary the notes
                          └──▶ localhost dashboard (`finance app`): Plaid Link, account mapping, plan vs actual, net worth
```

- **`finance.db`** is the only source of truth for what happened. `v_spending`, `v_budget` and `v_net_worth` are the
  views worth querying.
- **The vault Finance folder** holds the plan: what *should* happen. It's Markdown with YAML frontmatter, so an
  Obsidian vault works well, but any folder will do.
- **Claude Code skills** in `.claude/skills/` load automatically when you run `claude` in this repo:

  | Skill | What it does |
  |---|---|
  | `/finance-categorize` | Assigns schema categories to unknown merchants from `work/uncategorized.json`, then runs `finance categorize apply` |
  | `/finance-budget-schema-ingestion` | Interviews you to create or edit the Budget Schema and goal notes |
  | `/finance-investment-strategy-ingestion` | Records your own investing rules in `Investment Strategy.md` (it records rules and never recommends trades) |
  | `/finance-review-commentary YYYY-MM` | Adds 3–6 bullets of commentary to a generated monthly review, working from its tables only |

  To use the two vault skills from outside this repo (e.g. in a Claude session opened on your vault), copy their
  folders into `~/.claude/skills/`.

## Before you start: Plaid access

**Plaid.** You need a Plaid developer account ([dashboard.plaid.com](https://dashboard.plaid.com)).

- **Sandbox** is free and instant. It serves fake institutions and data, so use it to try the system end to end
  (`finance link-sandbox`, below).
- **Real bank data needs Production access.** Plaid retired its free Development environment in 2024. To reach real
  institutions you apply for Production in the dashboard: company profile, use-case questionnaire, security
  questions. Approval can take days. Plaid may treat a solo "personal use" app differently from a business, and its
  pricing, free tiers and per-Item or per-call fees change over time, so check Plaid's current terms before relying
  on it. Some large banks also need extra OAuth registration on your Plaid account before they appear in Link.
- Coverage is US/Canada/UK/EU institutions, depending on your Plaid account. This repo creates Link tokens for
  `US` only (`finance/plaid_client.py`); change `country_codes` if you need others.
- Plaid returns about 24 months of transactions at most, often less. Older history comes from the optional CSV
  importers (`finance backfill`).

Plaid is the only required source. Everything else is optional: a [provider](#adding-a-data-source) for an API Plaid
doesn't cover, or a [CSV import](#csv-backfill-optional) for anything without an API.

## Bundled provider: Wise (optional)

The repo includes a Wise provider for multi-currency balances, e.g. rent or bills paid in another currency from Wise
balances that you top up from your US checking account. To enable it:

```toml
# config.toml
[providers.wise]
token_env = "WISE_TOKEN"
days = 90
```

- Create a **personal API token** (Wise → Settings → API tokens), read-only, and put it in `.env` as `WISE_TOKEN`.
- **EU/EEA and UK accounts (PSD2):** Wise puts balance statements behind Strong Customer Authentication. The client
  answers the `x-2fa-approval` challenge if you generate an RSA key pair, upload the public key in Wise's API token
  settings and set `WISE_PRIVATE_KEY_PATH` in `.env`. For some EU/UK residents Wise limits what personal tokens can
  read at all. If statements come back 403 even with a key, download statement CSVs from Wise and import them with
  `finance backfill wise <file>.csv`.
- Wise accounts get ids `wise_<currency>` when your Budget Schema has them (role `fx_hub`); otherwise they show up as
  provisional `wise:<balance id>` rows to map once.

## Setup

Requires macOS or Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/) and
[Claude Code](https://claude.com/claude-code).

```bash
git clone <this repo> claude-finance && cd claude-finance
uv sync
cp config.example.toml config.toml        # paths, payroll/transfer descriptor patterns, payback senders
cp .env.example .env                      # PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=sandbox (+ any provider tokens)
uv run finance init                       # creates finance.db, vault/Finance/ with a starter Budget Schema
```

`config.toml`, `.env`, `finance.db`, `vault/`, `work/` and `exports/` are all gitignored. Your data never leaves
your machine except as API calls to Plaid, the providers you enable and the ECB rate service (frankfurter.app).

### 1. Write your plan

Open `claude` in this directory and run `/finance-budget-schema-ingestion`. It interviews you about income,
categories (25 at most), account roles and the savings waterfall, then edits `vault/Finance/Budget Schema.md`. If you
prefer, edit the starter file by hand. Either way, check it with:

```bash
uv run finance schema
```

To keep the plan in Obsidian, point `[paths] vault_finance` in `config.toml` at your vault's Finance folder.

### 2. Link accounts

```bash
uv run finance app            # http://127.0.0.1:8765/accounts
```

Choose a label from the dropdown (`[link] institutions` in `config.toml`; the label names the Item and its
`PLAID_TOKEN_<LABEL>` key in `.env`) and click **Link**. Plaid Link lets you search for any institution it supports.
New accounts show up as provisional `plaid:<id>` rows. Map each one to an account id from your Budget Schema, on the
page or with `uv run finance accounts map plaid:<id> checking`.

In sandbox you can skip the browser:

```bash
uv run finance link-sandbox   # links Plaid's "First Platypus Bank" test Item
uv run finance accounts list
```

### 3. First sync and categorization

```bash
uv run finance sync all                   # Plaid + every enabled provider (or: sync plaid, sync <provider>)
uv run finance categorize run             # transfers, rules, known merchants; unknowns → work/uncategorized.json
```

Then, in Claude Code:

```
/finance-categorize
```

Claude assigns a category to each unknown merchant and runs `finance categorize apply`. Merchants it isn't sure about
(confidence below `unclear_threshold`) wait for you:

```bash
uv run finance categorize unclear
uv run finance categorize correct "Merchant Name" groceries      # permanent, per merchant
uv run finance categorize correct --txn 123 travel               # one transaction (Zelle/Venmo, split paybacks)
uv run finance categorize transfer --txn 456                     # a move between your own accounts
```

Expect to spend a while on the first pass, tuning `config.toml` (`[income] payroll_patterns`,
`[matcher.transfer_patterns]`) until your pay is recognized and card payments stop showing as spending. After
that, each month needs only a handful of decisions.

### 4. Plan, forecast, review

```bash
uv run finance plan                       # this month: planned vs actual per category, pace, paid flags
uv run finance forecast --months 6        # cash flow, projected hub balance, waterfall, goal projections
uv run finance review 2026-10             # writes vault/Finance/Reviews/2026-10.md (tables only)
```

Then run `/finance-review-commentary 2026-10` in Claude Code.

### 5. Schedule it

```bash
uv run finance install-schedule --dry-run # show the launchd jobs for this checkout
uv run finance install-schedule           # macOS: daily 06:30 sync + categorize run, weekly Sunday snapshot
uv run finance install-schedule --cron    # Linux: print equivalent crontab lines
```

Times and the job label prefix live in `config.toml [schedule]`, and logs go to `~/Library/Logs/finance-*.log`. The
scheduled jobs never call an LLM. Unknown merchants pile up in `work/uncategorized.json` until you next run
`/finance-categorize`. `finance install-schedule --uninstall` removes the jobs.

## Money-flow model (read this before linking)

The transfer matcher works from the account `role`s in the Budget Schema and assumes a **single-hub** setup:

- **Exactly one `hub`** account: the checking account your pay lands in and transfers leave from. The schema
  parser rejects zero or several.
- Every other account is matched **against the hub**:
  - `credit`, `savings`, `fx_hub`: both legs hidden as an internal transfer.
  - `ira`, `brokerage`: both legs hidden, and the hub leg gets the allocation category whose `account:` names that
    account, so the budget view shows that the contribution happened. Deposits Plaid reports only as investment
    contributions are matched too.
  - `liability`: the hub payment stays visible as the `debt_*` expense, and only the loan-side receipt is hidden.
    Same-day principal and interest lines are summed first.
- **`fx_hub`** accounts (multi-currency balances such as Wise, one account per currency) also net with each other
  (currency conversions), with an allowance of 1.5% for conversion fees.
- **Not handled automatically:** transfers between two non-hub accounts (a card paid from savings, brokerage → IRA),
  two checking accounts, or a partner's joint account. Mark those with `finance categorize transfer --txn <id>`, or
  give the second checking account the `savings` role if money only moves hub ↔ it.
- Hub outflows whose description matches `[matcher.transfer_patterns]` but find no counterpart go to the review queue
  instead of being counted as spending.

Amounts are converted to **USD** internally (`amount_usd`, ECB rates). A non-USD household can use the system with
care, but the base-currency handling hasn't been tested outside USD.

## Adding a data source

Plaid covers most US banks, cards and brokerages, but not everything: non-US banks, multi-currency or crypto accounts,
pension portals, an employer's stock plan. Anything with an API you can read plugs in as a **provider**: a class that
turns that API's responses into plain records. The rest of the system treats its rows like Plaid's. Transfers are
matched by account role, categorization runs on the description, FX uses the provider's own rate when it reports one,
and the weekly snapshot records its balances.

1. Write the class. It can live anywhere importable from the repo root, for example `my_providers/neobank.py`:

   ```python
   import requests
   from finance.providers import Provider, ProviderAccount, ProviderBalance, ProviderTransaction

   class NeobankProvider(Provider):
       institution = "Neobank"

       def _get(self, path, **params):
           r = requests.get(f"https://api.neobank.example{path}", params=params, timeout=30,
                            headers={"Authorization": f"Bearer {self.secret('token_env')}"})
           r.raise_for_status()
           return r.json()

       def accounts(self):
           return [ProviderAccount(external_id=a["id"], name=a["name"], type="checking", currency=a["currency"])
                   for a in self._get("/accounts")]

       def transactions(self, start, end):
           for a in self._get("/accounts"):
               for t in self._get(f"/accounts/{a['id']}/transactions", since=start.isoformat(), until=end.isoformat()):
                   yield ProviderTransaction(
                       external_id=t["id"], account_external_id=a["id"], date=t["booked_on"],
                       amount=t["amount"],              # signed: > 0 = money INTO the account
                       currency=a["currency"], description=t["description"],
                       counterparty=t.get("counterparty_name"),
                   )

       def balances(self):
           return [ProviderBalance(a["id"], a["balance"], a["currency"]) for a in self._get("/accounts")]
   ```

2. Enable it in `config.toml`. The table name becomes the source tag and the prefix of provisional account ids, and
   every key other than `module` and `enabled` is available as `self.settings`:

   ```toml
   [providers.neobank]
   module = "my_providers.neobank:NeobankProvider"
   token_env = "NEOBANK_TOKEN"     # the token itself goes in .env
   days = 60                       # default window for `finance sync neobank`
   ```

3. Run `uv run finance sync neobank`, then map the new `neobank:<id>` accounts to Budget Schema ids
   (`finance accounts map neobank:<id> <schema id>`, or the Accounts page). A provider can skip that step by setting
   `suggested_id` on an account whose id the schema already uses.

The contract, from `finance/providers/base.py`:

| Method | Returns | Notes |
|---|---|---|
| `accounts()` | `ProviderAccount(external_id, name, type, currency, suggested_id=None)` | `external_id` must be stable |
| `transactions(start, end)` | `ProviderTransaction(external_id, account_external_id, date, amount, currency, description, counterparty=None, amount_usd=None, fx_rate_to_usd=None, pending=False, raw={})` | Re-syncing the same `external_id` updates the row. Report posted transactions. Include `amount_usd`/`fx_rate_to_usd` only when the API knows the real conversion. |
| `balances()` | `ProviderBalance(account_external_id, balance, currency)` | Liabilities negative |
| `holdings()` *(optional)* | `ProviderHolding(account_external_id, ticker, quantity, price, value_usd, name=None)` | Defaults to none |

A provider never touches the database, so it's easy to test with canned responses. `tests/fake_provider.py` is a
complete example. `finance/providers/wise.py` is a real one, including request signing. If something has no API at
all, use a CSV adapter instead.

## CSV backfill (optional)

```bash
uv run finance backfill generic exports/bank.csv --account checking --dry-run
```

`generic` reads a Date / Description / signed Amount file (negative = money out). The named adapters (`usaa`,
`chase`, `amex`, `robinhood`, `wise`) encode those institutions' export layouts and sign conventions. Adding one is a
~20-line module in `finance/ingest/csv_backfill/` registered in `PARSERS`. Imported rows are deduplicated against Plaid
rows. Run `finance categorize run --rematch` afterwards so transfers are matched across the full history.

## Development

```bash
uv run pytest
```

`CLAUDE.md` documents the data model, sign convention, views and standing rules for Claude Code sessions in this repo.
Schema changes go in a new `migrations/NNN_*.sql` file. Never edit `001_init.sql`.

## License

[MIT](LICENSE).
