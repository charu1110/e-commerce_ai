"""End-to-end pipeline tests on a copy of the generated test database."""
import json
import random
import shutil
from datetime import datetime, time

import pytest

from src import db
from src.engine.pipeline import priority_order, run_batch, run_pipeline
from src.engine.nodes import in_holdout
from src.engine.state import Deps
from src.features import compute_features
from src.guardrails import in_quiet_hours, validate
from tests.conftest import TODAY

NOON = datetime.combine(TODAY, time(14, 0))


@pytest.fixture
def conn(gen_db_path, tmp_path):
    """A fresh writable copy per test, so decisions logged in one test don't leak into another."""
    path = tmp_path / "pipe.db"
    shutil.copy(gen_db_path, path)
    c = db.connect(path)
    yield c
    c.close()


def deps_for(conn, agent=None, **kw):
    return Deps.load(conn, agent=agent, agent_name="test" if agent else None, **kw)


def find(conn, want_status: str, trigger: str | None = None) -> str:
    """A customer whose dry-run decision has the wanted status (and trigger)."""
    deps = deps_for(conn, dry_run=True)
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    for s in run_batch(ids, deps, NOON):
        if s.status == want_status and (trigger is None or s.trigger == trigger):
            return s.customer_id
    pytest.skip(f"no customer with status {want_status} / trigger {trigger}")


def log_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM decision_log ORDER BY decision_id")]


# ------------------------------------------------------------------ happy path
def test_lapsed_buyer_gets_a_logged_valid_offer(conn):
    cid = find(conn, "sent", "lapsed_buyer")
    s = run_pipeline(cid, deps_for(conn), NOON)
    assert s.path == ["1 Load customer", "2 Detect trigger", "3 Segment & score", "4 Eligibility check",
                      "5 AI offer agent", "6 Guardrail validator", "7 Send on Telegram", "8 Track & learn"]
    assert s.status == "sent" and s.final_offer_id == "AI"
    assert s.first_name in s.card_text and s.coupon_code in s.card_text
    rows = log_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["customer_id"] == cid and row["status"] == "sent" and row["coupon_code"] == s.coupon_code
    assert row["trigger"] == "lapsed_buyer" and row["segment"] == s.segment
    assert json.loads(row["validator_result"])["passed"] is True
    events = conn.execute("SELECT event FROM offer_events WHERE decision_id = ?", (row["decision_id"],)).fetchall()
    assert [e["event"] for e in events] == ["sent"]


def test_every_pass_writes_exactly_one_row(conn):
    deps = deps_for(conn)
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers LIMIT 40")]
    states = run_batch(ids, deps, NOON)
    assert len(log_rows(conn)) == len(states) == 40
    assert {s.status for s in states} <= {"sent", "queued", "skipped", "holdout", "no_offer"}


def test_dry_run_writes_nothing(conn):
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers LIMIT 30")]
    run_batch(ids, deps_for(conn, dry_run=True), NOON)
    assert log_rows(conn) == []


def test_unknown_customer_is_skipped_safely(conn):
    s = run_pipeline("C99999", deps_for(conn), NOON)
    assert s.status == "skipped" and s.path == ["1 Load customer", "9 Skip or holdout"]


# ------------------------------------------------------------------ eligibility (Node 4 -> 9)
def test_second_offer_within_cooldown_is_skipped(conn):
    cid = find(conn, "sent")
    deps = deps_for(conn)
    assert run_pipeline(cid, deps, NOON).status == "sent"
    again = run_pipeline(cid, deps, NOON)
    assert again.status == "skipped"
    assert "Cooldown" in again.skip_reason


def test_opted_out_customer_is_skipped(conn):
    cid = find(conn, "sent")
    conn.execute("UPDATE customers SET marketing_opt_in = 0 WHERE customer_id = ?", (cid,))
    s = run_pipeline(cid, deps_for(conn), NOON)
    assert s.status == "skipped" and "opted out" in s.skip_reason


def test_kill_switch_stops_everything(conn):
    db.set_setting(conn, "paused", "1")
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers LIMIT 50")]
    states = run_batch(ids, deps_for(conn), NOON)
    assert all(s.status == "skipped" and "kill switch" in s.skip_reason for s in states)


def test_holdout_customers_get_nothing_and_are_logged(conn):
    cid = find(conn, "holdout")
    assert in_holdout(cid, deps_for(conn).rules.global_.holdout_pct)
    s = run_pipeline(cid, deps_for(conn), NOON)
    assert s.final_offer is None and s.is_holdout
    assert log_rows(conn)[0]["is_holdout"] == 1


def test_holdout_is_about_ten_percent(conn):
    ids = [f"C{i:05d}" for i in range(1, 5001)]
    share = sum(in_holdout(c, 10) for c in ids) / len(ids)
    assert 0.08 <= share <= 0.12


def test_budget_runs_out_cleanly(conn):
    cid = find(conn, "sent", "lapsed_buyer")
    deps = deps_for(conn)
    deps._spent_today[TODAY] = 19_990.0
    s = run_pipeline(cid, deps, NOON)
    assert s.status in ("no_offer", "sent")
    if s.status == "no_offer":
        assert "budget" in s.skip_reason
    else:  # only a near-free offer can still fit
        assert s.final_validation.expected_cost_inr <= 10


