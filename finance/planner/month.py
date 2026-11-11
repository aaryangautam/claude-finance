"""Month plan vs actual, per Budget Schema category. Every number is a GROUP BY over v_budget / transactions."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from ..vault import BudgetSchema
from .common import check_month, days_in_month, hub_id, month_fx_rate, month_of, planned_usd


@dataclass
class CategoryLine:
    id: str
    group: str
    type: str
    currency: str
    planned_native: float
    planned_usd: float
    actual_usd: float  # net of reimbursements (they come in as negative spend)
    reimbursed_usd: float  # the reimbursement part of actual_usd, shown as a positive amount
    variance_usd: float  # actual - planned; > 0 = over plan
    pct_used: float | None  # actual / planned * 100; None when planned is 0
    projected_usd: float | None = None  # variable, current month only: pace-based month end
    paid: bool | None = None  # fixed only: any posted outflow this month


@dataclass
class MonthPlan:
    month: str
    is_current: bool
    days_elapsed: int
    days_in_month: int
    fx_rates: dict[str, float]
    lines: list[CategoryLine]
    planned_net_income: float
    observed_income: float
    income_variance: float  # observed - planned; a number, not an error (e.g. FX-linked pay)
    uncategorized_usd: float
    totals: dict = field(default_factory=dict)

    def line(self, cid: str) -> CategoryLine:
        for l in self.lines:
            if l.id == cid:
                return l
        raise KeyError(cid)

    def as_dict(self) -> dict:
        return asdict(self)


def build(conn: sqlite3.Connection, schema: BudgetSchema, month: str, today: date | None = None,
          income_category: str = "income_pay") -> MonthPlan:
    check_month(month)
    today = today or date.today()
    is_current = month == month_of(today)
    dim = days_in_month(month)
    if is_current:
        elapsed = today.day
    elif month < month_of(today):
        elapsed = dim
    else:
        elapsed = 0

    rates = {cur: month_fx_rate(conn, cur, month) for cur in sorted({c.currency for c in schema.categories} - {"USD"})}

    agg = {
        r["category"]: r
        for r in conn.execute(
            """SELECT category,
                      SUM(spend_usd) AS actual,
                      SUM(CASE WHEN is_reimbursement=1 THEN -spend_usd ELSE 0 END) AS reimbursed,
                      SUM(CASE WHEN spend_usd > 0 THEN 1 ELSE 0 END) AS outflows
               FROM v_budget WHERE month=? AND category IS NOT NULL GROUP BY category""",
            (month,),
        )
    }

    lines: list[CategoryLine] = []
    for cat in schema.categories:
        if cat.group == "income":
            continue
        p_usd = planned_usd(cat, rates)
        r = agg.get(cat.id)
        actual = round(r["actual"] or 0.0, 2) if r else 0.0
        line = CategoryLine(
            id=cat.id, group=cat.group, type=cat.type, currency=cat.currency,
            planned_native=cat.planned, planned_usd=p_usd, actual_usd=actual,
            reimbursed_usd=round(r["reimbursed"] or 0.0, 2) if r else 0.0,
            variance_usd=round(actual - p_usd, 2),
            pct_used=round(actual / p_usd * 100, 1) if p_usd else None,
        )
        if cat.type == "variable" and is_current:
            line.projected_usd = round(actual / elapsed * dim, 2)
        if cat.type == "fixed":
            line.paid = bool(r and r["outflows"])
        lines.append(line)

    hub = hub_id(schema)
    observed = conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) FROM transactions WHERE account_id=? AND category=? "
        "AND is_transfer=0 AND pending=0 AND substr(date,1,7)=?",
        (hub, income_category, month),
    ).fetchone()[0]
    uncategorized = conn.execute(
        "SELECT COALESCE(SUM(spend_usd),0) FROM v_spending WHERE month=? AND category IS NULL", (month,)
    ).fetchone()[0]

    planned_net = round(schema.planned_net, 2)
    observed = round(float(observed), 2)

    def tot(group: str | None, attr: str) -> float:
        return round(sum(getattr(l, attr) for l in lines if group is None or l.group == group), 2)

    totals = {
        "planned_expenses_usd": tot("expenses", "planned_usd"),
        "actual_expenses_usd": tot("expenses", "actual_usd"),
        "planned_allocations_usd": round(tot("saving", "planned_usd") + tot("investing", "planned_usd"), 2),
        "actual_allocations_usd": round(tot("saving", "actual_usd") + tot("investing", "actual_usd"), 2),
        "reimbursed_usd": tot(None, "reimbursed_usd"),
        "uncategorized_usd": round(float(uncategorized), 2),
    }
    totals["planned_surplus_usd"] = round(planned_net - totals["planned_expenses_usd"] - totals["planned_allocations_usd"], 2)
    totals["actual_surplus_usd"] = round(
        observed - totals["actual_expenses_usd"] - totals["actual_allocations_usd"] - totals["uncategorized_usd"], 2
    )

    return MonthPlan(
        month=month, is_current=is_current, days_elapsed=elapsed, days_in_month=dim, fx_rates=rates, lines=lines,
        planned_net_income=planned_net, observed_income=observed, income_variance=round(observed - planned_net, 2),
        uncategorized_usd=totals["uncategorized_usd"], totals=totals,
    )


def income_history(conn: sqlite3.Connection, schema: BudgetSchema, months: int = 12,
                   income_category: str = "income_pay") -> list[dict]:
    """Observed income_pay deposits on the hub, per month, newest first."""
    rows = conn.execute(
        "SELECT substr(date,1,7) m, ROUND(SUM(amount_usd),2) observed FROM transactions "
        "WHERE account_id=? AND category=? AND is_transfer=0 AND pending=0 GROUP BY m ORDER BY m DESC LIMIT ?",
        (hub_id(schema), income_category, months),
    ).fetchall()
    return [dict(r) for r in rows]
