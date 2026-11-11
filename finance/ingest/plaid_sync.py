"""Plaid ingestion: /transactions/sync with cursors, balances, holdings, investment transactions.

Sign convention: Plaid reports outflows as positive. We store amount > 0 = money INTO the account, so every Plaid
amount is negated at this boundary and nowhere else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from ..config import Config
from ..db import now_iso, sync_run, upsert_account
from ..plaid_client import PlaidClient, PlaidError

PLAID_TYPE_MAP = {
    ("depository", "checking"): "checking",
    ("depository", "savings"): "savings",
    ("credit", "credit card"): "credit",
    ("loan", "student"): "loan",
    ("loan", "loan"): "loan",
    ("loan", "consumer"): "loan",
    ("investment", "ira"): "ira",
    ("investment", "roth"): "ira",
    ("investment", "brokerage"): "brokerage",
}


def account_type(acct: dict) -> str:
    t, st = (acct.get("type") or "").lower(), (acct.get("subtype") or "").lower()
    if (t, st) in PLAID_TYPE_MAP:
        return PLAID_TYPE_MAP[(t, st)]
    if t == "loan":
        return "loan"
    if t == "credit":
        return "credit"
    if t == "investment":
        return "ira" if "roth" in st or "ira" in st else "brokerage"
    if t == "depository":
        return "savings" if "sav" in st else "checking"
    return t or "other"


def _account_row_id(conn: sqlite3.Connection, plaid_account_id: str) -> str | None:
    r = conn.execute("SELECT id FROM accounts WHERE plaid_account_id=?", (plaid_account_id,)).fetchone()
    return r["id"] if r else None


def register_accounts(conn: sqlite3.Connection, item_id: str, institution: str, accounts: list[dict]) -> list[str]:
    """Create provisional 'plaid:<id>' rows for unseen accounts. Mapping to schema ids happens once via `accounts map`."""
    ids = []
    for a in accounts:
        existing = _account_row_id(conn, a["account_id"])
        aid = existing or f"plaid:{a['account_id']}"
        upsert_account(
            conn,
            id=aid,
            institution=institution,
            plaid_item_id=item_id,
            plaid_account_id=a["account_id"],
            name=a.get("official_name") or a.get("name"),
            type=account_type(a),
            currency=(a.get("balances") or {}).get("iso_currency_code") or "USD",
        )
        ids.append(aid)
    conn.commit()
    return ids


def _tx_row(conn: sqlite3.Connection, t: dict) -> dict | None:
    aid = _account_row_id(conn, t["account_id"])
    if not aid:
        return None
    pfc = t.get("personal_finance_category") or {}
    plaid_cat = None
    if pfc:
        plaid_cat = f"{pfc.get('primary')}/{pfc.get('detailed')}"
    d = t.get("date")
    return dict(
        account_id=aid,
        date=d.isoformat() if hasattr(d, "isoformat") else str(d),
        amount_native=-float(t["amount"]),
        currency=t.get("iso_currency_code") or "USD",
        merchant_raw=t.get("merchant_name") or t.get("original_description") or t.get("name"),
        plaid_category=plaid_cat,
        pending=1 if t.get("pending") else 0,
        source="plaid",
        external_id=t["transaction_id"],
        raw_json=json.dumps(t, default=str),
    )


def sync_item(conn: sqlite3.Connection, client: PlaidClient, cfg: Config, item: sqlite3.Row) -> dict:
    """Full /transactions/sync loop for one item. Persists the cursor only after each page is committed."""
    token = cfg.secret(item["token_env_key"])
    stats_out = {}
    with sync_run(conn, f"plaid:{item['institution']}") as stats:
        cursor = item["cursor"]
        try:
            while True:
                page = client.transactions_sync(token, cursor)
                register_accounts(conn, item["item_id"], item["institution"], page.get("accounts", []))
                for t in page["added"]:
                    row = _tx_row(conn, t)
                    if row is None:
                        continue
                    cols = ", ".join(row)
                    conn.execute(
                        f"INSERT INTO transactions ({cols}) VALUES ({', '.join('?' for _ in row)}) "
                        "ON CONFLICT(source, external_id) DO UPDATE SET amount_native=excluded.amount_native, "
                        "merchant_raw=excluded.merchant_raw, plaid_category=excluded.plaid_category, pending=excluded.pending, "
                        "raw_json=excluded.raw_json, updated_at=datetime('now')",
                        tuple(row.values()),
                    )
                    stats["added"] += 1
                for t in page["modified"]:
                    row = _tx_row(conn, t)
                    if row is None:
                        continue
                    conn.execute(
                        "UPDATE transactions SET date=?, amount_native=?, currency=?, merchant_raw=?, plaid_category=?, pending=?, "
                        "raw_json=?, amount_usd=NULL, updated_at=datetime('now') WHERE source='plaid' AND external_id=?",
                        (row["date"], row["amount_native"], row["currency"], row["merchant_raw"], row["plaid_category"],
                         row["pending"], row["raw_json"], row["external_id"]),
                    )
                    stats["modified"] += 1
                for t in page["removed"]:
                    conn.execute("DELETE FROM transactions WHERE source='plaid' AND external_id=?", (t["transaction_id"],))
                    stats["removed"] += 1
                cursor = page["next_cursor"]
                conn.execute("UPDATE plaid_items SET cursor=?, status='ok', last_error=NULL WHERE item_id=?", (cursor, item["item_id"]))
                conn.commit()
                if not page["has_more"]:
                    break
            conn.execute("UPDATE plaid_items SET last_synced=? WHERE item_id=?", (now_iso(), item["item_id"]))
            conn.commit()
        except PlaidError as e:
            status = "login_required" if e.code == "ITEM_LOGIN_REQUIRED" else "error"
            conn.execute("UPDATE plaid_items SET status=?, last_error=? WHERE item_id=?", (status, str(e), item["item_id"]))
            conn.commit()
            raise
        stats_out = dict(stats)
    return stats_out


def sync_investments(conn: sqlite3.Connection, client: PlaidClient, cfg: Config, item: sqlite3.Row, since: date | None = None) -> dict:
    """Investment transactions for one item (only if its products include investments)."""
    products = json.loads(item["products"] or "[]")
    if "investments" not in products:
        return {"skipped": True}
    token = cfg.secret(item["token_env_key"])
    start = since or (date.today() - timedelta(days=730))
    with sync_run(conn, f"plaid-investments:{item['institution']}") as stats:
        data = client.investment_transactions(token, start, date.today())
        register_accounts(conn, item["item_id"], item["institution"], data["accounts"])
        secs = {s["security_id"]: s for s in data["securities"]}
        for it in data["investment_transactions"]:
            aid = _account_row_id(conn, it["account_id"])
            if not aid:
                continue
            sec = secs.get(it.get("security_id") or "", {})
            typ = (it.get("type") or "other").lower()
            sub = (it.get("subtype") or "").lower()
            if typ == "cash" and "dividend" in sub:
                typ = "dividend"
            elif typ == "cash" and sub in ("contribution", "deposit"):
                typ = "contribution"
            elif typ == "cash" and sub in ("withdrawal",):
                typ = "withdrawal"
            elif typ == "transfer":
                typ = "contribution" if float(it["amount"]) < 0 else "withdrawal"
            d = it["date"]
            conn.execute(
                "INSERT INTO investment_transactions (account_id, date, ticker, type, quantity, price, amount_usd, source, external_id, raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, external_id) DO UPDATE SET amount_usd=excluded.amount_usd, "
                "quantity=excluded.quantity, price=excluded.price, type=excluded.type",
                (aid, d.isoformat() if hasattr(d, "isoformat") else str(d), sec.get("ticker_symbol"), typ,
                 it.get("quantity"), it.get("price"), -float(it["amount"]), "plaid", it["investment_transaction_id"],
                 json.dumps(it, default=str)),
            )
            stats["added"] += 1
        conn.commit()
        return dict(stats)


def sync_all(conn: sqlite3.Connection, cfg: Config, client: PlaidClient | None = None, only: str | None = None) -> dict[str, dict]:
    client = client or PlaidClient(cfg)
    items = conn.execute("SELECT * FROM plaid_items ORDER BY institution").fetchall()
    if not items:
        raise RuntimeError("no Plaid items linked yet. Run `finance app` and link accounts.")
    results: dict[str, dict] = {}
    errors = []
    for item in items:
        if only and item["institution"].lower() != only.lower():
            continue
        try:
            results[item["institution"]] = sync_item(conn, client, cfg, item)
            inv = sync_investments(conn, client, cfg, item)
            if not inv.get("skipped"):
                results[item["institution"]]["investments"] = inv
        except PlaidError as e:
            errors.append(f"{item['institution']}: {e}")
            results[item["institution"]] = {"error": str(e)}
    if errors:
        raise RuntimeError("Plaid sync had errors:\n  " + "\n  ".join(errors))
    return results
