"""FX rates to USD. ECB daily via frankfurter.app, cached in fx_rates. Wise legs use Wise's own rate (source='wise')."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import requests

FRANKFURTER = "https://api.frankfurter.app"


class FxError(RuntimeError):
    pass


def to_usd_rate(conn: sqlite3.Connection, currency: str, on: str) -> float:
    """1 unit of `currency` in USD on date `on` (YYYY-MM-DD). Prefers Wise rate for that day, then cached ECB, then fetches."""
    if currency == "USD":
        return 1.0
    pair = f"{currency}USD"
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE date=? AND pair=? ORDER BY CASE source WHEN 'wise' THEN 0 ELSE 1 END LIMIT 1",
        (on, pair),
    ).fetchone()
    if row:
        return float(row[0])
    _fetch_range(conn, currency, date.fromisoformat(on) - timedelta(days=7), date.fromisoformat(on))
    row = conn.execute("SELECT rate FROM fx_rates WHERE date=? AND pair=? AND source='ecb'", (on, pair)).fetchone()
    if not row:
        raise FxError(f"no {pair} rate for {on}")
    return float(row[0])


def _fetch_range(conn: sqlite3.Connection, currency: str, start: date, end: date) -> None:
    """Fetch ECB rates for [start, end] and fill weekends/holidays forward, so every calendar day has a row."""
    url = f"{FRANKFURTER}/{start.isoformat()}..{end.isoformat()}"
    try:
        r = requests.get(url, params={"from": currency, "to": "USD"}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        raise FxError(f"frankfurter fetch failed for {currency}USD {start}..{end}: {e}")
    rates = r.json().get("rates", {})
    if not rates:
        raise FxError(f"frankfurter returned no rates for {currency}USD {start}..{end}")
    pair = f"{currency}USD"
    last: float | None = None
    d = start
    # Use the rate from the last business day at or before d; if start itself is a holiday, look one week earlier.
    if start.isoformat() not in rates:
        earlier = requests.get(
            f"{FRANKFURTER}/{(start - timedelta(days=7)).isoformat()}..{start.isoformat()}",
            params={"from": currency, "to": "USD"},
            timeout=20,
        ).json().get("rates", {})
        for k in sorted(earlier):
            last = float(earlier[k]["USD"])
    while d <= end:
        key = d.isoformat()
        if key in rates:
            last = float(rates[key]["USD"])
        if last is not None:
            conn.execute(
                "INSERT OR IGNORE INTO fx_rates (date, pair, rate, source) VALUES (?, ?, ?, 'ecb')",
                (key, pair, last),
            )
        d += timedelta(days=1)
    conn.commit()


def record_wise_rate(conn: sqlite3.Connection, currency: str, on: str, rate_to_usd: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fx_rates (date, pair, rate, source) VALUES (?, ?, ?, 'wise')",
        (on, f"{currency}USD", rate_to_usd),
    )


def fill_missing_usd(conn: sqlite3.Connection) -> int:
    """Set fx_rate_to_usd/amount_usd on any transaction that lacks it. Returns rows updated."""
    rows = conn.execute(
        "SELECT id, currency, date, amount_native FROM transactions WHERE amount_usd IS NULL"
    ).fetchall()
    n = 0
    for r in rows:
        rate = to_usd_rate(conn, r["currency"], r["date"])
        conn.execute(
            "UPDATE transactions SET fx_rate_to_usd=?, amount_usd=? WHERE id=?",
            (rate, round(r["amount_native"] * rate, 2), r["id"]),
        )
        n += 1
    conn.commit()
    return n
