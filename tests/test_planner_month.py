from datetime import date

import pytest

from finance.planner import month as month_plan
from finance.planner.common import PlannerError
from tests.conftest import add_fx, add_tx, seed_month, spend


def test_plan_vs_actual_past_month(conn, schema):
    seed_month(conn, "2026-09")
    p = month_plan.build(conn, schema, "2026-09", today=date(2026, 11, 5))
    assert not p.is_current
    rent = p.line("housing_rent")
    assert rent.planned_usd == pytest.approx(1650.0)  # 1500 EUR * 1.10
    assert rent.actual_usd == pytest.approx(1650.0)
    assert rent.paid is True and rent.variance_usd == 0
    g = p.line("groceries")
    assert g.actual_usd == 380 and g.variance_usd == -20 and g.pct_used == 95.0
    assert g.projected_usd is None  # only for the current month
    assert p.line("insurance_car").paid is False
    assert p.line("investing_aggressive").actual_usd == 1000
    assert p.line("investing_roth_ira").pct_used == pytest.approx(100.0)
    assert p.line("saving_emergency_fund").pct_used is None  # planned 0
    assert all(l.group != "income" for l in p.lines)
    assert p.observed_income == 5900 and p.planned_net_income == 5900 and p.income_variance == 0


def test_pace_projection_current_month(conn, schema):
    add_fx(conn, "2026-11", 1.08)
    spend(conn, "card_everyday", "2026-11-03", 100, "groceries")
    p = month_plan.build(conn, schema, "2026-11", today=date(2026, 11, 10))
    assert p.is_current and p.days_elapsed == 10 and p.days_in_month == 30
    assert p.line("groceries").projected_usd == pytest.approx(300.0)
    assert p.line("dining").projected_usd == 0
    assert p.line("housing_rent").projected_usd is None  # fixed: paid flag instead
    assert p.line("housing_rent").paid is False


def test_eur_rate_required(conn, schema):
    spend(conn, "card_everyday", "2026-10-03", 100, "groceries")
    with pytest.raises(PlannerError, match="EURUSD"):
        month_plan.build(conn, schema, "2026-10", today=date(2026, 11, 5))


def test_wise_rate_alone_is_not_enough(conn, schema):
    add_fx(conn, "2026-10", 1.09, source="wise")
    with pytest.raises(PlannerError, match="ecb"):
        month_plan.build(conn, schema, "2026-10", today=date(2026, 11, 5))


def test_reimbursements_net_against_category(conn, schema):
    add_fx(conn, "2026-10", 1.10)
    spend(conn, "checking", "2026-10-04", 132, "phone", "MOBILE CARRIER")
    add_tx(conn, "wise_eur", "2026-10-06", 30, "SAM", currency="EUR", usd=33.0, category="phone",
           category_source="rule", is_reimbursement=1)
    p = month_plan.build(conn, schema, "2026-10", today=date(2026, 11, 5))
    ph = p.line("phone")
    assert ph.actual_usd == pytest.approx(99.0)
    assert ph.reimbursed_usd == pytest.approx(33.0)
    assert ph.planned_usd == pytest.approx(132.0)
    assert ph.paid is True
    assert p.observed_income == 0  # reimbursement is never income
    assert p.totals["reimbursed_usd"] == pytest.approx(33.0)


def test_income_variance_is_a_number(conn, schema):
    seed_month(conn, "2026-09", pay=5750.0)
    p = month_plan.build(conn, schema, "2026-09", today=date(2026, 11, 5))
    assert p.income_variance == -150.0


def test_uncategorized_and_bad_month(conn, schema):
    add_fx(conn, "2026-10", 1.1)
    add_tx(conn, "card_everyday", "2026-10-10", -55, "MYSTERY SHOP")
    p = month_plan.build(conn, schema, "2026-10", today=date(2026, 11, 5))
    assert p.uncategorized_usd == 55
    with pytest.raises(PlannerError):
        month_plan.build(conn, schema, "2026-13", today=date(2026, 11, 5))


def test_dashboard_uses_planner(conn, schema, cfg, monkeypatch):
    from starlette.requests import Request

    from finance.app import main

    seed_month(conn, "2026-09")
    monkeypatch.setattr(main, "cfg", lambda: cfg)
    resp = main.dashboard(Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": [], "query_string": b""}), month="2026-09")
    html = resp.body.decode()
    assert "housing_rent" in html and "1650.00" in html and "95%" in html


def test_dashboard_reports_missing_rate(conn, schema, cfg, monkeypatch):
    from starlette.requests import Request

    from finance.app import main

    monkeypatch.setattr(main, "cfg", lambda: cfg)
    resp = main.dashboard(Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": [], "query_string": b""}), month="2026-10")
    assert "no EURUSD (ecb) rate" in resp.body.decode()
