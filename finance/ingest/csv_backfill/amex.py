"""Amex export: Date, Description, Amount, Extended Details, Appears On Your Statement As, Category, Reference.
Amex signs charges POSITIVE and payments negative, so flip."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick


def parse(rows: list[dict], account_id: str) -> Iterable[dict]:
    for r in rows:
        yield dict(
            account_id=account_id,
            date=parse_date(pick(r, "Date")),
            amount_native=-parse_amount(pick(r, "Amount")),
            currency="USD",
            merchant_raw=pick(r, "Appears On Your Statement As", "Description"),
            external_id=pick(r, "Reference"),
        )
