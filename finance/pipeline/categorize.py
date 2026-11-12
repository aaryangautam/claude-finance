"""Categorization pipeline. Order: matcher -> normalize -> rules -> merchant_map -> plaid seed -> handoff to Claude Code.

The LLM step runs inside Claude Code: `run` writes work/uncategorized.json, the finance-categorize skill writes
work/categorized.json, `apply` validates and writes it back. No API key, nothing unattended.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..fx import fill_missing_usd
from ..vault import BudgetSchema
from . import matcher, normalize


def apply_renames(conn: sqlite3.Connection, schema: BudgetSchema) -> int:
    n = 0
    for old, new in schema.renames.items():
        n += conn.execute("UPDATE merchant_map SET category=? WHERE category=?", (new, old)).rowcount
        n += conn.execute("UPDATE transactions SET category=? WHERE category=?", (new, old)).rowcount
    conn.commit()
    return n


def _not_per_txn(cfg: Config | None) -> tuple[str, tuple]:
    """SQL fragment excluding merchants whose category varies per transaction (e.g. Zelle)."""
    pats = cfg.per_transaction_patterns if cfg else []
    if not pats:
        return "", ()
    return " AND NOT (" + " OR ".join("upper(COALESCE(merchant_raw,'')) LIKE ?" for _ in pats) + ")", tuple(f"%{p}%" for p in pats)


def apply_rules(conn: sqlite3.Connection, schema: BudgetSchema, cfg: Config) -> int:
    """Deterministic categorization that doesn't need the merchant map."""
    n = 0
    hub = schema.accounts_by_role("hub")[0].id
    if cfg.income_category in schema.category_ids:
        for pat in cfg.payroll_patterns:
            n += conn.execute(
                "UPDATE transactions SET category=?, category_source='rule', category_confidence=1.0 "
                "WHERE account_id=? AND amount_usd>0 AND category IS NULL AND is_transfer=0 AND upper(merchant_raw) LIKE ?",
                (cfg.income_category, hub, f"%{pat}%"),
            ).rowcount
    # Non-pay income on the hub (tax refunds, misc pay).
    oic = cfg.other_income_category
    if oic and oic in schema.category_ids:
        for pat in cfg.other_income_patterns:
            n += conn.execute(
                "UPDATE transactions SET category=?, category_source='rule', category_confidence=1.0 "
                "WHERE account_id=? AND amount_usd>0 AND category IS NULL AND is_transfer=0 AND upper(merchant_raw) LIKE ?",
                (oic, hub, f"%{pat}%"),
            ).rowcount
    # Zelle paybacks from friends: reimbursements netted against a category, never income. Runs before the generic
    # sender rule so a person listed in both keeps their Zelle mapping.
    for sender, cat in cfg.zelle_reimbursements.items():
        if cat is not None and cat not in schema.category_ids:
            raise ValueError(f"config [reimbursements.zelle] {sender!r}: unknown category {cat!r}")
        n += conn.execute(
            "UPDATE transactions SET is_reimbursement=1, category=?, category_source=?, category_confidence=?, "
            "needs_review=?, review_reason=? "
            "WHERE account_id=? AND amount_usd>0 AND is_transfer=0 AND category IS NULL "
            "AND upper(merchant_raw) LIKE '%ZELLE%' AND lower(merchant_raw) LIKE ?",
            (cat, "rule" if cat else None, 1.0 if cat else None, 0 if cat else 1,
             None if cat else "zelle payback: pick the category it nets against", hub, f"%{sender}%"),
        ).rowcount
    # Paybacks from listed senders on any account (a Wise transfer, a provider's counterparty, a bank's "from <name>"
    # description): re-applied every run, so rows imported before a sender was listed get flagged too.
    for sender, cat in cfg.sender_reimbursements.items():
        if cat not in schema.category_ids:
            raise ValueError(f"config [reimbursements] sender {sender!r}: category {cat!r} is not in the Budget Schema")
        n += conn.execute(
            "UPDATE transactions SET is_reimbursement=1, category=?, category_source='rule', category_confidence=1.0 "
            "WHERE amount_native>0 AND is_transfer=0 AND category IS NULL AND is_reimbursement=0 "
            "AND lower(COALESCE(counterparty,'') || ' ' || COALESCE(merchant_raw,'')) LIKE ?",
            (cat, f"%{sender}%"),
        ).rowcount
    conn.commit()
    return n


