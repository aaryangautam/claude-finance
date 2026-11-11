"""Plaid sync against a fake client: added/modified/removed, cursor persisted per page, sign flipped."""

import json
from datetime import date

from finance.ingest import plaid_sync


class FakePlaid:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def transactions_sync(self, token, cursor):
        self.calls.append(cursor)
        return self.pages[len(self.calls) - 1]


ACCT = {"account_id": "acc1", "name": "EVERYDAY CHECKING", "type": "depository", "subtype": "checking",
        "balances": {"iso_currency_code": "USD", "current": 1200.0}}


def _tx(tid, amount, name, d="2026-08-01", pending=False, pfc=("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")):
    return {"transaction_id": tid, "account_id": "acc1", "amount": amount, "date": date.fromisoformat(d),
            "name": name, "merchant_name": None, "iso_currency_code": "USD", "pending": pending,
            "personal_finance_category": {"primary": pfc[0], "detailed": pfc[1]}}


def _item(conn):
    conn.execute("INSERT INTO plaid_items (item_id, institution, token_env_key, products) VALUES ('it1','MyBank','PLAID_TOKEN_MYBANK','[\"transactions\"]')")
    conn.commit()
    return conn.execute("SELECT * FROM plaid_items").fetchone()


def test_sync_pages_and_sign(cfg, conn):
    cfg.env["PLAID_TOKEN_MYBANK"] = "tok"
    item = _item(conn)
    pages = [
        {"accounts": [ACCT], "added": [_tx("t1", 45.20, "REWE"), _tx("t2", -3000.0, "ACME CORP PAYROLL")], "modified": [], "removed": [],
         "next_cursor": "c1", "has_more": True},
        {"accounts": [ACCT], "added": [_tx("t3", 9.99, "NETFLIX", pending=True)], "modified": [_tx("t1", 46.00, "REWE SAGT DANKE")],
         "removed": [{"transaction_id": "t2"}], "next_cursor": "c2", "has_more": False},
    ]
    fake = FakePlaid(pages)
    stats = plaid_sync.sync_item(conn, fake, cfg, item)
    assert stats == {"added": 3, "modified": 1, "removed": 1}
    assert fake.calls == [None, "c1"]
    assert conn.execute("SELECT cursor, status FROM plaid_items").fetchone()[0] == "c2"
    rows = {r["external_id"]: r for r in conn.execute("SELECT * FROM transactions")}
    assert set(rows) == {"t1", "t3"}
    assert rows["t1"]["amount_native"] == -46.00  # Plaid positive outflow -> negative
    assert rows["t1"]["merchant_raw"] == "REWE SAGT DANKE"
    assert rows["t1"]["plaid_category"] == "FOOD_AND_DRINK/FOOD_AND_DRINK_GROCERIES"
    assert rows["t3"]["pending"] == 1
    acct = conn.execute("SELECT * FROM accounts WHERE plaid_account_id='acc1'").fetchone()
    assert acct["id"] == "plaid:acc1" and acct["type"] == "checking"


def test_sync_is_idempotent(cfg, conn):
    cfg.env["PLAID_TOKEN_MYBANK"] = "tok"
    item = _item(conn)
    page = {"accounts": [ACCT], "added": [_tx("t1", 10.0, "X")], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}
    plaid_sync.sync_item(conn, FakePlaid([page]), cfg, item)
    item = conn.execute("SELECT * FROM plaid_items").fetchone()
    plaid_sync.sync_item(conn, FakePlaid([page]), cfg, item)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
