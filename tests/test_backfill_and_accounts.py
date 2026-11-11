from pathlib import Path

from finance import accounts as acct_mod
from finance.ingest import csv_backfill
from finance.ingest.csv_backfill._common import parse_amount, parse_date
from tests.conftest import add_tx


def test_parse_helpers():
    assert parse_amount("-$1,234.56") == -1234.56
    assert parse_amount("(12.00)") == -12.0
    assert parse_amount("1.234,56") == 1234.56
    assert parse_amount("--45.00") == -45.0
    assert parse_date("08/03/2026") == "2026-08-03"
    assert parse_date("2026-08-03") == "2026-08-03"
    assert parse_date("03.08.2026") == "2026-08-03"


def test_amex_backfill_dedupes_against_plaid(conn, schema, cfg, tmp_path):
    # existing plaid row: purchase 42.10 at REWE on 2026-08-02
    add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE SAGT DANKE. 10115 Berlin DEU", merchant_norm="Rewe Sagt Danke", source="plaid", ext="p1")
    csv = tmp_path / "amex.csv"
    csv.write_text(
        "Date,Description,Amount,Appears On Your Statement As,Reference\n"
        "08/02/2026,REWE SAGT DANKE,42.10,REWE SAGT DANKE. 10115 Berlin DEU,R1\n"
        "07/15/2026,EDEKA,18.30,EDEKA HAUSMANN BERLIN DE,R2\n"
        "07/20/2026,PAYMENT,-500.00,AUTOPAY PAYMENT - THANK YOU,R3\n"
    )
    dry = csv_backfill.run(conn, schema, "amex", csv, "card_everyday", dry_run=True)
    assert dry["inserted"] == 2 and dry["duplicates"] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    res = csv_backfill.run(conn, schema, "amex", csv, "card_everyday")
    assert res["inserted"] == 2
    rows = {r["merchant_raw"]: r for r in conn.execute("SELECT * FROM transactions WHERE source='csv_backfill'")}
    assert rows["EDEKA HAUSMANN BERLIN DE"]["amount_native"] == -18.30  # Amex charge flipped to outflow
    assert rows["AUTOPAY PAYMENT - THANK YOU"]["amount_native"] == 500.0


def test_robinhood_backfill(conn, schema, cfg, tmp_path):
    csv = tmp_path / "rh.csv"
    csv.write_text(
        "Activity Date,Process Date,Settle Date,Instrument,Description,Trans Code,Quantity,Price,Amount\n"
        "8/1/2026,8/1/2026,8/1/2026,,ACH Deposit,ACH,,,\"$583.33\"\n"
        "8/2/2026,8/2/2026,8/4/2026,VTI,Vanguard Total Stock Market ETF,Buy,2,\"$280.00\",\"($560.00)\"\n"
        "8/15/2026,8/15/2026,8/15/2026,VTI,Dividend,CDIV,,,\"$3.20\"\n"
    )
    res = csv_backfill.run(conn, schema, "robinhood", csv, "roth_ira")
    assert res["inserted"] == 1 and res["investment_rows"] == 2
    assert conn.execute("SELECT amount_native FROM transactions").fetchone()[0] == 583.33
    inv = conn.execute("SELECT type, amount_usd FROM investment_transactions ORDER BY date").fetchall()
    assert [tuple(r) for r in inv] == [("buy", -560.0), ("dividend", 3.2)]


def test_map_account_moves_children(cfg, schema):
    from finance.db import open_db

    conn = open_db(cfg.db_path)
    conn.execute("INSERT INTO accounts (id, institution, plaid_account_id, type, currency) VALUES ('plaid:abc','MyBank','abc','checking','USD')")
    conn.commit()
    add_tx(conn, "plaid:abc", "2026-08-01", -5.0, "X")
    conn.execute("INSERT INTO balance_snapshots (account_id, date, balance_native, currency, balance_usd) VALUES ('plaid:abc','2026-08-01',10,'USD',10)")
    conn.commit()
    acct_mod.map_account(conn, schema, "plaid:abc", "checking")
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    row = conn.execute("SELECT id, role, plaid_account_id FROM accounts").fetchone()
    assert tuple(row) == ("checking", "hub", "abc")
    assert conn.execute("SELECT account_id FROM transactions").fetchone()[0] == "checking"
    assert conn.execute("SELECT account_id FROM balance_snapshots").fetchone()[0] == "checking"
