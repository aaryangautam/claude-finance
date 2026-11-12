"""Store what a Provider returns. The one place provider data meets the database."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from ..db import sync_run, upsert_account
from ..fx import record_provider_rate
from .base import Provider, ProviderAccount


def account_row_id(conn: sqlite3.Connection, provider: str, external_id: str) -> str | None:
    r = conn.execute("SELECT id FROM accounts WHERE provider=? AND provider_account_id=?", (provider, external_id)).fetchone()
    return r["id"] if r else None


def register_accounts(conn: sqlite3.Connection, provider: Provider, accounts: list[ProviderAccount],
                      schema_ids: set[str] | None = None) -> dict[str, str]:
    """Ensure an accounts row per provider account. Returns {external_id: account_id}.

    A new account takes its suggested_id when that id is in the Budget Schema and not bound to another source (a row
    created by a CSV backfill is adopted); otherwise it gets a provisional '<provider>:<external_id>' id to map once
    with `finance accounts map`, exactly like Plaid accounts."""
    out = {}
    for a in accounts:
        aid = account_row_id(conn, provider.name, a.external_id)
        if aid is None:
            free = a.suggested_id and a.suggested_id in (schema_ids or set()) and not conn.execute(
                "SELECT 1 FROM accounts WHERE id=? AND (provider IS NOT NULL OR plaid_account_id IS NOT NULL)",
                (a.suggested_id,)).fetchone()
            aid = a.suggested_id if free else f"{provider.name}:{a.external_id}"
        upsert_account(conn, id=aid, institution=provider.institution or provider.name, provider=provider.name,
                       provider_account_id=a.external_id, name=a.name, type=a.type, currency=a.currency.upper())
        out[a.external_id] = aid
    conn.commit()
    return out


def sync(conn: sqlite3.Connection, provider: Provider, days: int = 90, schema_ids: set[str] | None = None,
         today: date | None = None) -> dict:
    """Pull accounts + the last `days` of transactions. Idempotent: rows upsert on (source=provider name, external_id)."""
    end = today or date.today()
    start = end - timedelta(days=days)
    results: dict[str, int] = {}
    with sync_run(conn, f"provider:{provider.name}") as stats:
        ids = register_accounts(conn, provider, provider.accounts(), schema_ids)
        for t in provider.transactions(start, end):
            aid = ids.get(t.account_external_id)
            if aid is None:
                raise ValueError(f"{provider.name}: transaction {t.external_id} names unknown account {t.account_external_id!r}")
            d = t.date.isoformat() if isinstance(t.date, date) else str(t.date)[:10]
            cur = t.currency.upper()
            rate = t.fx_rate_to_usd if t.fx_rate_to_usd is not None else (1.0 if cur == "USD" else None)
            usd = t.amount_usd if t.amount_usd is not None else (round(t.amount * rate, 2) if rate is not None else None)
            if t.fx_rate_to_usd is not None and cur != "USD":
                record_provider_rate(conn, cur, d, t.fx_rate_to_usd, provider.name)
            conn.execute(
                "INSERT INTO transactions (account_id, date, amount_native, currency, fx_rate_to_usd, amount_usd, merchant_raw, "
                "counterparty, pending, source, external_id, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source, external_id) DO UPDATE SET amount_native=excluded.amount_native, "
                "fx_rate_to_usd=COALESCE(excluded.fx_rate_to_usd, fx_rate_to_usd), amount_usd=COALESCE(excluded.amount_usd, amount_usd), "
                "merchant_raw=excluded.merchant_raw, counterparty=excluded.counterparty, pending=excluded.pending, "
                "raw_json=excluded.raw_json, updated_at=datetime('now')",
                (aid, d, float(t.amount), cur, rate, usd, t.description, t.counterparty, int(t.pending), provider.name,
                 t.external_id, json.dumps(t.raw, default=str) if t.raw else None),
            )
            stats["added"] += 1
            results[aid] = results.get(aid, 0) + 1
        conn.commit()
    return {"accounts": sorted(set(ids.values())), "transactions": results}
