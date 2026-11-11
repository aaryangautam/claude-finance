"""Balance + holdings snapshot. The one scheduled job. No LLM. Fails loudly (non-zero exit + macOS notification)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date

from .config import Config
from .db import sync_run
from .fx import to_usd_rate
from .ingest.plaid_sync import register_accounts
from .ingest.wise_sync import register_balances
from .plaid_client import PlaidClient, PlaidError
from .wise_client import WiseClient, WiseError


def _store_balance(conn: sqlite3.Connection, account_id: str, today: str, native: float, currency: str) -> None:
    rate = to_usd_rate(conn, currency, today)
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, date, balance_native, currency, balance_usd) VALUES (?,?,?,?,?) "
        "ON CONFLICT(account_id, date) DO UPDATE SET balance_native=excluded.balance_native, balance_usd=excluded.balance_usd, taken_at=datetime('now')",
        (account_id, today, native, currency, round(native * rate, 2)),
    )


def snapshot_plaid(conn: sqlite3.Connection, cfg: Config, client: PlaidClient, today: str) -> list[str]:
    errors = []
    for item in conn.execute("SELECT * FROM plaid_items").fetchall():
        try:
            token = cfg.secret(item["token_env_key"])
            accounts = client.balances(token)
            register_accounts(conn, item["item_id"], item["institution"], accounts)
            for a in accounts:
                row = conn.execute("SELECT id, type FROM accounts WHERE plaid_account_id=?", (a["account_id"],)).fetchone()
                bal = a.get("balances") or {}
                current = bal.get("current")
                if current is None:
                    continue
                # liabilities carry negative balances so v_net_worth can just SUM
                native = -float(current) if row["type"] in ("credit", "loan") else float(current)
                _store_balance(conn, row["id"], today, native, bal.get("iso_currency_code") or "USD")
            products = json.loads(item["products"] or "[]")
            if "investments" in products:
                h = client.holdings(token)
                secs = {s["security_id"]: s for s in h["securities"]}
                for hold in h["holdings"]:
                    row = conn.execute("SELECT id FROM accounts WHERE plaid_account_id=?", (hold["account_id"],)).fetchone()
                    sec = secs.get(hold["security_id"], {})
                    conn.execute(
                        "INSERT INTO holdings_snapshots (account_id, date, ticker, name, quantity, price, value_usd) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(account_id, date, ticker) DO UPDATE SET quantity=excluded.quantity, price=excluded.price, value_usd=excluded.value_usd",
                        (row["id"], today, sec.get("ticker_symbol") or sec.get("name") or hold["security_id"], sec.get("name"),
                         hold.get("quantity"), hold.get("institution_price"), hold.get("institution_value")),
                    )
            conn.commit()
        except PlaidError as e:
            status = "login_required" if e.code == "ITEM_LOGIN_REQUIRED" else "error"
            conn.execute("UPDATE plaid_items SET status=?, last_error=? WHERE item_id=?", (status, str(e), item["item_id"]))
            conn.commit()
            errors.append(f"plaid {item['institution']}: {e}")
    return errors


def snapshot_wise(conn: sqlite3.Connection, cfg: Config, client: WiseClient, today: str) -> list[str]:
    try:
        balances = client.balances()
        by_bal = register_balances(conn, balances)
        for b in balances:
            _store_balance(conn, by_bal[int(b["id"])], today, float(b["amount"]["value"]), b["currency"])
        conn.commit()
        return []
    except WiseError as e:
        return [f"wise: {e}"]


def run(conn: sqlite3.Connection, cfg: Config, notify: bool = True) -> dict:
    today = date.today().isoformat()
    errors: list[str] = []
    with sync_run(conn, "snapshot") as stats:
        if conn.execute("SELECT COUNT(*) FROM plaid_items").fetchone()[0]:
            errors += snapshot_plaid(conn, cfg, PlaidClient(cfg), today)
        if cfg.optional_secret("WISE_TOKEN"):
            errors += snapshot_wise(conn, cfg, WiseClient(cfg), today)
        stats["added"] = conn.execute("SELECT COUNT(*) FROM balance_snapshots WHERE date=?", (today,)).fetchone()[0]
        if errors:
            msg = "; ".join(errors)
            if notify:
                _notify("Finance snapshot failed", msg[:200])
            raise RuntimeError(msg)
    return {"date": today, "accounts": stats["added"]}


def _notify(title: str, text: str) -> None:
    if sys.platform != "darwin":
        return
    safe = text.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'], check=False)
