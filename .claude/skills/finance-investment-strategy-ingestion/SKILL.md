---
name: finance-investment-strategy-ingestion
description: Builds and maintains the user's living Investment Strategy note in the vault's Finance folder — the objective, the core vs. aggressive split, position-sizing and concentration limits, thesis criteria, exit rules, risk tolerance, and a free-text briefing a later alignment check reasons over. Use whenever the user talks about how they invest or want to invest, states a rule or limit for their brokerage, describes what makes an investment attractive or a dealbreaker to them, wants to change allocation, cadence, or limits, or says they've refined or formalized their strategy — even if they don't say "strategy" or "update the note." Do not use this to evaluate a specific stock or idea, run performance analysis, or look at holdings. This skill documents the user's own rules; it does not recommend what to buy or sell.
---

# Finance Investment Strategy Ingestion

Maintains one note: `Investment Strategy.md` in the vault's Finance folder (`[paths] vault_finance` in `config.toml`). Two later readers depend on it: an alignment check (the user brings an idea → Claude judges fit against these rules) and a retrospective (did the strategy get followed, and did following it beat the benchmark). Both are only as sharp as this note. Most people start with formulations, not finished rules — this skill's job is to turn formulations into rules precise enough to be checked, over several conversations, without inventing precision the user hasn't confirmed.

**Not financial advice.** This skill records the user's own rules. It never suggests what to buy, sell, or hold, and never says whether a rule is a good one beyond flagging when two rules contradict each other.

## 1. Check the vault first

Read `Investment Strategy.md` in full before touching anything (if it doesn't exist, this is a first run). Every edit is in place. Every rule change gets a dated entry in `## Change Log` with the user's stated reason — the retrospective correlates strategy changes with outcomes, so an undated change is lost information.

Also read `Budget Schema.md`'s `waterfall` block: the amount flowing into the brokerage is owned there (by the `finance-budget-schema-ingestion` skill), not here. This note describes what happens to money once it's in the account, not how much arrives.

## 2. Map what was said, ask about the rest

Fill in what the user gave. Then ask about gaps in a couple of natural questions. The shape of the note:

**Objective** — one sentence in the user's words (e.g. "maximize market value from a roughly fixed monthly contribution", "income in retirement", "beat the index with a small satellite sleeve"). Once confirmed, don't re-derive it each conversation.

**Accounts and roles** — many people run a passive core and a discretionary sleeve. Get the user to state their split explicitly, using the account ids from the Budget Schema:

- `core` — e.g. a retirement account with recurring index/ETF buys. Which tickers, what weights, what cadence, and whether the user ever touches it manually.
- `aggressive` (or whatever the user calls it) — e.g. a taxable brokerage funded by budget surplus, discretionary. This is where the checkable rules live.

If the user has only one account or one style, use one block and drop the other.

**Discretionary rules** — the fields that make an idea checkable:

- `max_position_pct` — largest single position as a share of the account
- `max_sector_pct` or equivalent concentration limit
- `min_holding_period` / whether the user trades or holds
- `instruments` — equities only? ETFs? options, crypto, anything excluded outright?
- `thesis_criteria` — what has to be true before the user buys (growth, valuation, a catalyst, a theme, research depth — get concrete)
- `exit_rules` — what triggers a sell: thesis broken, target hit, drawdown limit, rebalancing
- `risk_tolerance` — the drawdown the user can sit through without changing behavior. Ask as a number, not an adjective.

**Benchmark** — what the user wants to be compared against (a broad index fund is the usual choice). Record it; don't reopen it.

**Review cadence** — how often the user wants the retrospective run (monthly with the budget review, or quarterly).

Don't ask about fields the user's stated approach makes irrelevant. If they say "I never sell, ever," don't build an elaborate exit-rules block.

## 3. The free-text briefing is the point — push hard here

`notes_for_alignment` is what the alignment check reasons over. A restatement of the numeric limits wastes it. Ask two things directly:

1. **What would make you reject an idea that looks attractive on paper?** — dealbreakers, red flags, categories the user has been burned by, things that don't fit their conviction style. Get examples, not adjectives.
2. **What would make you say yes to something that breaks a numeric rule?** — override signals. Is there a conviction level or situation where the user would knowingly exceed `max_position_pct`? If so, name the condition. If not, say the limits are hard.

Also surface things users don't volunteer: do they want overlap with core holdings flagged (buying a tech fund in the brokerage when the core is already tech-heavy)? Does correlation with existing positions matter to them? Write these as "deprioritize / flag" vs. "reject" so the checker knows which is which.

Write the block as a briefing to someone who will judge ideas on the user's behalf — reasoning and priority order, not a rule dump.

## 4. Formulations become rules incrementally — don't fake certainty

When a rule is genuinely undecided, write it as `tentative: true` with the user's current lean, rather than leaving it blank or forcing a number. The alignment check treats tentative rules as "flag, don't fail." Each later conversation can firm one up. A note full of confirmed rules is the goal; a note full of guessed rules is worse than an empty one.

## 5. Draft before writing

Show the changed frontmatter block and the `notes_for_alignment` text before writing. For a Change Log entry, show the entry. Wait for confirmation.

## 6. Write and file

- `Investment Strategy.md` — one note, edited in place.
- Body sections, always present: `## Objective`, `## Core`, `## Aggressive` (or the user's names for them), `## What I've learned` (the user's own retrospective takeaways, added over time — not the pipeline's output), `## Change Log` (dated table: date, what changed, why).

### Note schema — frontmatter

```yaml
objective: "<one sentence, the user's words>"
benchmark: <ticker>
review_cadence: monthly | quarterly
core:
  account: roth_ira                     # an account id from the Budget Schema
  holdings: [{ticker: "", weight_pct: 0}]
  cadence: "recurring, automated"
  manual_changes: never | rebalance_only | occasional
aggressive:
  account: brokerage
  funding: "budget waterfall surplus — see Budget Schema"
  instruments: [equities, etfs]        # list what's allowed; anything absent is excluded
  max_position_pct: {value: 0, tentative: false}
  max_sector_pct: {value: 0, tentative: false}
  min_holding_period: {value: "", tentative: false}
  risk_tolerance_drawdown_pct: {value: 0, tentative: false}
  thesis_criteria: []                  # short, concrete, each independently checkable
  exit_rules: []
notes_for_alignment: >
  Free-text briefing: dealbreakers, override signals and their conditions,
  overlap/correlation preferences, what "conviction" means to this user,
  scoring modifiers (flag vs reject). Written for a judge, not a filter.
```

## Staying in scope

This skill only reads and writes the Investment Strategy note. If the user brings a specific ticker or idea to evaluate, asks how a position has performed, or wants the benchmark comparison, say that's outside this skill — don't evaluate it here, even informally.
