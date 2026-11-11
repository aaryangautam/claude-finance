"""Robinhood account activity export: Activity Date, Process Date, Settle Date, Instrument, Description, Trans Code,
Quantity, Price, Amount. Trans Codes: Buy, Sell, CDIV (dividend), ACH (deposit/withdrawal), GOLD, INT, ...

Yields two kinds: _kind='cash' (ACH deposits -> transactions table so the matcher can net them against the hub account) and
investment rows (buy/sell/dividend -> investment_transactions)."""

from __future__ import annotations

from collections.abc import Iterable

from ._common import parse_amount, parse_date, pick

CODES = {"buy": "buy", "sell": "sell", "cdiv": "dividend", "div": "dividend", "int": "dividend", "gold": "fee", "dfee": "fee", "ach": "contribution"}


def parse(rows: list[dict], account_id: str) -> Iterable[dict]:
    for r in rows:
        code = (pick(r, "Trans Code", default="") or "").strip().lower()
        amt = parse_amount(pick(r, "Amount"))
        date = parse_date(pick(r, "Process Date", "Activity Date", "Settle Date"))
        desc = pick(r, "Description", default="")
        if code == "ach":
            yield dict(_kind="cash", date=date, amount_native=amt, merchant_raw=f"Robinhood ACH {desc}".strip())
            continue
        typ = CODES.get(code)
        if not typ:
            continue
        yield dict(
            _kind="investment",
            date=date,
            ticker=pick(r, "Instrument"),
            type=typ,
            quantity=parse_amount(pick(r, "Quantity")) or None,
            price=parse_amount(pick(r, "Price")) or None,
            amount_usd=amt,  # Robinhood: buys negative, sells/dividends positive
        )
