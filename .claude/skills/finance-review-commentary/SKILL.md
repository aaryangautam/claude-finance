---
name: finance-review-commentary
description: Fill the empty "## Commentary" section of a monthly finance review note (Finance/Reviews/YYYY-MM.md) that `uv run finance review YYYY-MM` generated. Use when the user asks for commentary on, or a write-up of, a monthly finance review, or runs /finance-review-commentary YYYY-MM.
---

# Finance review commentary

You add 3–6 bullets of commentary to a review note that the deterministic `finance review` command already wrote.
The tables are ground truth. You explain them; you do not recompute or re-derive them.

## Inputs (and only these)

1. `<vault_finance>/Reviews/YYYY-MM.md`: the review being commented on. Get `vault_finance` from `config.toml [paths]`.
2. `<vault_finance>/Reviews/<previous month>.md`, if it exists, for "what changed vs last month". Read only its tables and frontmatter.

Do **not** open `finance.db`, do not run SQL, do not read `raw_json`, and do not page through transactions. If a number you
want isn't in the tables, say it isn't in the review instead of looking it up. Token cost stays flat.

## Steps

1. If the month argument is missing, ask for it. If the review file doesn't exist, stop and tell the user to run
   `uv run finance review YYYY-MM` first.
2. Read the frontmatter `totals` and the tables: Plan vs actual, Income, Waterfall check, Forecast, Goals.
3. Write 3–6 bullets under `## Commentary`, replacing whatever is between that heading and the end of the file.
   Leave everything above the heading byte-for-byte unchanged. Cover:
   - **What changed vs last month**: the 1–2 biggest movements in totals or categories (skip if there's no previous review).
   - **What's off-plan**: categories over 100% used or pacing over, fixed lines not yet paid, allocations below plan, the
     waterfall check's expected vs observed gap, uncategorized spend, income variance (FX-linked pay, bonuses; it's a number, not an error).
   - **Goals**: anything off track or newly reached; goals marked "not yet trackable" just need a `progress_source`.
   - **One concrete suggestion** tied to a specific line, e.g. "move the $400 above the EF target to brokerage",
     "dining has been above plan for the trailing window; raise its planned amount via the finance-budget-schema-ingestion skill
     or cut back", or "categorize the $55 of uncategorized spend with `finance categorize unclear --month YYYY-MM`".
4. Quote dollar amounts exactly as they appear in the tables. Keep each bullet to one or two sentences.

## Rules

- No investment advice: don't recommend securities, funds, tickers, asset allocation, market timing or buy/sell actions.
  Moving cash along the user's own waterfall (e.g. EF excess → brokerage account) is describing their plan, which is fine.
- Never suggest editing the Budget Schema directly or inventing a category. Plan changes go through the
  `finance-budget-schema-ingestion` skill; category corrections go through `uv run finance categorize correct`.
- Nothing here moves money. Phrase actions as things the user can do, never as something you did.
- Don't rewrite the review with `--force` to "fix" numbers; if the tables look wrong, say so in a bullet.