# ------------------------------------------------------------------ AI failures (Node 5/6 -> 10)
def test_ai_error_falls_back_to_safe_offer(conn):
    cid = find(conn, "sent", "lapsed_buyer")

    def broken(summary, options):
        raise TimeoutError("AI took longer than 10 s")

    s = run_pipeline(cid, deps_for(conn, agent=broken), NOON)
    assert s.fallback_used and s.final_offer_id not in (None, "AI")
    assert "10 Fallback offer" in s.path and s.status == "sent"
    assert "TimeoutError" in json.loads(log_rows(conn)[0]["violations"])[0]


@pytest.mark.parametrize("bad_output", [
    "Sure! Here is your offer: 90% off everything",                         # not JSON
    json.dumps({"offer_type": "percent_off", "value": 90, "category": "fashion", "expiry_days": 5,
                "message": "Hi, 90% off fashion! Valid for 5 days.", "reason": "be generous"}),
    json.dumps({"offer_type": "percent_off", "value": 10, "category": "fashion", "expiry_days": 5,
                "message": "Hi, 50% off fashion! Valid for 5 days.", "reason": "text lies"}),
])
def test_bad_ai_offer_is_blocked_and_replaced(conn, bad_output):
    cid = find(conn, "sent", "lapsed_buyer")
    s = run_pipeline(cid, deps_for(conn, agent=lambda summary, options: bad_output), NOON)
    assert not s.validation.passed
    assert s.fallback_used
    if s.final_offer:
        assert validate(s.final_offer, s.ctx, deps_for(conn).rules).passed
    row = log_rows(conn)[0]
    assert row["ai_proposal_json"] == bad_output
    assert json.loads(row["violations"])


def chaos_agent(seed):
    """Proposes random offers, many of them breaking rules on purpose."""
    rng = random.Random(seed)

    def agent(summary, options):
        name = summary.get("first_name") or "there"
        t = rng.choice(["percent_off", "flat_off", "free_delivery", "bonus_points", "early_access", "free_iphone"])
        value = rng.choice([0, 5, 10, 15, 20, 40, 90, 300, 800, 5000])
        claimed = rng.choice([value, value * 2, 50])
        return json.dumps({
            "offer_type": t, "value": value, "category": rng.choice([None, "fashion", "electronics", "grocery", "toys"]),
            "expiry_days": rng.choice([1, 3, 5, 7, 30]), "min_order_inr": rng.choice([99, 499, 999]),
            "message": rng.choice([f"Hi {name}, {claimed}% off! Valid for 5 days.", f"Hi {name}, a treat for you.",
                                   f"Hi {name}, guaranteed lowest price!", "Hello, click www.deal.com"]),
            "reason": "chaos"})
    return agent


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_rule_breaking_offer_ever_goes_out(conn, seed):
    """Core guarantee: whatever the AI proposes, every offer that is sent passes every rule."""
    deps = deps_for(conn, agent=chaos_agent(seed))
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    states = run_batch(ids, deps, NOON)
    sent = [s for s in states if s.status in ("sent", "queued")]
    assert sent, "expected at least some offers"
    for s in sent:
        assert validate(s.final_offer, s.ctx, deps.rules).passed, (s.customer_id, s.final_offer)
    blocked = [s for s in states if s.validation and not s.validation.passed]
    assert blocked, "chaos agent should have been blocked at least once"


# ------------------------------------------------------------------ sending (Node 7)
def test_quiet_hours_queue_until_active_hour(conn):
    cid = find(conn, "sent")
    night = datetime.combine(TODAY, time(23, 30))
    s = run_pipeline(cid, deps_for(conn, dry_run=True), night)
    assert s.status == "queued" and s.send_at > night
    assert not in_quiet_hours(s.send_at.hour, deps_for(conn).rules)


def test_on_demand_sends_now_even_at_night(conn):
    cid = find(conn, "sent")
    night = datetime.combine(TODAY, time(23, 30))
    s = run_pipeline(cid, deps_for(conn, dry_run=True, on_demand=True), night)
    assert s.status == "sent" and s.send_at == night


def test_coupon_codes_are_unique(conn):
    deps = deps_for(conn)
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    codes = [s.coupon_code for s in run_batch(ids, deps, NOON) if s.coupon_code]
    assert codes and len(codes) == len(set(codes))


# ------------------------------------------------------------------ triggers / priority
def test_lapsed_buyer_beats_abandoned_cart(conn):
    deps = deps_for(conn, dry_run=True)
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    for s in run_batch(ids, deps, NOON):
        f = s.features
        if s.trigger == "abandoned_cart" and f.get("n_orders", 0) >= 3 and f.get("typical_gap"):
            assert not (f["gap_ratio"] > 2 and f["days_since_last_order"] < 180), s.customer_id


def test_budget_priority_puts_valuable_churners_first(conn):
    feats = compute_features(conn, TODAY)
    order = priority_order(list(feats.index), feats)
    top, bottom = feats.loc[order[:50]], feats.loc[order[-50:]]
    assert top["churn_risk"].mean() > bottom["churn_risk"].mean()
    assert (top["buy_anyway"].mean()) < 0.3
