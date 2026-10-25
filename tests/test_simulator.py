"""Campaign simulator, dashboard numbers, and that the dashboard page runs."""
import json
from dataclasses import asdict
from datetime import date

import pytest

from src import db, reporting
from src.guardrails import load_rules
from src.simulator import actual_discount, offer_lift, run_simulation
from tests.conftest import TODAY


@pytest.fixture(scope="module")
def sim(gen_db_path, tmp_path_factory):
    target = tmp_path_factory.mktemp("sim") / "simulation.db"
    result = run_simulation(days=8, seed=3, source=gen_db_path, target=target, start=TODAY, progress=lambda *_: None)
    return result, target


def test_simulation_never_touches_the_source_db(gen_db_path, sim):
    c = db.connect(gen_db_path)
    assert c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'S%'").fetchone()[0] == 0


def test_simulation_has_treated_and_holdout_groups(sim):
    r, _ = sim
    assert r.treated > 0 and r.holdout > 0
    assert 0 <= r.conv_holdout <= 1 and 0 <= r.conv_treated <= 1
    assert r.funnel["decisions"] == r.customers * r.days
    assert r.funnel["claimed"] <= r.funnel["offers_sent"] <= r.funnel["eligible"] <= r.funnel["decisions"]


def test_offers_create_uplift_and_the_money_adds_up(sim):
    r, _ = sim
    assert r.uplift_pp > 0                       # offers change behaviour vs the holdout
    assert r.wasted_cost <= r.discount_cost
    assert r.roi == pytest.approx(r.extra_margin - r.discount_cost)
    assert json.loads(json.dumps(asdict(r), default=float))  # results file is serialisable


def test_simulated_offers_still_pass_every_rule(sim):
    _, target = sim
    log = reporting.decisions(db.connect(target))
    offers = log[log["final_offer_id"].notna()]
    assert len(offers) > 0
    for v in offers["validator_result"]:
        assert json.loads(v)["passed"] is True


def test_holdout_customers_never_got_an_offer(sim):
    _, target = sim
    c = db.connect(target)
    bad = c.execute("""SELECT COUNT(*) FROM decision_log h JOIN decision_log o USING (customer_id)
                       WHERE h.is_holdout = 1 AND o.final_offer_id IS NOT NULL""").fetchone()[0]
    assert bad == 0


def test_response_model_makes_sense():
    rules = load_rules()
    lapsed = offer_lift("percent_off", 15, 1500, "lapsing", 0.5, still_browsing=True)
    gone = offer_lift("percent_off", 15, 1500, "lapsing", 0.5, still_browsing=False)
    vip = offer_lift("percent_off", 15, 1500, "vip", 0.1, still_browsing=True)
    assert lapsed > gone and lapsed > vip
    assert offer_lift("percent_off", 20, 1500, "deal_hunter", 0.9, True) > offer_lift("free_delivery", 0, 1500,
                                                                                       "deal_hunter", 0.9, True)
    assert actual_discount("percent_off", 20, 10_000, rules) == 500  # capped
    assert actual_discount("free_delivery", 0, 1000, rules) == 49


def test_reporting_numbers(sim):
    _, target = sim
    log = reporting.decisions(db.connect(target))
    f = reporting.funnel(log)
    assert list(f["count"]) == sorted(f["count"], reverse=True)
    s = reporting.summary(log)
    assert s["offers"] > 0 and 0 <= s["claim_rate"] <= 1
    assert not reporting.skip_reasons(log).empty
    assert set(reporting.offers_by(log, "segment").columns) == {"segment", "sent", "claimed", "claim_rate"}


def test_reporting_handles_an_empty_log(gen_db_path):
    log = reporting.decisions(db.connect(gen_db_path))
    assert reporting.funnel(log).empty and reporting.summary(log)["offers"] == 0
    assert reporting.blocked_by_rule(log).empty


def test_dashboard_page_runs_without_errors():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60).run()
    assert not at.exception, at.exception
    for page in ["Audit log", "Customer explorer", "Rules & calendar"]:
        at.sidebar.radio[1].set_value(page).run()
        assert not at.exception, (page, at.exception)
