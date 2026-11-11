---
name: finance-budget-schema-ingestion
description: Creates and maintains the user's Budget Schema note and financial goal notes in the vault's Finance folder — the category set, planned amounts per category, account roles, the savings waterfall, and goals with targets and priorities. The finance pipeline reads these notes as the allowed category list for transaction categorization and as the plan to compare actuals against. Use whenever the user wants to add, rename, merge, or remove a spending category, change a planned amount, describe how money should flow between accounts, set or update a financial goal (emergency fund target, debt payoff, purchase, net-worth target), adjust goal priority, or describe a rule like "anything over X goes to Y" — even if they don't use the words "budget," "schema," or "goal." Do not use this for looking at actual transactions, categorizing specific purchases, running or debugging the monthly review, or anything involving the SQLite database, Plaid, or Wise — point the user to the `finance` CLI for those.
---

# Finance Budget Schema Ingestion

Maintains two things in the vault's Finance folder (`[paths] vault_finance` in `config.toml`): the single Budget Schema note (`Budget Schema.md`) and one note per financial goal (`Goals/<Goal Title>.md`). The pipeline treats the schema's category list as the only allowed categories when classifying transactions, its account roles as the map for transfer netting, and goals as the plan to check pace against — so precision here directly determines whether the monthly review is useful or noise.

After any write, `uv run finance schema` (from the repo root) parses the note and fails loudly on anything malformed. Run it.

## 1. Check the vault first

Search the Finance folder before drafting anything. Not optional.

- For a category change: read `Budget Schema.md` in full. Edit in place. Never regenerate the whole note from a partial description.
- For a goal: check `Goals/` for an existing note on the same target. Edit rather than duplicate. Check whether it conflicts with or should be linked to another goal (e.g., a purchase goal that competes with the surplus rule).
- Reuse existing category ids and account ids exactly as spelled. A renamed category is a migration for the pipeline's merchant memory — if the user genuinely wants a rename, record the old id in `renamed_from` so the pipeline can remap.

Use whatever vault tools are connected (search, read, create, update/patch), or plain file reads and writes. This skill doesn't assume a specific connector.

## 2. Map what was said, ask about the rest

Fill in everything the user's message already gives. Then ask about gaps, grouped into a couple of natural questions — not a field-by-field interrogation.

For categories, the gaps that matter:

- Which of the four groups it belongs to: `income`, `expenses`, `saving`, `investing`
- `type` — `fixed` (same every month), `variable` (fluctuates), or `allocation` (a planned transfer into savings/investing, counted in the budget view but not the spending view). An allocation category names the account it funds with `account:`.
- `planned` amount in the base currency, and whether that's a target or just an observation of what it's been
- `currency` if it's paid in another currency (e.g. rent or utilities paid in EUR from a Wise balance) — the pipeline stores native currency and converts at the month's rate; the plan is still in the base currency

Keep the category set small: 25 leaf categories at most (the parser enforces it). Every category is an option the categorizer has to choose between; a bloated list makes classification worse and costs more. If the user proposes a category that would only ever hold one merchant, suggest folding it into a parent and using a subcategory instead.

For accounts, every account the pipeline sees needs an id and a `role`. Roles drive transfer matching, so get them right:

- `hub` — exactly one: the checking account pay lands in and transfers leave from
- `savings`, `credit`, `liability` (loans: the hub payment stays the debt expense), `ira`, `brokerage`
- `fx_hub` — Wise balances, one account per currency, ids `wise_<currency>` (e.g. `wise_usd`, `wise_eur`), which is what the Wise sync creates

Transfers between two non-hub accounts aren't matched automatically; tell the user they'll mark those one at a time with `finance categorize transfer --txn <id>`.

For goals, the gaps that matter:

- `goal_type` — `balance-target` (an account should reach X), `debt-payoff` (a liability should reach zero), `purchase` (accumulate X by a date), `contribution-rate` (put X/month into Y), `net-worth`
- `target_amount`, `target_date` (if any — an emergency fund has a target but no date; a purchase has both)
- `measure` — for anything invested, ask explicitly: progress by contributions or by market value? These diverge, and market value rising isn't the user doing anything. Default suggestion: contributions for goals the user funds; market value only for net-worth goals.
- `progress_source` — which account id or allocation category id the pipeline should read to compute progress. If the user says "add that later," leave it null and say so in the body — the pipeline treats a null source as "not yet trackable" and skips it, not error.
- `priority` and `waterfall_position` — where it sits relative to other goals when money is finite

## 3. The waterfall is the rule, not a description — get it exact

Most people fund goals in priority order, with surplus rules moving money between them — e.g. fixed expenses → emergency fund held at a target → all remaining surplus to a brokerage. Write rules like this as ordered steps with explicit amounts and conditions in the schema's `waterfall` block, not as prose in a body section. The monthly review checks them literally ("savings ended the month at $10,400 against a $10,000 target — $400 should have moved; did it?"), so an ambiguous rule produces a wrong check every month.

The planner understands three rule shapes: a baseline step ("fixed expenses and allocations paid"), a hold (`"<account id> held at target"` with `target:`) and a sink (`"all remaining surplus → <account id>"`, at most one). Phrase rules that way, using account ids exactly; anything else makes `finance forecast` and `finance review` fail with a SchemaError.

When a new goal is added, ask where it slots into the waterfall. A new purchase goal almost always takes money from the surplus step — make the user say so.