def apply_merchant_map(conn: sqlite3.Connection, cfg: Config | None = None) -> int:
    skip, params = _not_per_txn(cfg)
    cur = conn.execute(
        """UPDATE transactions SET
             category = (SELECT category FROM merchant_map m WHERE m.merchant_norm = transactions.merchant_norm),
             subcategory = (SELECT subcategory FROM merchant_map m WHERE m.merchant_norm = transactions.merchant_norm),
             category_source = (SELECT source FROM merchant_map m WHERE m.merchant_norm = transactions.merchant_norm),
             category_confidence = (SELECT confidence FROM merchant_map m WHERE m.merchant_norm = transactions.merchant_norm)
           WHERE is_transfer=0 AND merchant_norm IN (SELECT merchant_norm FROM merchant_map)
             AND (category IS NULL OR category_source IN ('plaid','llm'))""" + skip,
        params,
    )
    conn.commit()
    return cur.rowcount


def seed_from_plaid(conn: sqlite3.Connection, schema: BudgetSchema, cfg: Config) -> int:
    """First-pass seed of merchant_map from Plaid's category, low confidence. Never overwrites an existing entry."""
    primary, detailed = cfg.plaid_seed
    valid = set(schema.category_ids)
    bad = {v for v in list(primary.values()) + list(detailed.values()) if v not in valid}
    if bad:
        print(f"  warning: plaid_seed ids not in schema, skipped: {sorted(bad)}")
    skip, params = _not_per_txn(cfg)
    rows = conn.execute(
        """SELECT merchant_norm, plaid_category, COUNT(*) c FROM transactions
           WHERE is_transfer=0 AND category IS NULL AND merchant_norm != '' AND plaid_category IS NOT NULL
             AND merchant_norm NOT IN (SELECT merchant_norm FROM merchant_map)""" + skip + """
           GROUP BY merchant_norm, plaid_category ORDER BY c DESC""",
        params,
    ).fetchall()
    n = 0
    seen = set()
    for r in rows:
        if r["merchant_norm"] in seen:
            continue
        prim, _, det = r["plaid_category"].partition("/")
        cat = detailed.get(det) or primary.get(prim)
        if not cat or cat not in valid:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO merchant_map (merchant_norm, category, confidence, source) VALUES (?, ?, 0.5, 'plaid')",
            (r["merchant_norm"], cat),
        )
        seen.add(r["merchant_norm"])
        n += 1
    conn.commit()
    return n


def write_handoff(conn: sqlite3.Connection, schema: BudgetSchema, cfg: Config) -> tuple[Path, int]:
    """Unknown merchants, plus merchants carrying only a low-confidence Plaid seed, -> work/uncategorized.json for the
    Claude Code categorization skill. Once Claude has looked at a seeded merchant its map entry becomes source=llm,
    so each merchant is sent at most once."""
    skip, params = _not_per_txn(cfg)
    rows = conn.execute(
        """SELECT merchant_norm, COUNT(*) n, ROUND(SUM(-amount_usd),2) total_usd, MIN(date) first, MAX(date) last,
                  GROUP_CONCAT(DISTINCT merchant_raw) raws, GROUP_CONCAT(DISTINCT account_id) accounts,
                  GROUP_CONCAT(DISTINCT plaid_category) plaid_cats
           FROM transactions
           WHERE is_transfer=0 AND merchant_norm != '' AND pending=0
             AND (category IS NULL OR category_source='plaid')
             AND (merchant_norm NOT IN (SELECT merchant_norm FROM merchant_map)
                  OR merchant_norm IN (SELECT merchant_norm FROM merchant_map WHERE source='plaid'))""" + skip + """
           GROUP BY merchant_norm ORDER BY n DESC, ABS(total_usd) DESC""",
        params,
    ).fetchall()
    seeded = {r[0]: r[1] for r in conn.execute("SELECT merchant_norm, category FROM merchant_map WHERE source='plaid'")}
    cats = [
        {"id": c.id, "group": c.group, "type": c.type, **({"note": c.note} if c.note else {})}
        for c in schema.categories
        if c.group != "income" and c.type != "allocation"
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Assign each merchant exactly one category id from allowed_categories. Optional short subcategory. "
            "confidence 0..1. If you genuinely cannot tell, use the lowest-confidence plausible category; below the "
            f"threshold ({cfg.unclear_threshold}) it goes to the unclear queue for the user. Never invent an id."
        ),
        "allowed_categories": cats,
        "merchants": [
            {
                "merchant_norm": r["merchant_norm"],
                "count": r["n"],
                "total_usd": r["total_usd"],
                "first": r["first"],
                "last": r["last"],
                "raw_examples": (r["raws"] or "").split(",")[:3],
                "accounts": (r["accounts"] or "").split(","),
                "plaid_categories": (r["plaid_cats"] or "").split(",") if r["plaid_cats"] else [],
                **({"seeded_guess": seeded[r["merchant_norm"]]} if r["merchant_norm"] in seeded else {}),
            }
            for r in rows
        ],
    }
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.work_dir / "uncategorized.json"
    out.write_text(json.dumps(payload, indent=2))
    return out, len(rows)


