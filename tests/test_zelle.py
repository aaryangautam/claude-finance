import json

import pytest

from finance.pipeline import categorize
from tests.conftest import add_tx


def _row(conn, tid):
    return conn.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()


def test_zelle_payback_with_category_nets_against_it(conn, schema, cfg):
    tid = add_tx(conn, "checking", "2026-10-30", 750.00, "Zelle Transfer from Alex Rivera")
    categorize.run(conn, schema, cfg)
    r = _row(conn, tid)
    assert (r["is_reimbursement"], r["category"], r["category_source"], r["needs_review"]) == (1, "travel", "rule", 0)
    spend = conn.execute("SELECT spend_usd FROM v_spending WHERE id=?", (tid,)).fetchone()[0]
    assert spend == -750.00  # negative spend in travel, not income


def test_zelle_payback_without_category_waits_for_a_pick(conn, schema, cfg):
    tid = add_tx(conn, "checking", "2026-11-02", 499.00, "Zelle Transfer from Sam Lee")
    categorize.run(conn, schema, cfg)
    r = _row(conn, tid)
    assert (r["is_reimbursement"], r["category"], r["needs_review"]) == (1, None, 1)
    assert [t["id"] for t in categorize.unclear_transactions(conn, cfg)] == [tid]
    categorize.correct_txn(conn, schema, tid, "dining")
    r = _row(conn, tid)
    assert (r["category"], r["category_source"], r["needs_review"], r["is_reimbursement"]) == ("dining", "user_txn", 0, 1)
    assert categorize.unclear_transactions(conn, cfg) == []


def test_zelle_never_merchant_mapped_or_sent_to_llm(conn, schema, cfg):
    a = add_tx(conn, "checking", "2026-09-08", -62.00, "Zelle Transfer to Sam Lee")
    b = add_tx(conn, "checking", "2026-10-26", -269.00, "Zelle Transfer to Sam Lee")
    stats = categorize.run(conn, schema, cfg)
    handoff = json.loads((cfg.work_dir / "uncategorized.json").read_text())
    assert not any("Zelle" in m["merchant_norm"] for m in handoff["merchants"])
    assert stats["handoff"]["unknown_merchants"] == 0
    # a merchant_map entry (e.g. an old user correction) must not sweep per-transaction rows
    norm = _row(conn, a)["merchant_norm"]
    conn.execute("INSERT INTO merchant_map (merchant_norm, category, confidence, source) VALUES (?, 'gifts', 1.0, 'llm')", (norm,))
    conn.commit()
    categorize.apply_merchant_map(conn, cfg)
    assert _row(conn, a)["category"] is None
    categorize.correct_txn(conn, schema, a, "dining")
    categorize.correct_txn(conn, schema, b, "travel")
    assert (_row(conn, a)["category"], _row(conn, b)["category"]) == ("dining", "travel")


def test_merchant_correction_keeps_per_txn_overrides(conn, schema, cfg):
    a = add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE BERLIN")
    b = add_tx(conn, "card_everyday", "2026-08-09", -18.00, "REWE BERLIN")
    categorize.run(conn, schema, cfg)
    categorize.correct_txn(conn, schema, b, "gifts")
    categorize.correct(conn, schema, _row(conn, a)["merchant_norm"], "groceries")
    assert (_row(conn, a)["category"], _row(conn, b)["category"]) == ("groceries", "gifts")


def test_correct_txn_fails_loudly(conn, schema, cfg):
    tid = add_tx(conn, "checking", "2026-10-26", -269.00, "Zelle Transfer to Sam Lee")
    with pytest.raises(ValueError, match="unknown category"):
        categorize.correct_txn(conn, schema, tid, "not_a_category")
    with pytest.raises(ValueError, match="no transaction"):
        categorize.correct_txn(conn, schema, 999999, "dining")
    xfer = add_tx(conn, "checking", "2026-10-02", -625.0, "ROBINHOOD", is_transfer=1)
    with pytest.raises(ValueError, match="internal transfer"):
        categorize.correct_txn(conn, schema, xfer, "dining")


def test_bad_zelle_category_in_config_raises(conn, schema, cfg):
    cfg.raw["reimbursements"]["zelle"] = {"alex rivera": "flights"}
    with pytest.raises(ValueError, match="flights"):
        categorize.run(conn, schema, cfg)


def test_wise_payback_rule_flags_rows_imported_before_sender_listed(conn, schema, cfg):
    # cfg fixture lists senders ["sam lee", "jordan kim"]
    tid = add_tx(conn, "wise_eur", "2026-10-26", 68.0, "Received money from Jordan Kim with reference phone",
                 currency="EUR", usd=79.0, source="wise")
    other = add_tx(conn, "wise_eur", "2026-08-18", 390.5, "Received money from City Utilities AG",
                   currency="EUR", usd=455.0, source="wise")
    categorize.run(conn, schema, cfg)
    r = _row(conn, tid)
    assert (r["is_reimbursement"], r["category"], r["category_source"]) == (1, "phone", "rule")
    assert _row(conn, other)["is_reimbursement"] == 0


def test_wise_category_senders_and_other_income(conn, schema, cfg):
    cfg.raw["reimbursements"]["categories"] = {"taxfree gmbh": "groceries"}
    cfg.raw["income"]["other_patterns"] = ["TAX REF"]
    cfg.raw["income"]["other_income_category"] = "travel"  # fixture schema has no income_other; any id proves the wiring
    v = add_tx(conn, "wise_eur", "2026-04-02", 35.0, "Received money from TAXFREE GmbH with April VAT Refund",
               currency="EUR", usd=40.0, source="wise")
    t = add_tx(conn, "checking", "2026-04-21", 528.0, "IRS TREAS 310 TAX REF")
    categorize.run(conn, schema, cfg)
    assert (_row(conn, v)["is_reimbursement"], _row(conn, v)["category"]) == (1, "groceries")
    assert _row(conn, t)["category"] == "travel"


def test_manual_transfer_survives_rematch(conn, schema, cfg):
    tid = add_tx(conn, "checking", "2025-10-30", 6000.0, "LOAN DISBURSEMENT")
    categorize.run(conn, schema, cfg)
    categorize.mark_transfer(conn, tid)
    categorize.run(conn, schema, cfg, rematch=True)
    r = _row(conn, tid)
    assert (r["is_transfer"], r["transfer_group_id"], r["category"]) == (1, "manual", None)
    assert conn.execute("SELECT COUNT(*) FROM v_spending WHERE id=?", (tid,)).fetchone()[0] == 0
    with pytest.raises(ValueError, match="no transaction"):
        categorize.mark_transfer(conn, 999999)
