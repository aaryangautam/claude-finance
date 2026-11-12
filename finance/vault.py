"""Read the Obsidian reasoning layer: Budget Schema.md and Goals/*.md. Validates hard; no silent defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

GROUPS = {"income", "expenses", "saving", "investing"}
TYPES = {"fixed", "variable", "allocation"}
ROLES = {"hub", "savings", "liability", "credit", "ira", "brokerage", "fx_hub"}
MAX_CATEGORIES = 25


class SchemaError(ValueError):
    pass


@dataclass
class Category:
    id: str
    group: str
    type: str
    planned: float
    currency: str = "USD"
    renamed_from: str | None = None
    note: str | None = None
    account: str | None = None  # allocation categories: the account this allocation funds (e.g. roth_ira)


@dataclass
class Account:
    id: str
    institution: str
    role: str
    currency: str
    goal: str | None = None


@dataclass
class WaterfallStep:
    step: int
    rule: str
    target: float | None = None
    goal: str | None = None


@dataclass
class BudgetSchema:
    base_currency: str
    pay_schedule: str
    income_planned: list[dict]
    pre_deductions: list[dict]
    categories: list[Category]
    accounts: list[Account]
    waterfall: list[WaterfallStep]
    reimbursements: list[dict]
    body: str = ""

    @property
    def category_ids(self) -> list[str]:
        return [c.id for c in self.categories]

    def category(self, cid: str) -> Category:
        for c in self.categories:
            if c.id == cid:
                return c
        raise KeyError(cid)

    @property
    def account_ids(self) -> list[str]:
        return [a.id for a in self.accounts]

    def account(self, aid: str) -> Account:
        for a in self.accounts:
            if a.id == aid:
                return a
        raise KeyError(aid)

    def accounts_by_role(self, role: str) -> list[Account]:
        return [a for a in self.accounts if a.role == role]

    @property
    def planned_gross(self) -> float:
        return sum(float(i.get("planned", 0)) for i in self.income_planned)

    @property
    def planned_net(self) -> float:
        return self.planned_gross - sum(float(d.get("planned", 0)) for d in self.pre_deductions)

    @property
    def renames(self) -> dict[str, str]:
        """old_id -> new_id for categories carrying renamed_from."""
        return {c.renamed_from: c.id for c in self.categories if c.renamed_from}

    def planned_by_category(self) -> dict[str, float]:
        return {c.id: c.planned for c in self.categories}


def _req(d: dict, key: str, ctx: str):
    if key not in d or d[key] is None:
        raise SchemaError(f"{ctx}: missing '{key}'")
    return d[key]


def load_budget_schema(path: Path) -> BudgetSchema:
    if not path.exists():
        raise SchemaError(f"Budget Schema not found: {path}. Run the finance-budget-schema-ingestion skill first.")
    post = frontmatter.load(path)
    fm = post.metadata
    if not fm:
        raise SchemaError(f"{path.name}: no frontmatter")

    cats: list[Category] = []
    for i, c in enumerate(_req(fm, "categories", path.name)):
        ctx = f"categories[{i}]"
        cid = _req(c, "id", ctx)
        group = _req(c, "group", ctx)
        typ = _req(c, "type", ctx)
        if group not in GROUPS:
            raise SchemaError(f"{ctx} ({cid}): group '{group}' not in {sorted(GROUPS)}")
        if typ not in TYPES:
            raise SchemaError(f"{ctx} ({cid}): type '{typ}' not in {sorted(TYPES)}")
        planned = c.get("planned", 0)
        if planned is None:
            planned = 0
        try:
            planned = float(planned)
        except (TypeError, ValueError):
            raise SchemaError(f"{ctx} ({cid}): planned must be a number, got {planned!r}")
        cats.append(
            Category(
                id=cid,
                group=group,
                type=typ,
                planned=planned,
                currency=str(c.get("currency") or "USD"),
                renamed_from=c.get("renamed_from") or None,
                note=c.get("note"),
                account=c.get("account") or None,
            )
        )
    ids = [c.id for c in cats]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise SchemaError(f"duplicate category ids: {sorted(dupes)}")
    if len(cats) > MAX_CATEGORIES:
        raise SchemaError(f"{len(cats)} categories; keep it at {MAX_CATEGORIES} or fewer")
    if not cats:
        raise SchemaError("no categories defined")

    accts: list[Account] = []
    for i, a in enumerate(_req(fm, "accounts", path.name)):
        ctx = f"accounts[{i}]"
        role = _req(a, "role", ctx)
        if role not in ROLES:
            raise SchemaError(f"{ctx}: role '{role}' not in {sorted(ROLES)}")
        accts.append(
            Account(
                id=_req(a, "id", ctx),
                institution=_req(a, "institution", ctx),
                role=role,
                currency=str(_req(a, "currency", ctx)),
                goal=a.get("goal"),
            )
        )
    aids = [a.id for a in accts]
    bad = [a for a in aids if ":" in a]
    if bad:
        raise SchemaError(f"account ids can't contain ':' (reserved for provisional plaid:/provider: ids): {bad}")
    if len(set(aids)) != len(aids):
        raise SchemaError("duplicate account ids")
    hubs = [a.id for a in accts if a.role == "hub"]
    if len(hubs) != 1:
        raise SchemaError(f"need exactly one account with role 'hub' (the checking account pay lands in and transfers "
                          f"leave from), got {hubs or 'none'}; give the others role 'savings'")
    for c in cats:
        if c.account and c.account not in aids:
            raise SchemaError(f"category {c.id}: account '{c.account}' is not in accounts")

    steps: list[WaterfallStep] = []
    for i, w in enumerate(fm.get("waterfall") or []):
        ctx = f"waterfall[{i}]"
        steps.append(
            WaterfallStep(
                step=int(_req(w, "step", ctx)),
                rule=str(_req(w, "rule", ctx)),
                target=float(w["target"]) if w.get("target") is not None else None,
                goal=w.get("goal"),
            )
        )
    steps.sort(key=lambda s: s.step)

    return BudgetSchema(
        base_currency=str(fm.get("base_currency") or "USD"),
        pay_schedule=str(fm.get("pay_schedule") or ""),
        income_planned=list(fm.get("income_planned") or []),
        pre_deductions=list(fm.get("pre_deductions") or []),
        categories=cats,
        accounts=accts,
        waterfall=steps,
        reimbursements=list(fm.get("reimbursements") or []),
        body=post.content,
    )


@dataclass
class Goal:
    title: str
    path: Path
    status: str
    goal_type: str
    target_amount: float | None
    target_date: str | None
    measure: str
    progress_source: str | None
    priority: str
    waterfall_position: int | None
    related_goals: list[str] = field(default_factory=list)
    body: str = ""


def load_goals(goals_dir: Path) -> list[Goal]:
    goals: list[Goal] = []
    if not goals_dir.exists():
        return goals
    for p in sorted(goals_dir.glob("*.md")):
        post = frontmatter.load(p)
        fm = post.metadata
        if not fm or "goal_type" not in fm:
            raise SchemaError(f"{p.name}: not a goal note (no goal_type)")
        goals.append(
            Goal(
                title=p.stem,
                path=p,
                status=str(fm.get("status", "active")),
                goal_type=str(fm["goal_type"]),
                target_amount=float(fm["target_amount"]) if fm.get("target_amount") is not None else None,
                target_date=str(fm["target_date"]) if fm.get("target_date") else None,
                measure=str(fm.get("measure", "balance")),
                progress_source=fm.get("progress_source") or None,
                priority=str(fm.get("priority", "secondary")),
                waterfall_position=int(fm["waterfall_position"]) if fm.get("waterfall_position") is not None else None,
                related_goals=list(fm.get("related_goals") or []),
                body=post.content,
            )
        )
    return goals
