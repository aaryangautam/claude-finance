"""Waterfall simulation over the forecast surplus, and goal projections.

Waterfall rules are read from the Budget Schema and interpreted deterministically; a rule this module can't
interpret raises SchemaError rather than being skipped:
  baseline  rule mentions fixed expenses / allocations (step 1): already netted out of the forecast surplus
  hold      has `target` and names an account (or its `goal` is an account's goal): top the account up to target;
            anything above target is released down the waterfall
  sink      "remaining" / "all" surplus → an account: takes whatever is left
Planned allocations (categories with `account:`) land in their account every month before the waterfall runs.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field

from ..vault import BudgetSchema, Goal, SchemaError
from .common import add_months, latest_balance
from .forecast import Forecast


@dataclass
class WaterfallRule:
    step: int
    kind: str  # baseline | hold | sink
    rule: str
    account: str | None = None
    target: float | None = None
    goal: str | None = None


def _rule_account(schema: BudgetSchema, rule: str, goal: str | None) -> str | None:
    for aid in schema.account_ids:
        if re.search(rf"(?<![\w]){re.escape(aid)}(?![\w])", rule):
            return aid
    if goal:
        for a in schema.accounts:
            if a.goal == goal:
                return a.id
    return None


def parse_waterfall(schema: BudgetSchema) -> list[WaterfallRule]:
    out: list[WaterfallRule] = []
    for w in schema.waterfall:
        text = w.rule.lower()
        acct = _rule_account(schema, w.rule, w.goal)
        if w.target is not None:
            if not acct:
                raise SchemaError(f"waterfall step {w.step}: has a target but names no account: {w.rule!r}")
            out.append(WaterfallRule(w.step, "hold", w.rule, acct, w.target, w.goal))
        elif acct and re.search(r"\b(remaining|all|rest)\b", text):
            out.append(WaterfallRule(w.step, "sink", w.rule, acct, None, w.goal))
        elif re.search(r"\b(fixed|allocations?)\b", text):
            out.append(WaterfallRule(w.step, "baseline", w.rule, None, None, w.goal))
        else:
            raise SchemaError(f"waterfall step {w.step}: can't interpret rule {w.rule!r}")
    if sum(r.kind == "sink" for r in out) > 1:
        raise SchemaError("waterfall: more than one 'remaining surplus' step")
    return out


def allocation_by_account(schema: BudgetSchema) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in schema.categories:
        if c.type == "allocation" and c.account:
            out[c.account] = out.get(c.account, 0.0) + c.planned
    return out


@dataclass
class WaterfallMonth:
    month: str
    surplus_usd: float
    moves: dict[str, float]  # account -> amount moved by the waterfall (negative = released above target)
    balances: dict[str, float]  # hold accounts, after the month
    unallocated_usd: float  # surplus left in the hub (or deficit, if negative)


def simulate(conn: sqlite3.Connection, schema: BudgetSchema, fc: Forecast, rules: list[WaterfallRule] | None = None,
             as_of: str | None = None) -> list[WaterfallMonth]:
    """Run each forecast month's surplus through the waterfall. Hold accounts start at their latest snapshot (<= as_of)."""
    rules = rules if rules is not None else parse_waterfall(schema)
    alloc = allocation_by_account(schema)
    bal = {r.account: latest_balance(conn, r.account, as_of)[0] for r in rules if r.kind == "hold"}
    out: list[WaterfallMonth] = []
    for m in fc.months:
        for acct in bal:
            bal[acct] = round(bal[acct] + alloc.get(acct, 0.0), 2)
        remaining = m.surplus_usd
        moves: dict[str, float] = {}
        for r in rules:
            if r.kind == "hold":
                b = bal[r.account]
                if b > r.target:
                    excess = round(b - r.target, 2)
                    bal[r.account] = r.target
                    remaining = round(remaining + excess, 2)
                    moves[r.account] = -excess
                elif remaining > 0:
                    mv = round(min(remaining, r.target - b), 2)
                    bal[r.account] = round(b + mv, 2)
                    remaining = round(remaining - mv, 2)
                    moves[r.account] = mv
                else:
                    moves[r.account] = 0.0
            elif r.kind == "sink":
                mv = round(max(remaining, 0.0), 2)
                moves[r.account] = mv
                remaining = round(remaining - mv, 2)
        out.append(WaterfallMonth(m.month, m.surplus_usd, moves, dict(bal), remaining))
    return out


@dataclass
class GoalProjection:
    title: str
    status: str  # tracked | reached | rate | not_trackable
    reason: str | None = None
    measure: str | None = None
    progress_source: str | None = None
    account: str | None = None
    target_amount: float | None = None
    target_date: str | None = None
    current_usd: float | None = None
    current_as_of: str | None = None
    monthly_flow_usd: float | None = None  # average over the forecast horizon
    projected_month: str | None = None  # first month the target is reached; None = not reached at this plan
    beyond_horizon: bool = False
    on_track: bool | None = None
    path: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _resolve_source(schema: BudgetSchema, source: str) -> tuple[str | None, list[str]]:
    """progress_source is a schema account id or an allocation category id -> (account, allocation category ids)."""
    if source in schema.account_ids:
        return source, [c.id for c in schema.categories if c.type == "allocation" and c.account == source]
    if source in schema.category_ids:
        c = schema.category(source)
        if c.type != "allocation":
            raise SchemaError(f"progress_source {source!r} is a {c.type} category; only allocation categories track goals")
        return c.account, [c.id]
    raise SchemaError(f"progress_source {source!r} is neither a Budget Schema account id nor a category id")


