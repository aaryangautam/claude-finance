"""Transfer matcher. Deterministic, driven by account roles from the Budget Schema.

Topology it assumes (see README "Money-flow model"): exactly one `hub` account (the checking account pay lands in
and transfers leave from); every other account pairs with the hub; `fx_hub` accounts (Wise balances) also pair with
each other. Transfers between two non-hub accounts (card paid from savings, brokerage -> IRA) are not matched: use
`finance categorize transfer --txn <id>` for those.

Modes per pair:
  net         both legs is_transfer=1, shared group, no category            (hub<->credit, hub<->savings, hub<->wise, wise_usd<->wise_eur)
  allocation  both legs is_transfer=1; the hub leg carries the allocation   (hub->ira, hub->brokerage; category from schema `account:`)
              category so it shows in the budget view
  expense     only the destination leg is flagged; the hub leg stays a       (hub->loan: the payment is the Debt expense line)
              normal expense and gets its category from merchant_map/rules

Real-data rules:
  - credit pairs match hub->card only; a card purchase never pairs with a hub inflow.
  - legs touching an fx_hub (Wise) allow FX_FEE_PCT of the amount on top of the USD tolerance (Wise takes a fee).
  - same-day liability inflows are summed before matching (some banks split a loan payment into principal + interest).
  - hub outflows to an ira/brokerage with no cash leg match the account's `contribution` row in investment_transactions
    (Plaid reports some brokerages' deposits only there), within INVEST_WINDOW_DAYS after the hub debit.
  - credit-account inflows only need a hub leg when they look like a payment; statement credits are refunds.
  - card payments dated before the hub's first transaction are transfers from a pre-history account, not review items.
  - rows matching `exclude_patterns` (Zelle, Venmo) are never transfer legs.
"""


from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from ..vault import BudgetSchema

FX_FEE_PCT = 0.015
INVEST_WINDOW_DAYS = 8
PAYMENT_WORDS = ("PAYMENT", "AUTOPAY", "THANK YOU", "PYMT", "AUTOMATIC PAY")


@dataclass(frozen=True)
class Pair:
    a: str  # account id (usually the hub)
    b: str
    mode: str  # net | allocation | expense
    category: str | None = None  # allocation category applied to the hub leg


def build_pairs(schema: BudgetSchema) -> list[Pair]:
    hubs = schema.accounts_by_role("hub")
    if len(hubs) != 1:
        raise ValueError(f"expected exactly one hub account, got {[h.id for h in hubs]}")
    hub = hubs[0].id
    # allocation categories may name the account they fund: {id: investing_roth_ira, type: allocation, account: roth_ira}
    alloc_by_account = {c.account: c.id for c in schema.categories if c.type == "allocation" and c.account}
    pairs: list[Pair] = []
    for a in schema.accounts:
        if a.id == hub:
            continue
        if a.role in ("credit", "savings", "fx_hub"):
            pairs.append(Pair(hub, a.id, "net"))
        elif a.role in ("ira", "brokerage"):
            pairs.append(Pair(hub, a.id, "allocation", alloc_by_account.get(a.id)))
        elif a.role == "liability":
            pairs.append(Pair(hub, a.id, "expense"))
    fx = [a.id for a in schema.accounts_by_role("fx_hub")]
    for i in range(len(fx)):
        for j in range(i + 1, len(fx)):
            pairs.append(Pair(fx[i], fx[j], "net"))
    return pairs


def _exclude_sql(exclude: list[str]) -> tuple[str, tuple]:
    if not exclude:
        return "", ()
    return (" AND NOT (" + " OR ".join("upper(COALESCE(merchant_raw,'')) LIKE ?" for _ in exclude) + ")",
            tuple(f"%{p.upper()}%" for p in exclude))


def _candidates(conn: sqlite3.Connection, account_id: str, sign: int, exclude: list[str] | None = None,
                guessed_ok: bool = False) -> list[sqlite3.Row]:
    """guessed_ok: also consider rows whose category is only a plaid/llm guess (loan payments stay expenses either way)."""
    op = "<" if sign < 0 else ">"
    skip, params = _exclude_sql(exclude or [])
    cat = "(category IS NULL OR category_source IN ('plaid','llm'))" if guessed_ok else "category IS NULL"
    return conn.execute(
        f"""SELECT id, account_id, date, amount_usd, merchant_raw FROM transactions
            WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd IS NOT NULL
              AND amount_usd {op} 0 AND {cat}{skip}
            ORDER BY date""",
        (account_id, *params),
    ).fetchall()


