from finance.pipeline import matcher
from tests.conftest import add_tx


def _run(conn, schema, cfg):
    return matcher.run(conn, schema, cfg.matcher_window_days, cfg.matcher_tolerance_usd, cfg.transfer_patterns)


def _tx(conn, id_):
    return conn.execute("SELECT * FROM transactions WHERE id=?", (id_,)).fetchone()


def test_card_payment_pair(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-03", -812.44, "AMEX EPAYMENT ACH PMT")
    inn = add_tx(conn, "card_everyday", "2026-08-04", 812.44, "PAYMENT RECEIVED - THANK YOU")
    purchase = add_tx(conn, "card_everyday", "2026-08-02", -42.10, "REWE BERLIN")
    s = _run(conn, schema, cfg)
    assert s["groups"] == 1
    a, b, p = _tx(conn, out), _tx(conn, inn), _tx(conn, purchase)
    assert a["is_transfer"] == 1 and b["is_transfer"] == 1
    assert a["transfer_group_id"] == b["transfer_group_id"]
    assert a["category"] is None
    assert p["is_transfer"] == 0 and p["needs_review"] == 0


def test_allocation_tags_hub_leg(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-01", -583.33, "ROBINHOOD ACH")
    inn = add_tx(conn, "roth_ira", "2026-08-02", 583.33, "ACH Deposit")
    _run(conn, schema, cfg)
    a, b = _tx(conn, out), _tx(conn, inn)
    assert a["is_transfer"] == 1 and a["category"] == "investing_roth_ira" and a["category_source"] == "rule"
    assert b["is_transfer"] == 1 and b["category"] is None
    # budget view shows the allocation, spending view doesn't
    assert conn.execute("SELECT COUNT(*) FROM v_budget WHERE category='investing_roth_ira'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_spending WHERE id=?", (out,)).fetchone()[0] == 0


def test_loan_payment_stays_expense(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-05", -450.00, "BANK AUTO LOAN PMT")
    inn = add_tx(conn, "car_loan", "2026-08-05", 450.00, "PAYMENT")
    _run(conn, schema, cfg)
    a, b = _tx(conn, out), _tx(conn, inn)
    assert a["is_transfer"] == 0, "hub leg is the Debt expense line"
    assert b["is_transfer"] == 1
    assert a["transfer_group_id"] == b["transfer_group_id"]


def test_wise_fx_leg_uses_wise_rate(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-10", -1650.00, "WISE INC")
    # EUR credit with amount_usd set from Wise's own transfer record (1500 EUR @ 1.10)
    inn = add_tx(conn, "wise_eur", "2026-08-11", 1500.00, "Transfer from own account", currency="EUR", usd=1650.00)
    _run(conn, schema, cfg)
    assert _tx(conn, out)["is_transfer"] == 1 and _tx(conn, inn)["is_transfer"] == 1


def test_window_boundary(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-01", -100.00, "CHASE CREDIT CRD AUTOPAY")
    add_tx(conn, "card_dining", "2026-08-07", 100.00, "Payment Thank You")  # 6 days > window of 5
    s = _run(conn, schema, cfg)
    assert s["groups"] == 0
    a = _tx(conn, out)
    assert a["is_transfer"] == 0 and a["needs_review"] == 1 and "unmatched" in a["review_reason"]


def test_unmatched_card_inflow_flagged(conn, schema, cfg):
    inn = add_tx(conn, "card_travel", "2026-08-20", 300.00, "PAYMENT RECEIVED")
    _run(conn, schema, cfg)
    assert _tx(conn, inn)["needs_review"] == 1


def test_closest_date_wins_one_to_one(conn, schema, cfg):
    o1 = add_tx(conn, "checking", "2026-08-01", -200.00, "AMEX EPAYMENT")
    o2 = add_tx(conn, "checking", "2026-08-03", -200.00, "AMEX EPAYMENT")
    i1 = add_tx(conn, "card_everyday", "2026-08-01", 200.00, "PAYMENT")
    i2 = add_tx(conn, "card_everyday", "2026-08-03", 200.00, "PAYMENT")
    s = _run(conn, schema, cfg)
    assert s["groups"] == 2
    assert _tx(conn, o1)["transfer_group_id"] == _tx(conn, i1)["transfer_group_id"]
    assert _tx(conn, o2)["transfer_group_id"] == _tx(conn, i2)["transfer_group_id"]


def test_reset_is_idempotent(conn, schema, cfg):
    out = add_tx(conn, "checking", "2026-08-01", -583.33, "ROBINHOOD")
    add_tx(conn, "brokerage", "2026-08-01", 583.33, "ACH")
    _run(conn, schema, cfg)
    matcher.reset(conn)
    a = _tx(conn, out)
    assert a["is_transfer"] == 0 and a["category"] is None
    s = _run(conn, schema, cfg)
    assert s["groups"] == 1 and _tx(conn, out)["category"] == "investing_aggressive"


def test_real_data_patterns(conn, schema, cfg):
    # Wise fee: $2,811.28 out arrives as ~$2,795 after FX -> still one transfer
    w_out = add_tx(conn, "checking", "2026-06-01", -2811.28, "Wise Inc WISE ***Wise")
    w_in = add_tx(conn, "wise_eur", "2026-06-01", 2400.0, "Topped up account", currency="EUR", usd=2795.0)
    # loan payment split into principal + interest on the loan side; hub leg already guessed by plaid
    l_out = add_tx(conn, "checking", "2026-06-15", -616.79, "ICPAYMENT", category="other", category_source="plaid")
    p_in = add_tx(conn, "car_loan", "2026-06-15", 604.00, "PRINCIPAL")
    i_in = add_tx(conn, "car_loan", "2026-06-15", 12.79, "INTEREST")
    # card purchase must not pair with a hub inflow of the same amount (Zelle)
    z_in = add_tx(conn, "checking", "2026-06-20", 496.00, "Zelle: ALEX RIVERA")
    buy = add_tx(conn, "card_everyday", "2026-06-20", -496.00, "LUFTHANSA")
    # statement credit is a refund, not an unmatched payment
    credit = add_tx(conn, "card_travel", "2026-06-08", 25.00, "Platinum Digital Entertainment Credit")
    # Robinhood deposit only exists as an investment contribution, 5 days later
    rh = add_tx(conn, "checking", "2026-06-02", -625.0, "Robinhood")
    conn.execute("INSERT INTO investment_transactions (account_id, date, type, amount_usd, source, external_id) "
                 "VALUES ('roth_ira', '2026-06-07', 'contribution', 625.0, 'test', 'c1')")
    conn.commit()
    s = _run(conn, schema, cfg)
    assert _tx(conn, w_out)["is_transfer"] == 1 and _tx(conn, w_in)["is_transfer"] == 1
    assert _tx(conn, p_in)["is_transfer"] == 1 and _tx(conn, i_in)["is_transfer"] == 1
    assert _tx(conn, l_out)["is_transfer"] == 0  # hub leg stays the debt expense
    assert _tx(conn, z_in)["is_transfer"] == 0 and _tx(conn, buy)["is_transfer"] == 0
    assert _tx(conn, credit)["needs_review"] == 0
    r = _tx(conn, rh)
    assert (r["is_transfer"], r["category"]) == (1, "investing_roth_ira")
    assert s["unmatched_review"] == 0


def test_card_payment_before_hub_history_is_a_transfer(conn, schema, cfg):
    add_tx(conn, "checking", "2026-05-01", -10.0, "SOMETHING")
    old = add_tx(conn, "card_everyday", "2025-01-05", 900.0, "AUTOPAY PAYMENT - THANK YOU")
    _run(conn, schema, cfg)
    r = _tx(conn, old)
    assert (r["is_transfer"], r["needs_review"]) == (1, 0)