def run(conn: sqlite3.Connection, schema: BudgetSchema, cfg: Config, rematch: bool = False) -> dict:
    stats: dict = {}
    if rematch:
        matcher.reset(conn)
    stats["fx_filled"] = fill_missing_usd(conn)
    stats["normalized"] = normalize.normalize_all(conn)
    stats["renamed"] = apply_renames(conn, schema)
    stats["matcher"] = matcher.run(conn, schema, cfg.matcher_window_days, cfg.matcher_tolerance_usd, cfg.transfer_patterns,
                                     exclude_patterns=cfg.per_transaction_patterns)
    stats["rules"] = apply_rules(conn, schema, cfg)
    stats["plaid_seeded"] = seed_from_plaid(conn, schema, cfg)
    stats["mapped"] = apply_merchant_map(conn, cfg)
    path, n = write_handoff(conn, schema, cfg)
    stats["handoff"] = {"path": str(path), "unknown_merchants": n}
    stats["unclear"] = unclear_count(conn, cfg)
    return stats


def apply(conn: sqlite3.Connection, schema: BudgetSchema, cfg: Config, path: Path | None = None) -> dict:
    """Read work/categorized.json, validate every id, write merchant_map (source=llm), fill transactions."""
    path = path or cfg.work_dir / "categorized.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run the finance-categorize skill first")
    data = json.loads(path.read_text())
    entries = data["merchants"] if isinstance(data, dict) else data
    valid = set(schema.category_ids)
    errors = []
    for e in entries:
        if e.get("category") not in valid:
            errors.append(f"{e.get('merchant_norm')!r}: unknown category {e.get('category')!r}")
        try:
            float(e.get("confidence", 0))
        except (TypeError, ValueError):
            errors.append(f"{e.get('merchant_norm')!r}: bad confidence {e.get('confidence')!r}")
    if errors:
        raise ValueError("categorized.json rejected:\n  " + "\n  ".join(errors))
    n = 0
    for e in entries:
        # user entries win permanently
        conn.execute(
            "INSERT INTO merchant_map (merchant_norm, category, subcategory, confidence, source, updated_at) VALUES (?,?,?,?,'llm',datetime('now')) "
            "ON CONFLICT(merchant_norm) DO UPDATE SET category=excluded.category, subcategory=excluded.subcategory, "
            "confidence=excluded.confidence, source='llm', updated_at=datetime('now') WHERE merchant_map.source != 'user'",
            (e["merchant_norm"], e["category"], e.get("subcategory"), float(e["confidence"])),
        )
        n += 1
    conn.commit()
    mapped = apply_merchant_map(conn, cfg)
    path.rename(path.with_name(f"categorized.{datetime.now().strftime('%Y%m%d-%H%M%S')}.applied.json"))
    return {"merchants": n, "transactions_mapped": mapped, "unclear": unclear_count(conn, cfg)}


def correct(conn: sqlite3.Connection, schema: BudgetSchema, merchant_norm: str, category: str, subcategory: str | None = None) -> int:
    """User correction. Wins permanently; re-applies to every transaction with that merchant."""
    if category not in schema.category_ids:
        raise ValueError(f"unknown category {category!r}; allowed: {schema.category_ids}")
    conn.execute(
        "INSERT INTO merchant_map (merchant_norm, category, subcategory, confidence, source, updated_at) VALUES (?,?,?,1.0,'user',datetime('now')) "
        "ON CONFLICT(merchant_norm) DO UPDATE SET category=excluded.category, subcategory=excluded.subcategory, confidence=1.0, source='user', updated_at=datetime('now')",
        (merchant_norm, category, subcategory),
    )
    cur = conn.execute(
        "UPDATE transactions SET category=?, subcategory=?, category_source='user', category_confidence=1.0, needs_review=0, review_reason=NULL "
        "WHERE merchant_norm=? AND is_transfer=0 AND COALESCE(category_source,'') != 'user_txn'",
        (category, subcategory, merchant_norm),
    )
    conn.commit()
    return cur.rowcount