def _liability_inflows(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    """Same-day inflows on a liability summed into one leg (principal + interest -> one payment)."""
    rows = conn.execute(
        """SELECT date, ROUND(SUM(amount_usd),2) amount_usd, GROUP_CONCAT(id) ids, MIN(id) id FROM transactions
           WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd>0 AND category IS NULL
           GROUP BY date ORDER BY date""",
        (account_id,),
    ).fetchall()
    return [dict(id=r["id"], date=r["date"], amount_usd=r["amount_usd"], ids=[int(x) for x in r["ids"].split(",")]) for r in rows]


def _match_legs(outs: list, ins: list, window: int, tol: float, pct: float = 0.0, after_only: bool = False) -> list[tuple[int, int]]:
    """Greedy one-to-one: for each outflow, the closest-dated unmatched inflow of equal magnitude."""
    used: set[int] = set()
    matches: list[tuple[int, int]] = []
    for o in outs:
        od = date.fromisoformat(o["date"])
        best = None
        best_gap = None
        for i in ins:
            if i["id"] in used:
                continue
            if abs(abs(i["amount_usd"]) - abs(o["amount_usd"])) > tol + pct * abs(o["amount_usd"]):
                continue
            delta = (date.fromisoformat(i["date"]) - od).days
            if after_only and delta < 0:
                continue
            gap = abs(delta)
            if gap > window:
                continue
            if best is None or gap < best_gap:
                best, best_gap = i, gap
        if best is not None:
            used.add(best["id"])
            matches.append((o["id"], best["id"]))
    return matches


def run(conn: sqlite3.Connection, schema: BudgetSchema, window_days: int, tolerance_usd: float, transfer_patterns: dict[str, list[str]],
        exclude_patterns: list[str] | None = None) -> dict:
    """Match transfer legs. Returns counts. Unmatched expected legs get needs_review=1."""
    pairs = build_pairs(schema)
    known = set(schema.account_ids)
    roles = {a.id: a.role for a in schema.accounts}
    exclude = exclude_patterns or []
    stats = {"groups": 0, "flagged": 0, "unmatched_review": 0}

    for p in pairs:
        if p.a not in known or p.b not in known:
            continue
        directions = ((p.a, p.b),) if roles[p.b] in ("credit", "liability") else ((p.a, p.b), (p.b, p.a))
        pct = FX_FEE_PCT if "fx_hub" in (roles[p.a], roles[p.b]) else 0.0
        for src, dst in directions:
            outs = _candidates(conn, src, -1, exclude, guessed_ok=roles[dst] == "liability")
            if roles[dst] == "liability":
                ins = _liability_inflows(conn, dst)
                by_id = {i["id"]: i["ids"] for i in ins}
            else:
                ins = _candidates(conn, dst, +1, exclude)
                by_id = None
            for out_id, in_id in _match_legs(outs, ins, window_days, tolerance_usd, pct):
                gid = uuid.uuid4().hex[:12]
                if p.mode == "expense":
                    # hub leg stays an expense; only the liability-side receipt(s) are hidden
                    liab_ids = by_id[in_id] if by_id else [in_id]
                    qs = ",".join("?" for _ in liab_ids)
                    conn.execute(
                        f"UPDATE transactions SET is_transfer=1, transfer_group_id=?, updated_at=datetime('now') WHERE id IN ({qs})",
                        (gid, *liab_ids),
                    )
                    conn.execute("UPDATE transactions SET transfer_group_id=? WHERE id=?", (gid, out_id))
                    stats["flagged"] += len(liab_ids)
                else:
                    conn.execute(
                        "UPDATE transactions SET is_transfer=1, transfer_group_id=?, updated_at=datetime('now') WHERE id IN (?, ?)",
                        (gid, out_id, in_id),
                    )
                    stats["flagged"] += 2
                    if p.mode == "allocation" and p.category and src == p.a:
                        conn.execute(
                            "UPDATE transactions SET category=?, category_source='rule', category_confidence=1.0 WHERE id=?",
                            (p.category, out_id),
                        )
                stats["groups"] += 1
    conn.commit()

    stats["groups"] += _match_investment_deposits(conn, schema, pairs, tolerance_usd, exclude)
    stats["prehistory_payments"] = _prehistory_card_payments(conn, schema)

    # Unmatched expected legs -> review queue. Never guess.
    stats["unmatched_review"] = _flag_unmatched(conn, schema, transfer_patterns)
    return stats


def _match_investment_deposits(conn: sqlite3.Connection, schema: BudgetSchema, pairs: list[Pair], tol: float, exclude: list[str]) -> int:
    """Hub -> ira/brokerage where the destination has no cash leg: match investment_transactions contributions."""
    hub = schema.accounts_by_role("hub")[0].id
    n = 0
    alloc = [p for p in pairs if p.mode == "allocation" and p.a == hub]
    if not alloc:
        return 0
    outs = _candidates(conn, hub, -1, exclude)
    used: set[int] = set()
    for p in alloc:
        ins = [dict(r) for r in conn.execute(
            "SELECT id, date, amount_usd FROM investment_transactions WHERE account_id=? AND type='contribution' AND amount_usd>0 ORDER BY date",
            (p.b,),
        )]
        taken = {r["transfer_group_id"] for r in conn.execute(
            "SELECT DISTINCT transfer_group_id FROM transactions WHERE transfer_group_id LIKE 'inv:%'")}
        ins = [i for i in ins if f"inv:{p.b}:{i['id']}" not in taken]
        for out_id, inv_id in _match_legs([o for o in outs if o["id"] not in used], ins, INVEST_WINDOW_DAYS, tol, after_only=True):
            used.add(out_id)
            conn.execute(
                "UPDATE transactions SET is_transfer=1, transfer_group_id=?, category=?, category_source='rule', "
                "category_confidence=1.0, updated_at=datetime('now') WHERE id=?",
                (f"inv:{p.b}:{inv_id}", p.category, out_id),
            )
            n += 1
    conn.commit()
    return n


def _prehistory_card_payments(conn: sqlite3.Connection, schema: BudgetSchema) -> int:
    """Card payments older than the hub's history were paid from an account we don't have: hide them as transfers."""
    hub = schema.accounts_by_role("hub")[0].id
    first = conn.execute("SELECT MIN(date) FROM transactions WHERE account_id=?", (hub,)).fetchone()[0]
    if not first:
        return 0
    n = 0
    for a in schema.accounts_by_role("credit"):
        rows = conn.execute(
            "SELECT id, merchant_raw FROM transactions WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd>0 "
            "AND category IS NULL AND date<?",
            (a.id, first),
        ).fetchall()
        for r in rows:
            if _looks_like_payment(r["merchant_raw"]):
                conn.execute(
                    "UPDATE transactions SET is_transfer=1, transfer_group_id='prehistory', updated_at=datetime('now') WHERE id=?",
                    (r["id"],),
                )
                n += 1
    conn.commit()
    return n


def _looks_like_payment(merchant_raw: str | None) -> bool:
    up = (merchant_raw or "").upper()
    return any(w in up for w in PAYMENT_WORDS)


def _flag_unmatched(conn: sqlite3.Connection, schema: BudgetSchema, transfer_patterns: dict[str, list[str]]) -> int:
    n = 0
    hub = schema.accounts_by_role("hub")[0].id
    # 1) hub outflows whose descriptor says "this is a transfer" but found no counterpart
    pats = [p.upper() for ps in transfer_patterns.values() for p in ps]
    if pats:
        rows = conn.execute(
            "SELECT id, merchant_raw FROM transactions WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd<0 AND needs_review=0 AND category IS NULL",
            (hub,),
        ).fetchall()
        for r in rows:
            up = (r["merchant_raw"] or "").upper()
            if any(p in up for p in pats):
                conn.execute(
                    "UPDATE transactions SET needs_review=1, review_reason='unmatched transfer leg (hub outflow)' WHERE id=?",
                    (r["id"],),
                )
                n += 1
    # 2) inflows on accounts that only ever receive money from the hub
    for a in schema.accounts:
        if a.role in ("savings", "ira", "brokerage", "liability"):
            cur = conn.execute(
                "UPDATE transactions SET needs_review=1, review_reason='inflow with no matching hub outflow' "
                "WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd>0 AND needs_review=0 AND category IS NULL AND is_reimbursement=0",
                (a.id,),
            )
            n += cur.rowcount
        elif a.role == "credit":
            # only payment-like inflows need a hub leg; statement credits and refunds are categorized as spending
            rows = conn.execute(
                "SELECT id, merchant_raw FROM transactions WHERE account_id=? AND is_transfer=0 AND pending=0 AND amount_usd>0 "
                "AND needs_review=0 AND category IS NULL AND is_reimbursement=0",
                (a.id,),
            ).fetchall()
            for r in rows:
                if _looks_like_payment(r["merchant_raw"]):
                    conn.execute(
                        "UPDATE transactions SET needs_review=1, review_reason='inflow with no matching hub outflow' WHERE id=?",
                        (r["id"],),
                    )
                    n += 1
    conn.commit()
    return n


def reset(conn: sqlite3.Connection) -> None:
    """Undo all matching (for a full re-run after backfill). Keeps user categories."""
    conn.execute(
        "UPDATE transactions SET is_transfer=0, transfer_group_id=NULL, needs_review=0, review_reason=NULL "
        "WHERE (transfer_group_id IS NOT NULL OR review_reason LIKE '%transfer%' OR review_reason LIKE '%hub outflow%') "
        "AND COALESCE(transfer_group_id,'') != 'manual'"
    )
    conn.execute("UPDATE transactions SET category=NULL, category_source=NULL, category_confidence=NULL WHERE category_source='rule'")
    conn.commit()
