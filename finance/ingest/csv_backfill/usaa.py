"""USAA export: Date, Description, Original Description, Category, Amount, Status. Amount: negative = debit."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick


def parse(rows: list[dict], account_id: str) -> Iterable[dict]:
    for r in rows:
        status = (pick(r, "Status", default="") or "").lower()
        if "pending" in status:
            continue
        amt = parse_amount(pick(r, "Amount"))
        yield dict(
            account_id=account_id,
            date=parse_date(pick(r, "Date", "Posted Date")),
            amount_native=amt,  # USAA already signs debits negative
            currency="USD",
            merchant_raw=pick(r, "Original Description", "Description"),
        )
