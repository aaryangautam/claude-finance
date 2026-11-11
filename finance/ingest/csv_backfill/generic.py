"""Generic bank CSV: one date column, one description column, one signed amount column (negative = money out).
Column names are matched case-insensitively against common spellings. For a different sign convention or
layout, copy one of the institution adapters next to this file and register it in PARSERS."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick


def parse(rows: list[dict], account_id: str) -> Iterable[dict]:
    for r in rows:
        yield dict(
            account_id=account_id,
            date=parse_date(pick(r, "Date", "Transaction Date", "Posted Date", "Post Date", "Booking Date")),
            amount_native=parse_amount(pick(r, "Amount", "Value", "Betrag")),
            currency=(pick(r, "Currency", default="USD") or "USD").upper(),
            merchant_raw=pick(r, "Description", "Payee", "Merchant", "Name", "Memo"),
        )
