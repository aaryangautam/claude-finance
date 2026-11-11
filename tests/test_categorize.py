import json

import pytest

from finance.pipeline import categorize
from tests.conftest import add_tx


def test_run_writes_handoff_and_apply(conn, schema, cfg):
    add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE SAGT DANKE. 10115 Berlin DEU")
    add_tx(conn, "card_everyday", "2026-08-09", -38.00, "REWE SAGT DANKE. 10115 Berlin DEU")
    add_tx(conn, "card_dining", "2026-08-03", -12.50, "SQ *BLUE BOTTLE COFFEE #1234 SAN FRANCISCO CA")
    add_tx(conn, "checking", "2026-08-01", 4200.00, "ACME CORP PAYROLL")
    stats = categorize.run(conn, schema, cfg)
    assert stats["rules"] == 1  # payroll
    assert stats["handoff"]["unknown_merchants"] == 2
    payload = json.loads((cfg.work_dir / "uncategorized.json").read_text())
    ids = {c["id"] for c in payload["allowed_categories"]}
    assert "groceries" in ids and "income_pay" not in ids and "investing_roth_ira" not in ids
    names = {m["merchant_norm"] for m in payload["merchants"]}
    assert names == {"Rewe Sagt Danke", "Blue Bottle Coffee"}

    (cfg.work_dir / "categorized.json").write_text(json.dumps({"merchants": [
        {"merchant_norm": "Rewe Sagt Danke", "category": "groceries", "confidence": 0.95},
        {"merchant_norm": "Blue Bottle Coffee", "category": "dining", "subcategory": "coffee", "confidence": 0.6},
    ]}))
    res = categorize.apply(conn, schema, cfg)
    assert res["merchants"] == 2
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='groceries'").fetchone()[0] == 2
    # low confidence -> unclear queue
    assert res["unclear"] == 1
    assert [r["merchant_norm"] for r in categorize.unclear_queue(conn, cfg)] == ["Blue Bottle Coffee"]
    # second run finds nothing new
    stats2 = categorize.run(conn, schema, cfg)
    assert stats2["handoff"]["unknown_merchants"] == 0


def test_apply_rejects_unknown_id(conn, schema, cfg):
    cfg.work_dir.mkdir(exist_ok=True)
    (cfg.work_dir / "categorized.json").write_text(json.dumps({"merchants": [
        {"merchant_norm": "X", "category": "not_a_category", "confidence": 0.9}]}))
    with pytest.raises(ValueError, match="unknown category"):
        categorize.apply(conn, schema, cfg)


def test_user_correction_wins(conn, schema, cfg):
    add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE BERLIN")
    categorize.run(conn, schema, cfg)
    categorize.correct(conn, schema, "Rewe Berlin", "groceries")
    assert conn.execute("SELECT source FROM merchant_map WHERE merchant_norm='Rewe Berlin'").fetchone()[0] == "user"
    # an LLM re-guess must not override
    (cfg.work_dir / "categorized.json").write_text(json.dumps({"merchants": [
        {"merchant_norm": "Rewe Berlin", "category": "dining", "confidence": 0.99}]}))
    categorize.apply(conn, schema, cfg)
    row = conn.execute("SELECT category, source FROM merchant_map WHERE merchant_norm='Rewe Berlin'").fetchone()
    assert tuple(row) == ("groceries", "user")
    assert conn.execute("SELECT category FROM transactions").fetchone()[0] == "groceries"


def test_plaid_seed(conn, schema, cfg):
    add_tx(conn, "card_dining", "2026-08-03", -60.00, "EDEKA", plaid_category="FOOD_AND_DRINK/FOOD_AND_DRINK_GROCERIES")
    stats = categorize.run(conn, schema, cfg)
    assert stats["plaid_seeded"] == 1
    row = conn.execute("SELECT category, category_source, category_confidence FROM transactions").fetchone()
    assert tuple(row) == ("groceries", "plaid", 0.5)
    assert stats["unclear"] == 1  # 0.5 < threshold, so it still surfaces for confirmation


def test_correct_rejects_unknown(conn, schema):
    with pytest.raises(ValueError):
        categorize.correct(conn, schema, "X", "nope")


def test_plaid_seeded_merchants_go_to_handoff_once(conn, schema, cfg):
    add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE BERLIN", plaid_category="FOOD_AND_DRINK/FOOD_AND_DRINK_GROCERIES")
    categorize.run(conn, schema, cfg)
    h = json.loads((cfg.work_dir / "uncategorized.json").read_text())
    m = {x["merchant_norm"]: x for x in h["merchants"]}
    assert m["Rewe Berlin"]["seeded_guess"] == "groceries"
    (cfg.work_dir / "categorized.json").write_text(json.dumps({"merchants": [
        {"merchant_norm": "Rewe Berlin", "category": "groceries", "confidence": 0.95}]}))
    categorize.apply(conn, schema, cfg)
    categorize.run(conn, schema, cfg)
    h = json.loads((cfg.work_dir / "uncategorized.json").read_text())
    assert h["merchants"] == []
