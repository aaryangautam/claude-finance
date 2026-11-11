"""Merchant normalization. Deterministic; the same raw string always yields the same key for merchant_map."""

from __future__ import annotations

import re
import sqlite3

_PREFIXES = re.compile(
    r"^(?:(?:SQ|TST|SP|PP|PAYPAL|PY|DD|POS|DEBIT|CHECKCARD|PURCHASE|VISA|MC|APLPAY|APPLE PAY|GOOGLE PAY|CKCD)\s*[\*\-:]?\s+)+",
    re.I,
)
_STORE_NUMBERS = re.compile(r"\s*(?:#|NO\.?|STORE|STR|LOC)\s*\d{2,}\b", re.I)
_TRAILING_NUMBERS = re.compile(r"(?:\s+\d{3,})+$")
_DATE_LIKE = re.compile(r"\b\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?\b")
_CARD_TAIL = re.compile(r"\b(?:X{2,}|\*{2,})\d{2,}\b")
_US_STATES = r"(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])"
_CITY_PREFIX = r"(?:SAN|NEW|LOS|LAS|FORT|FT|ST|SAINT|EL|SANTA|NORTH|SOUTH|EAST|WEST|MOUNT|MT|LAKE|PORT|GRAND)"
# "<merchant> [<prefix>] <City> <ST>" -> "<merchant>"; the city is one word, or two when it starts with a known prefix
_US_STATE_TAIL = re.compile(rf"^(.+?\S)\s+(?:{_CITY_PREFIX}\s+)?[A-Za-z\.\-']+\s+{_US_STATES}$")
# "<merchant> <City> DEU" -> "<merchant>"
_DE_TAIL = re.compile(r"^(.+?\S)\s+[A-Za-z\.\-']+\s+(?:DEU|DE|GERMANY|DEUTSCHLAND|NLD|FRA|ITA|AUT|CHE|ESP|GBR)$", re.I)
_DIGIT_TOKEN = re.compile(r"\s+\S*\d\S*")  # any token containing a digit (transfer descriptors only)
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")   # store/terminal ids anywhere
_URL = re.compile(r"(?:HTTPS?://)?(?:WWW\.)?([A-Z0-9\-]+)(?:\.[A-Z0-9\-]+)*\.(?:COM|DE|NET|ORG|IO|CO|EU)\b(?:/\S*)?", re.I)
_REF = re.compile(r"\b(?:REF|TRN|TXN|ID|CONF|AUTH)[:#]?\s*[A-Z0-9]{5,}\b", re.I)
_LONG_ALNUM = re.compile(r"\b(?=[A-Z0-9]{8,}\b)(?=[A-Z0-9]*\d)[A-Z0-9]+\b")
_NONWORD = re.compile(r"[^A-Za-z0-9&'\- ]+")
_SPACES = re.compile(r"\s+")

# Descriptor families that carry meaning as a whole and shouldn't be trimmed to a city/state.
_KEEP_WHOLE = ("PAYROLL", "USAA", "AMEX", "AMERICAN EXPRESS", "CHASE", "ROBINHOOD", "WISE", "TRANSFERWISE", "VENMO", "ZELLE")


def normalize(raw: str | None) -> str:
    if not raw:
        return ""
    s = raw.strip()
    up = s.upper()
    if any(k in up for k in _KEEP_WHOLE):
        # keep the family name but drop trailing reference noise
        s = _DIGIT_TOKEN.sub("", s)
        s = _NONWORD.sub(" ", s)
        return _SPACES.sub(" ", s).strip().title()
    s = _URL.sub(lambda m: m.group(1), s)  # amazon.com/bill -> amazon
    s = _PREFIXES.sub("", s)
    s = _REF.sub("", s)
    s = _CARD_TAIL.sub("", s)
    s = _DATE_LIKE.sub("", s)
    s = _STORE_NUMBERS.sub("", s)
    s = _LONG_ALNUM.sub("", s)
    s = _LONG_DIGITS.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    s = _DE_TAIL.sub(r"\1", s)
    s = _US_STATE_TAIL.sub(r"\1", s)
    s = _NONWORD.sub(" ", s)
    s = _TRAILING_NUMBERS.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    return s.title()


def normalize_all(conn: sqlite3.Connection, only_missing: bool = True) -> int:
    where = "WHERE merchant_norm IS NULL OR merchant_norm = ''" if only_missing else ""
    rows = conn.execute(f"SELECT id, merchant_raw FROM transactions {where}").fetchall()
    for r in rows:
        conn.execute("UPDATE transactions SET merchant_norm=? WHERE id=?", (normalize(r["merchant_raw"]), r["id"]))
    conn.commit()
    return len(rows)
