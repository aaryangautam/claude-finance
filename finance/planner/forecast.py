"""Cash-flow forecast, N months ahead.

Income = BudgetSchema.planned_net. Fixed and allocation categories = planned. Variable categories use the blend:
planned until the category has 3 complete months of actuals (its first posted month is at least 3 complete months
back), then the trailing 3-complete-month average (months with no spend count as 0). Both numbers are kept.
EUR-planned categories convert at the latest ECB EURUSD on file (raises if none).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from ..vault import BudgetSchema
from .common import add_months, check_month, hub_id, latest_balance, latest_fx_rate, month_of, planned_usd

BLEND_MONTHS = 3


@dataclass
class CategoryBasis:
    id: str
    type: str
    planned_usd: float
    trailing_avg_usd: float | None  # None when fewer than 3 complete months of actuals
    used: str  # planned | trailing_avg
    forecast_usd: float


@dataclass
class ForecastMonth:
    month: str
    income_usd: float
    fixed_usd: float
    variable_usd: float
    allocations_usd: float
    surplus_usd: float
    hub_balance_usd: float | None = None  # cumulative, if the surplus stayed in the hub (before the waterfall sweeps it)


@dataclass
class Forecast:
    start: str
    months: list[ForecastMonth]
    basis: list[CategoryBasis]
    trailing_window: list[str]
    fx_rates: dict[str, tuple[float, str]]
    hub_account: str
    hub_start_balance: tuple[float, str] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _basis(conn: sqlite3.Connection, schema: BudgetSchema, rates: dict[str, float], last_complete: str) -> tuple[list[CategoryBasis], list[str]]:
    window = [add_months(last_complete, -i) for i in range(BLEND_MONTHS)]  # newest first
    oldest = window[-1]
    first_month = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT category, MIN(month) FROM v_budget WHERE category IS NOT NULL GROUP BY category"
        )
    }
    trailing = {
        r[0]: float(r[1])
        for r in conn.execute(
            "SELECT category, SUM(spend_usd) FROM v_budget WHERE category IS NOT NULL AND month BETWEEN ? AND ? GROUP BY category",
            (oldest, last_complete),
        )
    }
    out: list[CategoryBasis] = []
    for cat in schema.categories:
        if cat.group == "income":
            continue
        p = planned_usd(cat, rates)
        avg = None
        if cat.type == "variable" and first_month.get(cat.id) and first_month[cat.id] <= oldest:
            avg = round(trailing.get(cat.id, 0.0) / BLEND_MONTHS, 2)
        used = "trailing_avg" if avg is not None else "planned"
        out.append(CategoryBasis(cat.id, cat.type, p, avg, used, avg if avg is not None else p))
    return out, window


def build(conn: sqlite3.Connection, schema: BudgetSchema, months: int = 6, start: str | None = None,
          today: date | None = None, with_balance: bool = True) -> Forecast:
    """Forecast `months` months from `start` (default: the month after today's)."""
    today = today or date.today()
    start = check_month(start) if start else add_months(month_of(today), 1)
    if months < 1:
        raise ValueError("months must be >= 1")
    # Trailing window ends at the last month that is complete both as of today and before the forecast starts.
    last_complete = min(add_months(month_of(today), -1), add_months(start, -1))

    fx = {cur: latest_fx_rate(conn, cur) for cur in sorted({c.currency for c in schema.categories} - {"USD"})}
    rates = {k: v[0] for k, v in fx.items()}
    basis, window = _basis(conn, schema, rates, last_complete)

    income = round(schema.planned_net, 2)
    fixed = round(sum(b.forecast_usd for b in basis if b.type == "fixed"), 2)
    variable = round(sum(b.forecast_usd for b in basis if b.type == "variable"), 2)
    alloc = round(sum(b.forecast_usd for b in basis if b.type == "allocation"), 2)
    surplus = round(income - fixed - variable - alloc, 2)

    hub = hub_id(schema)
    fc = Forecast(start=start, months=[], basis=basis, trailing_window=window, fx_rates=fx, hub_account=hub)
    bal = None
    if with_balance:
        fc.hub_start_balance = latest_balance(conn, hub)
        bal = fc.hub_start_balance[0]
        fc.notes.append(f"hub balance starts from the {fc.hub_start_balance[1]} snapshot and assumes the surplus stays in {hub}")
    for i in range(months):
        if bal is not None:
            bal = round(bal + surplus, 2)
        fc.months.append(ForecastMonth(add_months(start, i), income, fixed, variable, alloc, surplus, bal))
    return fc
