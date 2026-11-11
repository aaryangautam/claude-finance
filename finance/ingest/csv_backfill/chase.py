"""Chase card export: Transaction Date, Post Date, Description, Category, Type, Amount, Memo. Amount: purchases negative."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick


def parse(rows: list[dict], account_id: str) -> Iterable[dict]:
    for r in rows:
        yield dict(
            account_id=account_id,
            date=parse_date(pick(r, "Transaction Date", "Post Date", "Date")),
            amount_native=parse_amount(pick(r, "Amount")),  # negative purchase = money out of the card account
            currency="USD",
            merchant_raw=pick(r, "Description"),
        )
