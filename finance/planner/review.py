"""`finance review YYYY-MM` → <vault_finance>/Reviews/YYYY-MM.md: frontmatter + deterministic tables + empty Commentary.

The tables come from month.py, forecast.py and goals.py; the commentary is written later by the
finance-review-commentary skill, which reads only this file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from ..db import now_iso
from ..vault import BudgetSchema, Goal
from . import forecast as fc_mod
from . import goals as goals_mod
from . import month as month_mod
from .common import add_months, check_month, latest_balance, month_end

COMMENTARY_HEADING = "## Commentary"


def _usd(x: float | None) -> str:
    if x is None:
        return "-"
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _table(headers: list[str], rows: list[list], right: set[int] | None = None) -> str:
    right = right if right is not None else set(range(1, len(headers)))
    sep = ["---:" if i in right else "---" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def waterfall_check(conn: sqlite3.Connection, schema: BudgetSchema, plan: month_mod.MonthPlan) -> list[str]:
    """Literal statements of what the waterfall implied for the month vs what the allocation legs show."""
    month = plan.month
    end = month_end(month)
    rules = goals_mod.parse_waterfall(schema)
    alloc = goals_mod.allocation_by_account(schema)
    sink = next((r for r in rules if r.kind == "sink"), None)
    out = [
        f"Month surplus (observed income − actual spend − actual allocations − uncategorized): "
        f"{_usd(plan.totals['actual_surplus_usd'])} (planned {_usd(plan.totals['planned_surplus_usd'])})."
    ]
    released = 0.0
    for r in rules:
        if r.kind == "baseline":
            unpaid = [l.id for l in plan.lines if l.type == "fixed" and not l.paid]
            short = [l.id for l in plan.lines if l.type == "allocation" and l.planned_usd and l.actual_usd < l.planned_usd - 0.005]
            out.append(f"Step {r.step} ({r.rule}): fixed not paid: {', '.join(unpaid) or 'none'}; "
                       f"allocations below plan: {', '.join(short) or 'none'}.")
        elif r.kind == "hold":
            bal, as_of = latest_balance(conn, r.account, end)
            label = r.goal or r.account
            if bal > r.target:
                excess = round(bal - r.target, 2)
                released += excess
                dest = sink.account if sink else "the next waterfall step"
                out.append(f"Step {r.step}: {label} ({r.account}) ended at {_usd(bal)} (snapshot {as_of}), "
                           f"target {_usd(r.target)}, so {_usd(excess)} should have moved to {dest}.")
            elif bal < r.target:
                out.append(f"Step {r.step}: {label} ({r.account}) ended at {_usd(bal)} (snapshot {as_of}), "
                           f"{_usd(r.target - bal)} short of the {_usd(r.target)} target, so surplus should go there first.")
            else:
                out.append(f"Step {r.step}: {label} ({r.account}) ended exactly at its {_usd(r.target)} target (snapshot {as_of}).")
        elif r.kind == "sink":
            cats = [c.id for c in schema.categories if c.type == "allocation" and c.account == r.account]
            observed = sum(l.actual_usd for l in plan.lines if l.id in cats)
            planned = alloc.get(r.account, 0.0)
            expected = round(planned + released, 2)
            out.append(f"Step {r.step}: {r.account}: planned allocations {_usd(planned)} + {_usd(released)} released above "
                       f"target = {_usd(expected)} expected; observed: {_usd(observed)} "
                       f"({'+' if observed - expected >= 0 else '−'}{_usd(abs(observed - expected))} vs expected).")
    return out


def render(conn: sqlite3.Connection, schema: BudgetSchema, goals: list[Goal], month: str, today: date | None = None,
           months_ahead: int = 6, income_category: str = "income_pay") -> tuple[str, dict]:
    check_month(month)
    today = today or date.today()
    plan = month_mod.build(conn, schema, month, today=today, income_category=income_category)
    fc = fc_mod.build(conn, schema, months=months_ahead, start=add_months(month, 1), today=today)
    as_of = month_end(month)
    wf = goals_mod.simulate(conn, schema, fc, as_of=as_of)
    gps = goals_mod.project_goals(conn, schema, goals, fc, wf=wf, as_of=as_of)
    checks = waterfall_check(conn, schema, plan)

    totals = {
        "planned_net_income_usd": plan.planned_net_income,
        "observed_income_usd": plan.observed_income,
        **plan.totals,
        "forecast_monthly_surplus_usd": fc.months[0].surplus_usd,
        "forecast_hub_balance_end_usd": fc.months[-1].hub_balance_usd,
    }
    fm = {"month": month, "generated_at": now_iso(), "totals": totals}

    md: list[str] = [f"# Finance review {month}", ""]
    rates = ", ".join(f"{k}USD {v:.4f} (ECB, month avg)" for k, v in plan.fx_rates.items()) or "none needed"
    status = f"current month, day {plan.days_elapsed} of {plan.days_in_month}" if plan.is_current else "complete month"
    md += [f"Generated by `finance review {month}` ({status}). FX: {rates}. All amounts USD.", ""]

    md += ["## Plan vs actual", ""]
    rows = []
    for l in plan.lines:
        extra = []
        if l.paid is not None:
            extra.append("paid" if l.paid else "not yet paid")
        if l.projected_usd is not None:
            extra.append(f"pace → {_usd(l.projected_usd)}")
        if l.reimbursed_usd:
            extra.append(f"net of {_usd(l.reimbursed_usd)} reimbursed")
        planned = _usd(l.planned_usd) + (f" ({l.planned_native:,.0f} {l.currency})" if l.currency != "USD" else "")
        rows.append([f"`{l.id}`", l.type, planned, _usd(l.actual_usd), _usd(l.variance_usd),
                     f"{l.pct_used:.0f}%" if l.pct_used is not None else "-", "; ".join(extra)])
    md += [_table(["Category", "Type", "Planned", "Actual", "Variance", "% used", "Notes"], rows, {2, 3, 4, 5}), ""]
    t = plan.totals
    md += [_table(["Total", "Planned", "Actual"], [
        ["Expenses", _usd(t["planned_expenses_usd"]), _usd(t["actual_expenses_usd"])],
        ["Allocations", _usd(t["planned_allocations_usd"]), _usd(t["actual_allocations_usd"])],
        ["Uncategorized spend", "-", _usd(t["uncategorized_usd"])],
        ["Surplus", _usd(t["planned_surplus_usd"]), _usd(t["actual_surplus_usd"])],
    ]), ""]

    md += ["## Income", "", _table(["", "Amount"], [
        ["Planned net (schema)", _usd(plan.planned_net_income)],
        [f"Observed `{income_category}` on hub", _usd(plan.observed_income)],
        ["Variance", _usd(plan.income_variance)],
    ]), ""]

    md += ["## Waterfall check", ""] + [f"- {c}" for c in checks] + [""]

    md += [f"## Forecast ({fc.months[0].month} → {fc.months[-1].month})", ""]
    md += [_table(["Month", "Income", "Fixed", "Variable", "Allocations", "Surplus", "Hub balance"], [
        [m.month, _usd(m.income_usd), _usd(m.fixed_usd), _usd(m.variable_usd), _usd(m.allocations_usd),
         _usd(m.surplus_usd), _usd(m.hub_balance_usd)] for m in fc.months]), ""]
    md += [f"Hub balance starts from the {fc.hub_start_balance[1]} snapshot ({_usd(fc.hub_start_balance[0])}) and assumes the "
           f"surplus stays in the hub; the waterfall below shows where it should go.", ""]
    md += [f"Variable categories (blend: planned until 3 complete months of actuals, then trailing average of "
           f"{', '.join(reversed(fc.trailing_window))}):", ""]
    md += [_table(["Category", "Planned", "Trailing 3-mo avg", "Used"], [
        [f"`{b.id}`", _usd(b.planned_usd), _usd(b.trailing_avg_usd), b.used] for b in fc.basis if b.type == "variable"]), ""]
    if wf:
        accts = list(wf[0].moves)
        md += ["Waterfall per forecast month:", ""]
        md += [_table(["Month", "Surplus"] + [f"→ {a}" for a in accts] + ["Left in hub"], [
            [w.month, _usd(w.surplus_usd)] + [_usd(w.moves[a]) for a in accts] + [_usd(w.unallocated_usd)] for w in wf]), ""]

    md += ["## Goals", ""]
    rows = []
    for g in gps:
        if g.status == "not_trackable":
            rows.append([g.title, "-", "-", "-", "-", "-", g.reason])
            continue
        if g.status == "rate":
            rows.append([g.title, f"monthly {g.measure} to `{g.progress_source}`", f"{_usd(g.current_usd)} ({g.current_as_of})",
                         f"{_usd(g.target_amount)}/mo", "-", f"{_usd(g.monthly_flow_usd)}/mo planned",
                         "on track" if g.on_track else "below rate"])
            continue
        on = {True: "on track", False: "off track", None: "no target date"}[g.on_track]
        proj = "reached" if g.status == "reached" else (g.projected_month or "not reached at this plan")
        if g.beyond_horizon:
            proj += " (extrapolated)"
        rows.append([g.title, f"{g.measure} of `{g.progress_source}`", f"{_usd(g.current_usd)} ({g.current_as_of})",
                     _usd(g.target_amount), g.target_date or "-", proj, on])
    md += [_table(["Goal", "Measure", "Current", "Target", "Target date", "Projected", "Status"], rows, {3}) if rows
           else "No active goals.", ""]

    md += [COMMENTARY_HEADING, "", ""]
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + "\n".join(md)
    return text, fm


def write(conn: sqlite3.Connection, schema: BudgetSchema, goals: list[Goal], month: str, reviews_dir: Path,
          force: bool = False, today: date | None = None, months_ahead: int = 6, income_category: str = "income_pay",
          unclear_threshold: float = 0.8) -> Path:
    check_month(month)
    path = reviews_dir / f"{month}.md"
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite it")
    text, fm = render(conn, schema, goals, month, today=today, months_ahead=months_ahead, income_category=income_category)
    reviews_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    unclear = conn.execute(
        """SELECT COUNT(DISTINCT merchant_norm) FROM transactions
           WHERE is_transfer=0 AND pending=0 AND merchant_norm != '' AND substr(date,1,7)=?
             AND (category IS NULL OR (category_source IN ('llm','plaid') AND category_confidence < ?) OR needs_review=1)""",
        (month, unclear_threshold),
    ).fetchone()[0]
    corrections = conn.execute(
        "SELECT COUNT(*) FROM merchant_map WHERE source='user' AND substr(updated_at,1,7)=?", (month,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO review_log (month, unclear_count, corrections_made, notes, reviewed_at, path, totals_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (month, unclear, corrections, "generated by finance review", fm["generated_at"], str(path), json.dumps(fm["totals"])),
    )
    conn.commit()
    return path
