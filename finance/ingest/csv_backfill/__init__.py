"""One-time CSV backfill. Optional per-institution adapters -> the same transactions table, source='csv_backfill'.

Plaid only returns ~24 months (often less); these importers fill in older history from the institution's own CSV
export. They're optional: skip them if Plaid's window is enough. `generic` handles any Date/Description/signed-Amount
file; the named adapters encode that institution's export columns and sign convention. To add one, write a module
with `parse(rows, account_id)` next to these and register it in PARSERS (and NEEDS_ACCOUNT if the file doesn't say
which account it belongs to).

Each parser yields dicts: {date, amount_native (>0 into account), currency, merchant_raw, external_id?}.
Dedupe against provider-pulled rows on (account_id, date, amount_native, merchant_norm). Provider rows win.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ...pipeline.normalize import normalize
from ...vault import BudgetSchema
from . import amex, chase, generic, robinhood, usaa, wise

PARSERS = {"generic": generic.parse, "usaa": usaa.parse, "chase": chase.parse, "amex": amex.parse,
           "robinhood": robinhood.parse, "wise": wise.parse}
NEEDS_ACCOUNT = {"generic", "usaa", "chase", "amex", "robinhood"}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _ensure_account(conn: sqlite3.Connection, schema: BudgetSchema, account_id: str) -> None:
    if account_id not in schema.account_ids:
        raise ValueError(f"{account_id!r} not in Budget Schema accounts: {schema.account_ids}")
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        a = schema.account(account_id)
        typ = {"hub": "checking", "savings": "savings", "liability": "loan", "credit": "credit", "ira": "ira",
               "brokerage": "brokerage", "fx_hub": "multicurrency"}[a.role]
        conn.execute(
            "INSERT INTO accounts (id, institution, name, type, currency, role) VALUES (?,?,?,?,?,?)",
            (account_id, a.institution, account_id, typ, a.currency, a.role),
        )
        conn.commit()


def run(conn: sqlite3.Connection, schema: BudgetSchema, institution: str, path: Path, account: str | None, dry_run: bool = False) -> dict:
    if institution in NEEDS_ACCOUNT and not account:
        raise ValueError(f"--account is required for {institution} (a Budget Schema account id, e.g. checking)")
    rows = read_csv(path)
    parsed = list(PARSERS[institution](rows, account))
    stats = {"file": str(path), "parsed": len(parsed), "inserted": 0, "duplicates": 0, "investment_rows": 0}
    if institution == "robinhood":
        return _run_investments(conn, schema, parsed, account, stats, dry_run)
    if not dry_run:
        for aid in {r["account_id"] for r in parsed}:
            _ensure_account(conn, schema, aid)
    for r in parsed:
        norm = normalize(r["merchant_raw"])
        dup = conn.execute(
            "SELECT 1 FROM transactions WHERE account_id=? AND date=? AND ABS(amount_native-?)<0.005 AND merchant_norm=?",
            (r["account_id"], r["date"], r["amount_native"], norm),
        ).fetchone()
        if dup:
            stats["duplicates"] += 1
            continue
        stats["inserted"] += 1
        if dry_run:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO transactions (account_id, date, amount_native, currency, merchant_raw, merchant_norm, source, external_id) "
            "VALUES (?,?,?,?,?,?,'csv_backfill',?)",
            (r["account_id"], r["date"], r["amount_native"], r["currency"], r["merchant_raw"], norm, r.get("external_id")),
        )
    if not dry_run:
        conn.commit()
    return stats


def _run_investments(conn, schema, parsed, account, stats, dry_run):
    if not dry_run:
        _ensure_account(conn, schema, account)
    for r in parsed:
        if r.get("_kind") == "cash":
            dup = conn.execute(
                "SELECT 1 FROM transactions WHERE account_id=? AND date=? AND ABS(amount_native-?)<0.005",
                (account, r["date"], r["amount_native"]),
            ).fetchone()
            if dup:
                stats["duplicates"] += 1
                continue
            stats["inserted"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO transactions (account_id, date, amount_native, currency, merchant_raw, merchant_norm, source, external_id) "
                    "VALUES (?,?,?,?,?,?,'csv_backfill',?)",
                    (account, r["date"], r["amount_native"], "USD", r["merchant_raw"], normalize(r["merchant_raw"]), r.get("external_id")),
                )
        else:
            dup = conn.execute(
                "SELECT 1 FROM investment_transactions WHERE account_id=? AND date=? AND ABS(amount_usd-?)<0.005 AND IFNULL(ticker,'')=IFNULL(?,'')",
                (account, r["date"], r["amount_usd"], r.get("ticker")),
            ).fetchone()
            if dup:
                stats["duplicates"] += 1
                continue
            stats["investment_rows"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO investment_transactions (account_id, date, ticker, type, quantity, price, amount_usd, source, external_id) "
                    "VALUES (?,?,?,?,?,?,?,'csv_backfill',?)",
                    (account, r["date"], r.get("ticker"), r["type"], r.get("quantity"), r.get("price"), r["amount_usd"], r.get("external_id")),
                )
    if not dry_run:
        conn.commit()
    return stats
