import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from finance import config as cfgmod
from finance.db import open_db
from finance.vault import load_budget_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def schema():
    return load_budget_schema(FIXTURES / "Budget Schema.md")


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    vault = tmp_path / "vault" / "Finance"
    (vault / "Goals").mkdir(parents=True)
    shutil.copy(FIXTURES / "Budget Schema.md", vault / "Budget Schema.md")
    # the shipped example config, pointed at the fixture vault: the tests double as a check that it works as-is
    text = (cfgmod.ROOT / "config.example.toml").read_text()
    text = re.sub(r'^vault_finance = .*$', f'vault_finance = "{vault}"', text, count=1, flags=re.M)
    (root / "config.toml").write_text(text)
    (root / ".env").write_text("PLAID_CLIENT_ID=x\nPLAID_SECRET=y\nPLAID_ENV=sandbox\n")
    c = cfgmod.load(root)
    # payback senders (the example ships them empty)
    r = c.raw["reimbursements"]
    r["senders"] = ["sam lee", "jordan kim"]
    r["zelle"] = {"alex rivera": "travel", "sam lee": ""}
    return c


@pytest.fixture
def conn(cfg, schema) -> sqlite3.Connection:
    c = open_db(cfg.db_path)
    for a in schema.accounts:
        typ = {"hub": "checking", "savings": "savings", "liability": "loan", "credit": "credit", "ira": "ira",
               "brokerage": "brokerage", "fx_hub": "multicurrency"}[a.role]
        c.execute("INSERT INTO accounts (id, institution, name, type, currency, role) VALUES (?,?,?,?,?,?)",
                  (a.id, a.institution, a.id, typ, a.currency, a.role))
    c.commit()
    return c


def add_tx(conn, account_id, date, amount, merchant, currency="USD", usd=None, ext=None, **kw):
    cols = dict(account_id=account_id, date=date, amount_native=amount, currency=currency,
                amount_usd=usd if usd is not None else (amount if currency == "USD" else None),
                fx_rate_to_usd=1.0 if currency == "USD" else None,
                merchant_raw=merchant, source=kw.pop("source", "test"),
                external_id=ext or f"{account_id}:{date}:{amount}:{merchant}", **kw)
    conn.execute(f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def add_fx(conn, month, rate, pair="EURUSD", source="ecb"):
    """One fx_rates row per calendar day of `month` (YYYY-MM)."""
    import calendar

    y, m = int(month[:4]), int(month[5:7])
    for d in range(1, calendar.monthrange(y, m)[1] + 1):
        conn.execute("INSERT OR REPLACE INTO fx_rates (date, pair, rate, source) VALUES (?,?,?,?)",
                     (f"{month}-{d:02d}", pair, rate, source))
    conn.commit()


def add_snapshot(conn, account_id, date, balance_usd, currency="USD"):
    conn.execute("INSERT OR REPLACE INTO balance_snapshots (account_id, date, balance_native, currency, balance_usd) VALUES (?,?,?,?,?)",
                 (account_id, date, balance_usd, currency, balance_usd))
    conn.commit()


def spend(conn, account_id, date, amount, category, merchant=None, **kw):
    """A categorized outflow (amount > 0 = spend) on `account_id`."""
    return add_tx(conn, account_id, date, -amount, merchant or f"{category.upper()} {date} {amount}", category=category,
                  category_source=kw.pop("category_source", "rule"), **kw)


def seed_month(conn, month, rent_eur=1500, eurusd=1.10, groceries=380.0, dining=260.0, pay=5900.0):
    """A plausible month: pay on the hub, EUR rent via wise, card spend, allocation legs to Robinhood."""
    add_fx(conn, month, eurusd)
    add_tx(conn, "checking", f"{month}-01", pay / 2, f"ACME CORP PAYROLL {month}a", category="income_pay", category_source="rule")
    add_tx(conn, "checking", f"{month}-15", pay / 2, f"ACME CORP PAYROLL {month}b", category="income_pay", category_source="rule")
    add_tx(conn, "wise_eur", f"{month}-02", -rent_eur, "LANDLORD", currency="EUR", usd=-round(rent_eur * eurusd, 2),
           category="housing_rent", category_source="user")
    spend(conn, "card_everyday", f"{month}-05", groceries, "groceries")
    spend(conn, "card_dining", f"{month}-09", dining, "dining")
    spend(conn, "checking", f"{month}-20", 450, "debt_car_loan", "BANK LOAN PMT")
    for cat, amt in (("investing_roth_ira", 583.0), ("investing_aggressive", 1000.0)):
        add_tx(conn, "checking", f"{month}-03", -amt, f"ROBINHOOD {cat}", category=cat, category_source="rule", is_transfer=1)


def write_goal(goals_dir, title, **fm):
    import yaml

    (goals_dir / f"{title}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + title + "\n")


@pytest.fixture
def goals(cfg):
    from finance.vault import load_goals

    write_goal(cfg.goals_dir, "Emergency Fund", goal_type="savings", status="active", target_amount=10000,
               target_date="2027-03-31", measure="balance", progress_source="savings", priority="primary",
               waterfall_position=2)
    write_goal(cfg.goals_dir, "Aggressive Investing", goal_type="investing", status="active", target_amount=20000,
               target_date="2027-06-30", measure="contributions", progress_source="investing_aggressive",
               priority="secondary", waterfall_position=3)
    write_goal(cfg.goals_dir, "Roth IRA", goal_type="investing", status="active", target_amount=7000,
               target_date="2027-04-15", measure="contributions", progress_source=None, priority="secondary")
    write_goal(cfg.goals_dir, "Old Car Fund", goal_type="savings", status="done", target_amount=5000,
               measure="balance", progress_source="savings", priority="secondary")
    return load_goals(cfg.goals_dir)