## 4. Income is a planned gross breakdown, not what the bank sees

The pipeline only observes net pay deposits on the hub (matched by `[income] payroll_patterns` in `config.toml`). The schema stores the planned gross breakdown from a pay stub (salary, allowances, bonus) and pre-deductions (tax, retirement contributions, benefits) as a static guideline. The monthly variance between planned net and observed net is expected — overtime, bonuses, pay that moves with an exchange rate — and the review shows it as a number, not an error. Don't make the schema track pay monthly.

On first run, ask the user for one recent pay stub (or the numbers from it) to seed these lines. Don't ask again unless pay structure changes.

## 5. Draft before writing

Show the full frontmatter (for the schema, the changed block plus enough context to place it; for a goal, the whole note) before writing anything. Wait for confirmation. This is intake, not analysis — keep it tight.

## 6. Write and file

- `Budget Schema.md` — one note, edited in place.
- `Goals/<Goal Title>.md` — one note per goal, title case.
- Goal body always includes: `## Why this goal`, `## Funding rule`, `## On-track criteria`, `## Review Log` (empty table, headers only).
- After a goal write, check whether `Budget Schema.md`'s `waterfall` block needs updating, and whether any other goal's `related_goals` should point at it.

### Budget Schema — frontmatter

```yaml
base_currency: USD
pay_schedule: "1st and 15th"
income_planned:            # gross guideline from a pay stub; static, not monthly
  - {id: salary, planned: 0}
  - {id: bonus, planned: 0, note: "annual, averaged monthly"}
pre_deductions:            # never hit the bank; shown for the gross→net bridge only
  - {id: tax, planned: 0}
  - {id: retirement_401k, planned: 0, note: "not tracked as an account"}
categories:
  - {id: income_pay, group: income, type: fixed, planned: 0}
  - id: housing_rent
    group: expenses
    type: fixed
    planned: 0
    currency: USD
    renamed_from: null
  # ... one entry per leaf category; 25 at most
  - {id: saving_emergency_fund, group: saving, type: allocation, planned: 0, account: savings}
  - {id: investing_brokerage, group: investing, type: allocation, planned: 0, account: brokerage}
accounts:                  # ids the pipeline uses; roles drive transfer netting
  - {id: checking, institution: My Bank, role: hub, currency: USD}
  - {id: savings, institution: My Bank, role: savings, currency: USD, goal: Emergency Fund}
  - {id: car_loan, institution: My Bank, role: liability, currency: USD}
  - {id: card_everyday, institution: My Card Issuer, role: credit, currency: USD}
  - {id: roth_ira, institution: My Broker, role: ira, currency: USD}
  - {id: brokerage, institution: My Broker, role: brokerage, currency: USD}
  - {id: wise_usd, institution: Wise, role: fx_hub, currency: USD}   # only if you use Wise
  - {id: wise_eur, institution: Wise, role: fx_hub, currency: EUR}
waterfall:                 # ordered; the review checks these literally
  - step: 1
    rule: "fixed expenses and allocations paid"
  - step: 2
    rule: "savings held at target"
    target: 10000
    goal: Emergency Fund
  - step: 3
    rule: "all remaining surplus → brokerage"
reimbursements:            # documentation; senders themselves live in config.toml [reimbursements]
  - {category: phone, from: "roommates via Wise", treatment: net_against_expense}
```

### Goal note — frontmatter

```yaml
status: active | paused | achieved | dropped
goal_type: balance-target | debt-payoff | purchase | contribution-rate | net-worth
target_amount: <number>
target_date: <YYYY-MM-DD or null>
measure: balance | contributions | market_value
progress_source: <account id | allocation category id | null>
priority: primary | secondary | maintenance
waterfall_position: <step number or null>
related_goals: []
created: <YYYY-MM-DD>
```

## First-run seed

If the user has an existing budget (a spreadsheet, a list), it's a plan, not history. Propose this structure as the draft, using their numbers; the user confirms every one:

- income: the gross lines from the pay stub, plus `income_pay` (observed net pay) and, if wanted, `income_other` (tax refunds etc.)
- expenses (fixed): rent/mortgage, utilities, phone, insurance, loan payments (`debt_<name>`), recurring subscriptions. Keep the user's own names for things.
- expenses (variable): if the user has a single "credit card spending" lump, replace it — propose a starter set the categorizer can actually use: groceries, dining, transport, travel, shopping_household, subscriptions, health, entertainment, gifts, cash_atm, other. Trim or merge with the user; this is where most of the schema's value lives.
- saving (allocation): emergency fund, any sinking funds
- investing (allocation): retirement account contributions, taxable brokerage

Then offer two starter goals: `Emergency Fund` (balance-target, measure: balance, progress_source: the savings account id) and one investing or debt-payoff goal that matches what the user said they care about.

## Staying in scope

This skill reads and writes the Budget Schema note and goal notes. If the user asks about actual spending, a specific transaction, the unclear queue, Plaid/Wise connections, or the review output, point them to the `finance` CLI (`finance plan`, `finance categorize unclear`, `finance review`). Investment strategy content (sizing rules, thesis criteria) belongs to the `finance-investment-strategy-ingestion` skill — the only overlap is the waterfall's surplus step, which this skill owns. Nothing here is financial advice: record the user's plan, don't tell them what it should be beyond flagging contradictions and unrealistic arithmetic.
