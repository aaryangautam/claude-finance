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
from .plaid_client import PlaidClient, PlaidError
from .providers import Provider
from .providers import load as load_providers
from .providers.sync import register_accounts as register_provider_accounts


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


def snapshot_provider(conn: sqlite3.Connection, provider: Provider, today: str, schema_ids: set[str] | None = None) -> list[str]:
    try:
        ids = register_provider_accounts(conn, provider, provider.accounts(), schema_ids)
        for b in provider.balances():
            _store_balance(conn, ids[b.account_external_id], today, float(b.balance), b.currency.upper())
        for h in provider.holdings():
            conn.execute(
                "INSERT INTO holdings_snapshots (account_id, date, ticker, name, quantity, price, value_usd) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id, date, ticker) DO UPDATE SET quantity=excluded.quantity, price=excluded.price, value_usd=excluded.value_usd",
                (ids[h.account_external_id], today, h.ticker, h.name, h.quantity, h.price, h.value_usd),
            )
        conn.commit()
        return []
    except Exception as e:  # one provider failing must not hide the others; run() raises with all of them
        return [f"{provider.name}: {type(e).__name__}: {e}"]


def run(conn: sqlite3.Connection, cfg: Config, notify: bool = True) -> dict:
    today = date.today().isoformat()
    errors: list[str] = []
    with sync_run(conn, "snapshot") as stats:
        if conn.execute("SELECT COUNT(*) FROM plaid_items").fetchone()[0]:
            errors += snapshot_plaid(conn, cfg, PlaidClient(cfg), today)
        schema_ids = _schema_ids(cfg)
        try:
            provs = load_providers(cfg)
        except Exception as e:
            provs, errors = [], errors + [f"providers: {e}"]
        for p in provs:
            errors += snapshot_provider(conn, p, today, schema_ids)
        stats["added"] = conn.execute("SELECT COUNT(*) FROM balance_snapshots WHERE date=?", (today,)).fetchone()[0]
        if errors:
            msg = "; ".join(errors)
            if notify:
                _notify("Finance snapshot failed", msg[:200])
            raise RuntimeError(msg)
    return {"date": today, "accounts": stats["added"]}


def _schema_ids(cfg: Config) -> set[str] | None:
    from .vault import load_budget_schema

    try:
        return set(load_budget_schema(cfg.budget_schema_path).account_ids)
    except Exception:
        return None


def _notify(title: str, text: str) -> None:
    if sys.platform != "darwin":
        return
    safe = text.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'], check=False)