def _project_rate(conn: sqlite3.Connection, g: Goal, gp: GoalProjection, account: str | None, cats: list[str],
                  alloc_cat: dict[str, float], fc: Forecast, wf: list[WaterfallMonth]) -> GoalProjection:
    """contribution-rate: target_amount is per month. Current = trailing 3-complete-month average contribution;
    the plan's monthly flow (planned allocation + waterfall moves) decides on_track. A rate never 'reaches' a target."""
    if g.measure != "contributions":
        raise SchemaError(f"goal {g.title}: contribution-rate goals need measure=contributions, got {g.measure!r}")
    if not cats:
        raise SchemaError(f"goal {g.title}: no allocation category funds {g.progress_source!r}")
    window = fc.trailing_window  # newest first
    qs = ",".join("?" for _ in cats)
    r = conn.execute(
        f"SELECT COALESCE(SUM(spend_usd),0) FROM v_budget WHERE category IN ({qs}) AND month BETWEEN ? AND ?",
        (*cats, window[-1], window[0]),
    ).fetchone()
    gp.status = "rate"
    gp.current_usd = round(float(r[0]) / len(window), 2)
    gp.current_as_of = f"{window[-1]}..{window[0]} avg/mo"
    planned_in = sum(alloc_cat[c] for c in cats)
    flows = [round(planned_in + (w.moves.get(account, 0.0) if account else 0.0), 2) for w in wf]
    gp.path = flows
    gp.monthly_flow_usd = round(sum(flows) / len(flows), 2) if flows else 0.0
    gp.on_track = gp.monthly_flow_usd >= g.target_amount - 0.005
    return gp


def project_goals(conn: sqlite3.Connection, schema: BudgetSchema, goals: list[Goal], fc: Forecast,
                  wf: list[WaterfallMonth] | None = None, as_of: str | None = None) -> list[GoalProjection]:
    """`as_of` (YYYY-MM-DD) caps current progress at that date; default: everything on file."""
    wf = wf if wf is not None else simulate(conn, schema, fc, as_of=as_of)
    alloc_cat = {c.id: c.planned for c in schema.categories if c.type == "allocation"}
    out: list[GoalProjection] = []
    for g in goals:
        if g.status != "active":
            continue
        gp = GoalProjection(g.title, "not_trackable", measure=g.measure, progress_source=g.progress_source,
                            target_amount=g.target_amount, target_date=g.target_date)
        if g.target_amount is None:
            gp.reason = "no target_amount"
            out.append(gp)
            continue
        if not g.progress_source:
            gp.reason = "not yet trackable (progress_source is null)"
            out.append(gp)
            continue
        account, cats = _resolve_source(schema, g.progress_source)
        gp.account = account

        if g.goal_type == "contribution-rate":
            out.append(_project_rate(conn, g, gp, account, cats, alloc_cat, fc, wf))
            continue

        if g.measure == "balance":
            if not account:
                raise SchemaError(f"goal {g.title}: measure=balance needs an account")
            gp.current_usd, gp.current_as_of = latest_balance(conn, account, as_of)
        elif g.measure == "contributions":
            if not cats:
                raise SchemaError(f"goal {g.title}: no allocation category funds {g.progress_source!r}")
            qs = ",".join("?" for _ in cats)
            sql = f"SELECT COALESCE(SUM(spend_usd),0), MAX(date) FROM v_budget WHERE category IN ({qs})"
            params: tuple = tuple(cats)
            if as_of:
                sql += " AND date<=?"
                params += (as_of,)
            r = conn.execute(sql, params).fetchone()
            gp.current_usd, gp.current_as_of = round(float(r[0]), 2), r[1]
        else:
            raise SchemaError(f"goal {g.title}: measure {g.measure!r} not in ['balance', 'contributions']")

        # monthly flow into the goal: planned allocations for its categories + what the waterfall moves to its account
        planned_in = sum(alloc_cat[c] for c in cats)
        flows = [round(planned_in + (w.moves.get(account, 0.0) if account else 0.0), 2) for w in wf]
        hold_balances = [w.balances[account] for w in wf] if g.measure == "balance" and wf and account in wf[0].balances else None

        value = gp.current_usd
        for i, f in enumerate(flows):
            value = hold_balances[i] if hold_balances else round(value + f, 2)
            gp.path.append(value)
        gp.monthly_flow_usd = round(sum(flows) / len(flows), 2) if flows else 0.0

        if gp.current_usd >= g.target_amount:
            gp.status = "reached"
        else:
            gp.status = "tracked"
            for i, v in enumerate(gp.path):
                if v >= g.target_amount - 0.005:
                    gp.projected_month = fc.months[i].month
                    break
            if gp.projected_month is None and gp.monthly_flow_usd > 0 and not hold_balances:
                extra = math.ceil((g.target_amount - gp.path[-1]) / gp.monthly_flow_usd)
                gp.projected_month = add_months(fc.months[-1].month, extra)
                gp.beyond_horizon = True
        if g.target_date:
            if gp.status == "reached":
                gp.on_track = True
            elif gp.projected_month:
                gp.on_track = gp.projected_month <= g.target_date[:7]
            else:
                gp.on_track = False
        out.append(gp)
    return out
