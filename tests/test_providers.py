import pytest

from finance import providers, snapshot
from finance.config import ConfigError
from finance.pipeline import categorize
from finance.providers import sync as provider_sync
from finance.providers.wise import statement_line
from tests.conftest import add_fx


@pytest.fixture
def fake(cfg):
    cfg.raw["providers"] = {"neo": {"module": "tests.fake_provider:FakeProvider", "days": 30}}
    return providers.get(cfg, "neo")


def test_loader(cfg, fake):
    assert fake.name == "neo" and fake.settings["days"] == 30
    cfg.raw["providers"]["off"] = {"module": "tests.fake_provider:FakeProvider", "enabled": False}
    assert list(providers.configured(cfg)) == ["neo"]
    cfg.raw["providers"]["bad"] = {"module": "finance.config:Config"}
    with pytest.raises(ConfigError, match="not a finance.providers.Provider"):
        providers.get(cfg, "bad")
    cfg.raw["providers"]["nomodule"] = {}
    with pytest.raises(ConfigError, match="module"):
        providers.get(cfg, "nomodule")
    cfg.raw["providers"] = {"plaid": {}}
    with pytest.raises(ConfigError, match="reserved"):
        providers.configured(cfg)


def test_sync_stores_rows_idempotently(conn, schema, fake):
    res = provider_sync.sync(conn, fake, schema_ids=set(schema.account_ids))
    # suggested id taken when it's in the schema; otherwise provisional <provider>:<id>
    assert res["accounts"] == ["neo:acc-x", "wise_eur"]
    provider_sync.sync(conn, fake, schema_ids=set(schema.account_ids))
    rows = {r["external_id"]: r for r in conn.execute("SELECT * FROM transactions WHERE source='neo'")}
    assert len(rows) == 3
    assert (rows["t1"]["account_id"], rows["t1"]["amount_usd"], rows["t1"]["counterparty"]) == ("wise_eur", -1650.0, "Landlord GmbH")
    assert rows["t2"]["currency"] == "EUR" and rows["t2"]["amount_usd"] == 33.0  # from the provider's rate
    assert rows["t3"]["amount_usd"] == 12.5
    rate = conn.execute("SELECT source, rate FROM fx_rates WHERE date='2026-10-02' AND pair='EURUSD'").fetchone()
    assert tuple(rate) == ("neo", 1.10)


def test_provider_account_maps_like_plaid(conn, schema, fake):
    from finance.accounts import map_account, unmapped

    conn.execute("DELETE FROM accounts WHERE id='brokerage'")  # the conn fixture pre-creates every schema account
    provider_sync.sync(conn, fake, schema_ids=set(schema.account_ids))
    assert [r["id"] for r in unmapped(conn)] == ["neo:acc-x"]
    map_account(conn, schema, "neo:acc-x", "brokerage")
    provider_sync.sync(conn, fake, schema_ids=set(schema.account_ids))  # re-sync finds the mapped row
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id='brokerage' AND source='neo'").fetchone()[0] == 1
    assert unmapped(conn) == []


def test_counterparty_payback_is_a_reimbursement(conn, schema, cfg, fake):
    provider_sync.sync(conn, fake, schema_ids=set(schema.account_ids))
    categorize.run(conn, schema, cfg)
    r = conn.execute("SELECT is_reimbursement, category FROM transactions WHERE external_id='t2'").fetchone()
    assert tuple(r) == (1, "phone")  # "sam lee" is a listed sender; matched on counterparty, not description


def test_snapshot_runs_every_provider(conn, schema, cfg, fake):
    add_fx(conn, "2026-10", 1.10)
    errs = snapshot.snapshot_provider(conn, fake, "2026-10-31", set(schema.account_ids))
    assert errs == []
    bal = dict(conn.execute("SELECT account_id, balance_usd FROM balance_snapshots WHERE date='2026-10-31'").fetchall())
    assert bal["neo:acc-x"] == 5000.0 and bal["wise_eur"] == pytest.approx(2400 * 1.10)
    assert conn.execute("SELECT value_usd FROM holdings_snapshots WHERE ticker='VTI'").fetchone()[0] == 2500.0


def test_snapshot_reports_a_failing_provider(conn, fake):
    fake.balances = lambda: (_ for _ in ()).throw(RuntimeError("token expired"))
    assert snapshot.snapshot_provider(conn, fake, "2026-10-31") == ["neo: RuntimeError: token expired"]


def test_wise_statement_line():
    line = {"type": "CREDIT", "date": "2026-08-11T10:00:00Z", "amount": {"value": 1500.0, "currency": "EUR"},
            "referenceNumber": "TRANSFER-1",
            "details": {"description": "Received money", "senderName": "Own Account",
                        "sourceAmount": {"value": 1650.0, "currency": "USD"}, "targetAmount": {"value": 1500.0, "currency": "EUR"}}}
    t = statement_line("42", "EUR", line)
    assert (t.external_id, t.amount, t.amount_usd, t.fx_rate_to_usd, t.counterparty) == ("42:TRANSFER-1", 1500.0, 1650.0, 1.1, "Own Account")
    debit = dict(line, type="DEBIT", amount={"value": 20.0, "currency": "EUR"}, details={"description": "CARD"})
    d = statement_line("42", "EUR", debit)
    assert (d.amount, d.amount_usd, d.counterparty) == (-20.0, None, None)


def test_wise_is_builtin(cfg, monkeypatch):
    cfg.raw["providers"] = {"wise": {}}
    cfg.env["WISE_TOKEN"] = "tok"
    p = providers.get(cfg, "wise")
    assert type(p).__name__ == "WiseProvider" and p.institution == "Wise"


def test_migration_moves_wise_balance_ids(tmp_path):
    import sqlite3

    from finance.db import MIGRATIONS_DIR, migrate

    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for f in sorted(MIGRATIONS_DIR.glob("00[12]_*.sql")):
        c.executescript(f.read_text())
        c.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (int(f.name[:3]),))
    c.execute("INSERT INTO accounts (id, institution, wise_balance_id, type, currency) VALUES ('wise_eur','Wise','77','wise_eur','EUR')")
    c.commit()
    assert migrate(c) == ["003_providers.sql"]
    assert tuple(c.execute("SELECT provider, provider_account_id, wise_balance_id FROM accounts").fetchone()) == ("wise", "77", None)
