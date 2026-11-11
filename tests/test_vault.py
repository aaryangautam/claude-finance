import pytest

from finance.vault import SchemaError, load_budget_schema
from tests.conftest import FIXTURES


def test_loads_fixture(schema):
    assert "groceries" in schema.category_ids
    assert schema.accounts_by_role("hub")[0].id == "checking"
    assert schema.category("investing_roth_ira").account == "roth_ira"
    assert schema.planned_net == 5000 + 300 + 2000 - 900 - 500
    assert [w.step for w in schema.waterfall] == [1, 2, 3]
    assert schema.waterfall[1].target == 10000


def test_missing_file(tmp_path):
    with pytest.raises(SchemaError, match="not found"):
        load_budget_schema(tmp_path / "nope.md")


def _write(tmp_path, body: str):
    p = tmp_path / "Budget Schema.md"
    p.write_text(body)
    return p


def test_bad_group(tmp_path):
    p = _write(tmp_path, "---\ncategories:\n  - {id: a, group: fun, type: fixed}\naccounts:\n  - {id: h, institution: X, role: hub, currency: USD}\n---\n")
    with pytest.raises(SchemaError, match="group"):
        load_budget_schema(p)


def test_duplicate_ids(tmp_path):
    p = _write(tmp_path, "---\ncategories:\n  - {id: a, group: expenses, type: fixed}\n  - {id: a, group: expenses, type: fixed}\naccounts:\n  - {id: h, institution: X, role: hub, currency: USD}\n---\n")
    with pytest.raises(SchemaError, match="duplicate"):
        load_budget_schema(p)


def test_no_hub(tmp_path):
    p = _write(tmp_path, "---\ncategories:\n  - {id: a, group: expenses, type: fixed}\naccounts:\n  - {id: h, institution: X, role: credit, currency: USD}\n---\n")
    with pytest.raises(SchemaError, match="hub"):
        load_budget_schema(p)


def test_too_many(tmp_path):
    cats = "\n".join(f"  - {{id: c{i}, group: expenses, type: fixed}}" for i in range(26))
    p = _write(tmp_path, f"---\ncategories:\n{cats}\naccounts:\n  - {{id: h, institution: X, role: hub, currency: USD}}\n---\n")
    with pytest.raises(SchemaError, match="25"):
        load_budget_schema(p)


def test_allocation_account_must_exist(tmp_path):
    p = _write(tmp_path, "---\ncategories:\n  - {id: a, group: investing, type: allocation, account: ghost}\naccounts:\n  - {id: h, institution: X, role: hub, currency: USD}\n---\n")
    with pytest.raises(SchemaError, match="ghost"):
        load_budget_schema(p)


def test_two_hubs_rejected(tmp_path):
    p = _write(tmp_path, "---\ncategories:\n  - {id: a, group: expenses, type: fixed}\naccounts:\n"
                         "  - {id: h1, institution: X, role: hub, currency: USD}\n  - {id: h2, institution: Y, role: hub, currency: USD}\n---\n")
    with pytest.raises(SchemaError, match="exactly one"):
        load_budget_schema(p)


def test_starter_schema_is_valid():
    from finance.config import ROOT
    from finance.planner.goals import parse_waterfall

    s = load_budget_schema(ROOT / "examples" / "Budget Schema.md")
    assert s.accounts_by_role("hub")[0].id == "checking"
    assert [r.kind for r in parse_waterfall(s)] == ["baseline", "hold", "sink"]
