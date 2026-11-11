"""Wise statement CSV: TransferWise ID, Date, Amount, Currency, Description, Payment Reference, Running Balance,
Exchange From, Exchange To, Exchange Rate, Payer Name, Payee Name, ... One file per balance currency.
Account id is derived from Currency (wise_usd / wise_eur) unless --account is given."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick


def parse(rows: list[dict], account_id: str | None) -> Iterable[dict]:
    for r in rows:
        cur = (pick(r, "Currency", default="") or "").upper()
        aid = account_id or f"wise_{cur.lower()}"
        payer = pick(r, "Payer Name", default="")
        payee = pick(r, "Payee Name", default="")
        desc = pick(r, "Description", default="") or ""
        yield dict(
            account_id=aid,
            date=parse_date(pick(r, "Date")),
            amount_native=parse_amount(pick(r, "Amount")),
            currency=cur,
            merchant_raw=desc or payee or payer,
            external_id=f"csv:{pick(r, 'TransferWise ID', 'ID')}",
        )
