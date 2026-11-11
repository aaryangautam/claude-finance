"""Shared helpers: month arithmetic, FX for planned amounts, snapshot lookups. All reads are aggregates."""

from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date

from ..vault import BudgetSchema, Category

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PlannerError(RuntimeError):
    pass


def check_month(month: str) -> str:
    if not MONTH_RE.match(month or ""):
        raise PlannerError(f"month must be YYYY-MM, got {month!r}")
    return month


def add_months(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_of(d: date) -> str:
    return d.strftime("%Y-%m")


def days_in_month(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def month_end(month: str) -> str:
    return f"{month}-{days_in_month(month):02d}"


def hub_id(schema: BudgetSchema) -> str:
    hubs = schema.accounts_by_role("hub")
    if len(hubs) != 1:
        raise PlannerError(f"expected exactly one hub account, got {[h.id for h in hubs]}")
    return hubs[0].id


def month_fx_rate(conn: sqlite3.Connection, currency: str, month: str) -> float:
    """Average ECB rate (1 unit of currency in USD) over the month's fx_rates rows. Raises if none."""
    if currency == "USD":
        return 1.0
    pair = f"{currency}USD"
    r = conn.execute(
        "SELECT AVG(rate), COUNT(*) FROM fx_rates WHERE pair=? AND source='ecb' AND substr(date,1,7)=?",
        (pair, month),
    ).fetchone()
    if not r[1]:
        raise PlannerError(f"no {pair} (ecb) rate in fx_rates for {month}; run a sync so fx.py caches one")
    return float(r[0])


def latest_fx_rate(conn: sqlite3.Connection, currency: str) -> tuple[float, str]:
    """Most recent ECB rate on file, used for future months. Returns (rate, date). Raises if none."""
    if currency == "USD":
        return 1.0, ""
    r = conn.execute(
        "SELECT rate, date FROM fx_rates WHERE pair=? AND source='ecb' ORDER BY date DESC LIMIT 1", (f"{currency}USD",)
    ).fetchone()
    if not r:
        raise PlannerError(f"no {currency}USD (ecb) rate in fx_rates; cannot convert {currency}-planned categories")
    return float(r[0]), r[1]


def planned_usd(cat: Category, rates: dict[str, float]) -> float:
    if cat.currency == "USD":
        return cat.planned
    if cat.currency not in rates:
        raise PlannerError(f"category {cat.id}: no rate for {cat.currency}")
    return round(cat.planned * rates[cat.currency], 2)


def latest_balance(conn: sqlite3.Connection, account_id: str, on_or_before: str | None = None) -> tuple[float, str]:
    """(balance_usd, date) of the latest snapshot for an account. Raises if there is none."""
    sql = "SELECT balance_usd, date FROM balance_snapshots WHERE account_id=? AND balance_usd IS NOT NULL"
    params: tuple = (account_id,)
    if on_or_before:
        sql += " AND date<=?"
        params += (on_or_before,)
    r = conn.execute(sql + " ORDER BY date DESC LIMIT 1", params).fetchone()
    if not r:
        suffix = f" on or before {on_or_before}" if on_or_before else ""
        raise PlannerError(f"no balance snapshot for {account_id}{suffix}; run `finance snapshot`")
    return float(r[0]), r[1]
