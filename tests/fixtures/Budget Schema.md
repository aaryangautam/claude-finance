---
base_currency: USD
pay_schedule: "1st and 15th"
income_planned:
  - {id: salary, planned: 5000}
  - {id: stipend, planned: 300}
  - {id: housing_allowance, planned: 2000, note: "paid in EUR, moves with FX"}
pre_deductions:
  - {id: tax, planned: 900}
  - {id: retirement_401k, planned: 500}
categories:
  - {id: income_pay, group: income, type: fixed, planned: 5900}
  - {id: housing_rent, group: expenses, type: fixed, planned: 1500, currency: EUR}
  - {id: utilities_electricity, group: expenses, type: fixed, planned: 80, currency: EUR}
  - {id: phone, group: expenses, type: fixed, planned: 120, currency: EUR}
  - {id: insurance_car, group: expenses, type: fixed, planned: 150}
  - {id: debt_car_loan, group: expenses, type: fixed, planned: 450}
  - {id: groceries, group: expenses, type: variable, planned: 400}
  - {id: dining, group: expenses, type: variable, planned: 250}
  - {id: transport, group: expenses, type: variable, planned: 150}
  - {id: travel, group: expenses, type: variable, planned: 300}
  - {id: shopping_household, group: expenses, type: variable, planned: 200}
  - {id: subscriptions, group: expenses, type: fixed, planned: 60}
  - {id: health, group: expenses, type: variable, planned: 50}
  - {id: entertainment, group: expenses, type: variable, planned: 100}
  - {id: gifts, group: expenses, type: variable, planned: 50}
  - {id: cash_atm, group: expenses, type: variable, planned: 100}
  - {id: other, group: expenses, type: variable, planned: 100}
  - {id: saving_emergency_fund, group: saving, type: allocation, planned: 0, account: savings}
  - {id: investing_roth_ira, group: investing, type: allocation, planned: 583, account: roth_ira}
  - {id: investing_aggressive, group: investing, type: allocation, planned: 1000, account: brokerage}
accounts:
  - {id: checking, institution: Example Bank, role: hub, currency: USD}
  - {id: savings, institution: Example Bank, role: savings, currency: USD, goal: Emergency Fund}
  - {id: car_loan, institution: Example Bank, role: liability, currency: USD}
  - {id: card_travel, institution: Example Card, role: credit, currency: USD}
  - {id: card_everyday, institution: Example Card, role: credit, currency: USD}
  - {id: card_dining, institution: Other Card, role: credit, currency: USD}
  - {id: roth_ira, institution: Example Broker, role: ira, currency: USD}
  - {id: brokerage, institution: Example Broker, role: brokerage, currency: USD}
  - {id: wise_usd, institution: Wise, role: fx_hub, currency: USD}
  - {id: wise_eur, institution: Wise, role: fx_hub, currency: EUR}
waterfall:
  - {step: 1, rule: "fixed expenses and allocations paid"}
  - {step: 2, rule: "savings held at target", target: 10000, goal: Emergency Fund}
  - {step: 3, rule: "all remaining surplus → brokerage", goal: Aggressive Investing}
reimbursements:
  - {category: phone, from: "roommates via Wise", treatment: net_against_expense}
---

# Budget Schema (test fixture)
