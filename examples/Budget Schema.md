---
# Starter Budget Schema. Every number here is a placeholder: replace them with your own plan, or run the
# finance-budget-schema-ingestion skill in Claude Code and let it interview you. `uv run finance schema` validates it.
base_currency: USD
pay_schedule: "1st and 15th"
income_planned:            # gross guideline from a pay stub; static, not monthly
  - {id: salary, planned: 6000}
pre_deductions:            # never hit the bank; shown for the gross→net bridge only
  - {id: tax, planned: 1200}
  - {id: retirement_401k, planned: 300}
categories:
  - {id: income_pay, group: income, type: fixed, planned: 4500}
  - {id: income_other, group: income, type: variable, planned: 0}
  - {id: housing_rent, group: expenses, type: fixed, planned: 1600}
  - {id: utilities, group: expenses, type: fixed, planned: 150}
  - {id: phone, group: expenses, type: fixed, planned: 60}
  - {id: insurance, group: expenses, type: fixed, planned: 120}
  - {id: debt_student_loan, group: expenses, type: fixed, planned: 250}
  - {id: subscriptions, group: expenses, type: fixed, planned: 40}
  - {id: groceries, group: expenses, type: variable, planned: 450}
  - {id: dining, group: expenses, type: variable, planned: 250}
  - {id: transport, group: expenses, type: variable, planned: 150}
  - {id: travel, group: expenses, type: variable, planned: 150}
  - {id: shopping_household, group: expenses, type: variable, planned: 150}
  - {id: health, group: expenses, type: variable, planned: 50}
  - {id: entertainment, group: expenses, type: variable, planned: 80}
  - {id: gifts, group: expenses, type: variable, planned: 40}
  - {id: cash_atm, group: expenses, type: variable, planned: 40}
  - {id: other, group: expenses, type: variable, planned: 80}
  - {id: saving_emergency_fund, group: saving, type: allocation, planned: 300, account: savings}
  - {id: investing_roth_ira, group: investing, type: allocation, planned: 500, account: roth_ira}
accounts:                  # ids the pipeline uses; roles drive transfer netting. Exactly one hub.
  - {id: checking, institution: My Bank, role: hub, currency: USD}
  - {id: savings, institution: My Bank, role: savings, currency: USD, goal: Emergency Fund}
  - {id: card_everyday, institution: My Card Issuer, role: credit, currency: USD}
  - {id: student_loan, institution: My Lender, role: liability, currency: USD}
  - {id: roth_ira, institution: My Broker, role: ira, currency: USD}
  - {id: brokerage, institution: My Broker, role: brokerage, currency: USD}
  # Wise users: one fx_hub account per balance currency, ids wise_<currency>.
  # - {id: wise_usd, institution: Wise, role: fx_hub, currency: USD}
  # - {id: wise_eur, institution: Wise, role: fx_hub, currency: EUR}
waterfall:                 # ordered; the monthly review checks these literally
  - {step: 1, rule: "fixed expenses and allocations paid"}
  - {step: 2, rule: "savings held at target", target: 10000, goal: Emergency Fund}
  - {step: 3, rule: "all remaining surplus → brokerage"}
reimbursements: []
---

# Budget Schema

The plan the pipeline compares actuals against. Category ids above are the only categories transactions can get.
