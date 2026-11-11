"""Wise ingestion: balances + balance statements per currency. Falls back to CSV export via csv_backfill.wise.

Wise statement lines already carry sign (DEBIT/CREDIT). Conversions and USD-funded transfers include the source
amount, which we use as the USD value so the hub-account leg matches exactly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import sync_run, upsert_account
from ..fx import record_wise_rate
from ..wise_client import WiseClient


def register_balances(conn: sqlite3.Connection, balances: list[dict]) -> dict[int, str]:
    """Ensure an accounts row per Wise balance. Returns {balance_id: account_id}. Default ids wise_usd / wise_eur."""
    out = {}
    for b in balances:
        cur = b["currency"]
        bid = str(b["id"])
        existing = conn.execute("SELECT id FROM accounts WHERE wise_balance_id=?", (bid,)).fetchone()
        aid = existing["id"] if existing else f"wise_{cur.lower()}"
        upsert_account(
            conn, id=aid, institution="Wise", wise_balance_id=bid, name=f"Wise {cur}", type=f"wise_{cur.lower()}", currency=cur
        )
        out[int(b["id"])] = aid
    conn.commit()
    return out


def _usd_value(line: dict, currency: str) -> tuple[float | None, float | None]:
    """(amount_usd, rate_to_usd) from a statement line's transfer details when a USD leg is present."""
    d = line.get("details") or {}
    src = d.get("sourceAmount") or {}
    tgt = d.get("targetAmount") or {}
    amt = float(line["amount"]["value"])
    if currency == "USD":
        return amt, 1.0
    if src.get("currency") == "USD" and tgt.get("currency") == currency and float(tgt.get("value") or 0):
        rate = float(src["value"]) / float(tgt["value"])
        return round(amt * rate, 2), rate
    if tgt.get("currency") == "USD" and src.get("currency") == currency and float(src.get("value") or 0):
        rate = float(tgt["value"]) / float(src["value"])
        return round(amt * rate, 2), rate
    return None, None


def ingest_statement(conn: sqlite3.Connection, account_id: str, currency: str, balance_id: int, stmt: dict, cfg: Config) -> dict:
    stats = {"added": 0}
    senders = cfg.reimbursement_senders
    for line in stmt.get("transactions", []):
        amt = float(line["amount"]["value"])  # Wise statement amounts are signed: DEBIT negative
        if line.get("type") == "DEBIT" and amt > 0:
            amt = -amt
        d = line["date"][:10]
        det = line.get("details") or {}
        desc = det.get("description") or det.get("merchant", {}).get("name") or line.get("referenceNumber")
        sender = (det.get("senderName") or "").lower()
        is_reimb = 1 if amt > 0 and senders and any(s in sender for s in senders) else 0
        usd, rate = _usd_value(line, currency)
        if rate and currency != "USD":
            record_wise_rate(conn, currency, d, rate)
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount_native, currency, fx_rate_to_usd, amount_usd, merchant_raw, "
            "is_reimbursement, category, category_source, category_confidence, source, external_id, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, external_id) DO UPDATE SET amount_native=excluded.amount_native, "
            "merchant_raw=excluded.merchant_raw, raw_json=excluded.raw_json, updated_at=datetime('now')",
            (account_id, d, amt, currency, rate, usd, desc, is_reimb,
             cfg.reimbursement_category if is_reimb else None, "rule" if is_reimb else None, 1.0 if is_reimb else None,
             "wise", f"{balance_id}:{line.get('referenceNumber')}", json.dumps(line, default=str)),
        )
        stats["added"] += 1
    conn.commit()
    return stats


def sync(conn: sqlite3.Connection, cfg: Config, client: WiseClient | None = None, days: int = 90) -> dict:
    client = client or WiseClient(cfg)
    with sync_run(conn, "wise") as stats:
        balances = client.balances()
        acct_by_bal = register_balances(conn, balances)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        results = {}
        for b in balances:
            aid = acct_by_bal[int(b["id"])]
            stmt = client.statement(int(b["id"]), b["currency"], start, end)
            r = ingest_statement(conn, aid, b["currency"], int(b["id"]), stmt, cfg)
            stats["added"] += r["added"]
            results[aid] = r
    return results
