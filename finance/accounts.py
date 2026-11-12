"""Map provisional Plaid accounts ('plaid:<id>') to Budget Schema account ids. Done once per account."""

from __future__ import annotations

import sqlite3

from .vault import BudgetSchema

CHILD_TABLES = ("transactions", "balance_snapshots", "holdings_snapshots", "investment_transactions")


def map_account(conn: sqlite3.Connection, schema: BudgetSchema, old_id: str, new_id: str) -> None:
    if new_id not in schema.account_ids:
        raise ValueError(f"{new_id!r} is not in the Budget Schema accounts: {schema.account_ids}")
    old = conn.execute("SELECT * FROM accounts WHERE id=?", (old_id,)).fetchone()
    if not old:
        raise ValueError(f"no account {old_id!r}")
    if conn.execute("SELECT 1 FROM accounts WHERE id=?", (new_id,)).fetchone():
        raise ValueError(f"{new_id!r} already exists; unmap it first")
    sa = schema.account(new_id)
    row = dict(old)
    row["id"] = new_id
    row["role"] = sa.role
    conn.execute("BEGIN")
    try:
        # free the UNIQUE provider ids on the old row, insert the new row so FK-referencing children can move, drop the old one
        conn.execute("UPDATE accounts SET plaid_account_id=NULL, wise_balance_id=NULL, provider_account_id=NULL WHERE id=?", (old_id,))
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        for t in CHILD_TABLES:
            conn.execute(f"UPDATE {t} SET account_id=? WHERE account_id=?", (new_id, old_id))
        conn.execute("DELETE FROM accounts WHERE id=?", (old_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def apply_roles(conn: sqlite3.Connection, schema: BudgetSchema) -> None:
    """Copy roles from the schema onto any mapped account rows missing them."""
    for a in schema.accounts:
        conn.execute("UPDATE accounts SET role=? WHERE id=? AND (role IS NULL OR role != ?)", (a.role, a.id, a.role))
    conn.commit()


def is_provisional(account_id: str) -> bool:
    """'plaid:<id>' or '<provider>:<id>': not yet mapped to a Budget Schema id (schema ids never contain ':')."""
    return ":" in account_id


def unmapped(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return [r for r in conn.execute("SELECT * FROM accounts ORDER BY institution").fetchall() if is_provisional(r["id"])]
