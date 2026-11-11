from __future__ import annotations

import re
from datetime import datetime

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y")


def parse_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s[: len(datetime.now().strftime(fmt))] if "T" not in fmt else s[:19], fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date {s!r}")


def parse_amount(s: str | None) -> float:
    """'-$1,234.56' / '(1234.56)' / '1.234,56' -> float."""
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.startswith("--")
    s = re.sub(r"[^\d,.\-]", "", s)
    s = s.lstrip("-")
    # decide decimal separator: last of , or .
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    val = float(s) if s else 0.0
    return -val if neg else val


def pick(row: dict, *names: str, default=None):
    """First present column among case-insensitive candidate names."""
    lower = {k.strip().lower(): v for k, v in row.items() if k}
    for n in names:
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return default
