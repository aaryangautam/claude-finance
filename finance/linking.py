"""Exchange a Plaid public token and register the Item: token -> .env, item -> plaid_items, accounts -> provisional rows."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .db import open_db
from .plaid_client import PlaidClient


def token_key(institution: str) -> str:
    return "PLAID_TOKEN_" + re.sub(r"[^A-Z0-9]", "", institution.upper())


def write_env_token(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def register_item(cfg: Config, client: PlaidClient, public_token: str, institution: str) -> dict:
    from .ingest.plaid_sync import register_accounts

    access_token, item_id = client.exchange_public_token(public_token)
    item = client.item(access_token)["item"]
    key = token_key(institution)
    write_env_token(cfg.root / ".env", key, access_token)
    cfg.env[key] = access_token
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO plaid_items (item_id, institution, institution_id, token_env_key, products, status) VALUES (?,?,?,?,?,'ok') "
        "ON CONFLICT(item_id) DO UPDATE SET status='ok', last_error=NULL, products=excluded.products",
        (item_id, institution, item.get("institution_id"), key, json.dumps(item.get("products") or item.get("billed_products") or [])),
    )
    conn.commit()
    # pull accounts right away so they show up for mapping
    accounts = register_accounts(conn, item_id, institution, client.balances(access_token))
    return {"ok": True, "item_id": item_id, "products": item.get("products"), "accounts": accounts}