def correct_txn(conn: sqlite3.Connection, schema: BudgetSchema, txn_id: int, category: str, subcategory: str | None = None) -> None:
    """User correction for one transaction only (split paybacks, Zelle). Never touches merchant_map, and a later
    merchant-level correction doesn't override it."""
    if category not in schema.category_ids:
        raise ValueError(f"unknown category {category!r}; allowed: {schema.category_ids}")
    row = conn.execute("SELECT is_transfer FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if row is None:
        raise ValueError(f"no transaction with id {txn_id}")
    if row["is_transfer"]:
        raise ValueError(f"transaction {txn_id} is an internal transfer; it has no spending category")
    conn.execute(
        "UPDATE transactions SET category=?, subcategory=?, category_source='user_txn', category_confidence=1.0, "
        "needs_review=0, review_reason=NULL, updated_at=datetime('now') WHERE id=?",
        (category, subcategory, txn_id),
    )
    conn.commit()


def mark_transfer(conn: sqlite3.Connection, txn_id: int) -> None:
    """User says this one transaction is money moving between own accounts (e.g. a brokerage withdrawal, a loan
    disbursement, a transfer from an old bank): hide it from spending and income. Survives `categorize run --rematch`."""
    row = conn.execute("SELECT id FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if row is None:
        raise ValueError(f"no transaction with id {txn_id}")
    conn.execute(
        "UPDATE transactions SET is_transfer=1, transfer_group_id='manual', category=NULL, subcategory=NULL, "
        "category_source=NULL, category_confidence=NULL, needs_review=0, review_reason=NULL, updated_at=datetime('now') WHERE id=?",
        (txn_id,),
    )
    conn.commit()


def unclear_count(conn: sqlite3.Connection, cfg: Config) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT merchant_norm) FROM transactions WHERE is_transfer=0 AND pending=0 AND "
        "(category IS NULL OR (category_source IN ('llm','plaid') AND category_confidence < ?))",
        (cfg.unclear_threshold,),
    ).fetchone()[0]


def unclear_queue(conn: sqlite3.Connection, cfg: Config, month: str | None = None) -> list[sqlite3.Row]:
    where = "AND date LIKE ?" if month else ""
    params: tuple = (cfg.unclear_threshold,) + ((f"{month}%",) if month else ())
    return conn.execute(
        f"""SELECT merchant_norm, category, category_source, category_confidence, COUNT(*) n, ROUND(SUM(-amount_usd),2) total_usd,
                   MAX(date) last, GROUP_CONCAT(DISTINCT review_reason) reasons
            FROM transactions
            WHERE is_transfer=0 AND pending=0 AND merchant_norm != ''
              AND (category IS NULL OR (category_source IN ('llm','plaid') AND category_confidence < ?) OR needs_review=1) {where}
            GROUP BY merchant_norm ORDER BY n DESC""",
        params,
    ).fetchall()


def unclear_transactions(conn: sqlite3.Connection, cfg: Config, month: str | None = None) -> list[sqlite3.Row]:
    """Per-transaction merchants (Zelle) still waiting for a category, one row each, with the id to correct."""
    pats = cfg.per_transaction_patterns
    if not pats:
        return []
    like = " OR ".join("upper(COALESCE(merchant_raw,'')) LIKE ?" for _ in pats)
    where = "AND date LIKE ?" if month else ""
    params = tuple(f"%{p}%" for p in pats) + ((f"{month}%",) if month else ())
    return conn.execute(
        f"""SELECT id, date, account_id, ROUND(amount_usd,2) amount_usd, merchant_raw, is_reimbursement, review_reason
            FROM transactions
            WHERE is_transfer=0 AND pending=0 AND category IS NULL AND ({like}) {where}
            ORDER BY date DESC""",
        params,
    ).fetchall()
