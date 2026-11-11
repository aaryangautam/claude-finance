---
name: finance-categorize
description: Categorizes never-seen merchants for the personal finance pipeline. Use when the user runs /finance-categorize, says "categorize the unknown merchants", or after `finance categorize run` reports unknown merchants written to work/uncategorized.json. Reads that file, assigns each merchant exactly one category id from the allowed list, writes work/categorized.json, then runs `finance categorize apply`. Never invents category ids; never touches merchant_map directly; never looks at balances or raw transaction logs.
---

# Finance Categorize

The pipeline's one LLM step, run here instead of via an API key. Cheap, batched, deterministic in shape.

Root: this repository (the directory holding `config.toml` and `finance.db`). Run every command from there.

## Steps

1. If `work/uncategorized.json` doesn't exist or is stale, run `uv run finance categorize run` first (from the root).
2. Read `work/uncategorized.json`. It has `allowed_categories` (the only ids you may use, from the Budget Schema) and `merchants` (merchant_norm, count, total_usd, raw examples, accounts, Plaid's guess).
3. For every merchant, decide **one** `category` id from `allowed_categories`, an optional short `subcategory` (free text, e.g. "coffee", "fuel"), and `confidence` 0–1.
   - Use the raw examples, the account it hit (a foreign-currency Wise direct debit is usually a rent/utility-type expense; a credit-card charge is a card purchase), the amount pattern (recurring same-amount = subscription-shaped), and Plaid's guess as a hint, not the answer.
   - Confidence below the threshold stated in the file's `instructions` puts it in the unclear queue for the user. Use low confidence honestly; a wrong confident guess costs a month of bad numbers, an honest 0.5 costs the user one question.
   - Never invent an id. If nothing fits, pick the closest `other`-type category with low confidence.
4. Write `work/categorized.json`:
   ```json
   {"merchants": [{"merchant_norm": "…", "category": "groceries", "subcategory": "supermarket", "confidence": 0.92}, …]}
   ```
   Every merchant from the input appears exactly once.
5. Run `uv run finance categorize apply`. It validates every id against the schema and rejects the file on any unknown id; fix and re-run rather than editing the DB.
6. Report: how many mapped, how many landed in the unclear queue (`uv run finance categorize unclear`). Don't list every merchant back.

## Never

- Don't read `transactions.raw_json` or balances. Merchant info only.
- Don't write to `merchant_map` directly. User corrections go through `uv run finance categorize correct "<merchant_norm>" <category>`, which is what the monthly review uses.
- Don't edit the Budget Schema to make a merchant fit. If a category is genuinely missing, say so; the schema is changed through the `finance-budget-schema-ingestion` skill.
