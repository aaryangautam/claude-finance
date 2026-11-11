import dataclasses
from datetime import date

import pytest

from finance.planner import forecast, goals as goals_mod
from finance.planner.common import PlannerError
from finance.vault import SchemaError, WaterfallStep
from tests.conftest import add_fx, add_snapshot, seed_month, spend

TODAY = date(2026, 11, 5)
# fixture schema at EURUSD 1.10: fixed 2530, variable 1700, allocations 1583, planned net 5900 -> surplus 87
BASE_SURPLUS = 87.0


def _fc(conn, schema, **kw):
    kw.setdefault("today", TODAY)
    return forecast.build(conn, schema, **kw)


def test_planned_forecast(conn, schema):
    add_fx(conn, "2026-10", 1.10)
    add_snapshot(conn, "checking", "2026-10-31", 3000)
    fc = _fc(conn, schema)
    assert [m.month for m in fc.months] == ["2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05"]
    m = fc.months[0]
    assert (m.income_usd, m.fixed_usd, m.variable_usd, m.allocations_usd) == (5900, 2530, 1700, 1583)
    assert m.surplus_usd == BASE_SURPLUS
    assert [m.hub_balance_usd for m in fc.months[:2]] == [3087, 3174]
    assert fc.hub_start_balance == (3000, "2026-10-31")
    assert all(b.used == "planned" for b in fc.basis)


def test_blend_switches_after_three_complete_months(conn, schema):
    for m in ("2026-07", "2026-08", "2026-09", "2026-10"):
        seed_month(conn, m)  # groceries 380, dining 260
    spend(conn, "card_everyday", "2026-08-12", 300, "travel")  # one trip in the window -> avg 100
    spend(conn, "card_everyday", "2026-10-12", 90, "health")  # first seen in Oct: < 3 months -> planned
    spend(conn, "card_everyday", "2026-11-02", 999, "groceries")  # current month is incomplete: ignored
    fc = _fc(conn, schema, with_balance=False)
    b = {x.id: x for x in fc.basis}
    assert fc.trailing_window == ["2026-10", "2026-09", "2026-08"]
    assert (b["groceries"].used, b["groceries"].trailing_avg_usd, b["groceries"].planned_usd) == ("trailing_avg", 380, 400)
    assert b["dining"].forecast_usd == 260
    assert b["travel"].trailing_avg_usd == 100
    assert (b["health"].used, b["health"].trailing_avg_usd, b["health"].forecast_usd) == ("planned", None, 50)
    assert b["investing_aggressive"].used == "planned"  # allocations always planned
    assert b["housing_rent"].used == "planned"  # fixed always planned
    assert fc.months[0].variable_usd == 1700 - 400 - 250 - 300 + 380 + 260 + 100
    assert fc.months[0].hub_balance_usd is None


def test_two_months_is_not_enough(conn, schema):
    for m in ("2026-09", "2026-10"):
        seed_month(conn, m)
    fc = _fc(conn, schema, with_balance=False)
    g = next(x for x in fc.basis if x.id == "groceries")
    assert g.used == "planned" and g.trailing_avg_usd is None


def test_forecast_raises_without_snapshot_or_rate(conn, schema):
    with pytest.raises(PlannerError, match="EURUSD"):
        _fc(conn, schema, with_balance=False)
    add_fx(conn, "2026-10", 1.10)
    with pytest.raises(PlannerError, match="snapshot"):
        _fc(conn, schema)
    assert len(_fc(conn, schema, with_balance=False, months=3).months) == 3


def test_waterfall_parse(schema):
    rules = goals_mod.parse_waterfall(schema)
    assert [(r.kind, r.account, r.target) for r in rules] == [
        ("baseline", None, None), ("hold", "savings", 10000), ("sink", "brokerage", None)]
    bad = dataclasses.replace(schema, waterfall=[WaterfallStep(1, "buy a boat")])
    with pytest.raises(SchemaError, match="interpret"):
        goals_mod.parse_waterfall(bad)


def _setup(conn, ef=9900.0):
    add_fx(conn, "2026-10", 1.10)
    add_snapshot(conn, "checking", "2026-10-31", 3000)
    add_snapshot(conn, "savings", "2026-10-31", ef)
    from tests.conftest import add_tx

    add_tx(conn, "checking", "2026-10-03", -1000, "ROBINHOOD", category="investing_aggressive", category_source="rule", is_transfer=1)


def test_waterfall_fills_target_then_sinks(conn, schema):
    _setup(conn)
    wf = goals_mod.simulate(conn, schema, _fc(conn, schema))
    assert wf[0].moves == {"savings": 87, "brokerage": 0} and wf[0].balances["savings"] == 9987
    assert wf[1].moves == {"savings": 13, "brokerage": 74}
    assert wf[2].moves == {"savings": 0, "brokerage": 87}
    assert all(w.unallocated_usd == 0 for w in wf)


def test_waterfall_releases_excess(conn, schema):
    _setup(conn, ef=10400)
    wf = goals_mod.simulate(conn, schema, _fc(conn, schema))
    assert wf[0].moves == {"savings": -400, "brokerage": 487}
    assert wf[0].balances["savings"] == 10000


def test_goal_projections(conn, schema, goals):
    _setup(conn)
    fc = _fc(conn, schema)
    res = {g.title: g for g in goals_mod.project_goals(conn, schema, goals, fc)}
    assert "Old Car Fund" not in res  # inactive
    ef = res["Emergency Fund"]
    assert (ef.status, ef.current_usd, ef.projected_month, ef.on_track) == ("tracked", 9900, "2027-01", True)
    ag = res["Aggressive Investing"]
    assert ag.current_usd == 1000 and ag.path[:3] == [2000, 3074, 4161]
    assert ag.beyond_horizon and ag.projected_month == "2028-05" and ag.on_track is False
    roth = res["Roth IRA"]
    assert roth.status == "not_trackable" and "progress_source" in roth.reason


def test_goal_reached_and_bad_source(conn, schema, goals):
    _setup(conn, ef=10400)
    fc = _fc(conn, schema)
    ef = next(g for g in goals_mod.project_goals(conn, schema, goals, fc) if g.title == "Emergency Fund")
    assert ef.status == "reached" and ef.on_track is True
    bad = [dataclasses.replace(goals[0], progress_source="ghost_account")]
    with pytest.raises(SchemaError, match="ghost_account"):
        goals_mod.project_goals(conn, schema, bad, fc)


def test_contribution_rate_goal_is_per_month(conn, schema, goals):
    # A $500/mo rate must not read as "reached" once cumulative contributions pass $500.
    _setup(conn)
    fc = _fc(conn, schema)
    ag = next(g for g in goals if g.title == "Aggressive Investing")
    rate = dataclasses.replace(ag, goal_type="contribution-rate", target_amount=500, target_date=None)
    gp = goals_mod.project_goals(conn, schema, [rate], fc)[0]
    assert gp.status == "rate" and gp.projected_month is None
    assert gp.current_usd == round(1000 / 3, 2)  # Oct contribution averaged over Aug..Oct
    assert gp.path[:3] == [1000, 1074, 1087] and gp.on_track is True
    high = dataclasses.replace(rate, target_amount=1100)
    assert goals_mod.project_goals(conn, schema, [high], fc)[0].on_track is False
    with pytest.raises(SchemaError, match="measure=contributions"):
        goals_mod.project_goals(conn, schema, [dataclasses.replace(rate, measure="balance")], fc)
