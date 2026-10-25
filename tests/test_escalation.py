"""Escalation ladder, scheduler jobs and /timetravel."""
import shutil
from datetime import datetime, time, timedelta

import pytest
import yaml

from src import db, scheduler
from src.bot_logic import BotService
from src.engine.pipeline import run_batch, run_pipeline
from src.engine.state import Deps
from src.guardrails import parse_rules
from tests.conftest import TODAY

NOON = datetime.combine(TODAY, time(14, 0))


def day(n: int) -> datetime:
    return NOON + timedelta(days=n)


@pytest.fixture
def conn(gen_db_path, tmp_path):
    path = tmp_path / "esc.db"
    shutil.copy(gen_db_path, path)
    c = db.connect(path)
    yield c
    c.close()


def lapsing_customer(conn) -> str:
    """A lapsed fashion/beauty buyer who gets an offer today (so every ladder step fits the caps)."""
    probe = Deps.load(conn, dry_run=True)
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    for s in run_batch(ids, probe, NOON):
        favs = (s.features.get("favourite_categories") or "").split(",")
        if s.status == "sent" and s.segment == "lapsing" and favs[0] in ("fashion", "beauty", "footwear"):
            return s.customer_id
    pytest.skip("no suitable lapsing customer")


def status(conn, decision_id):
    return conn.execute("SELECT status FROM decision_log WHERE decision_id = ?", (decision_id,)).fetchone()[0]


# ------------------------------------------------------------------ the ladder
def test_full_ladder_when_every_offer_is_ignored(conn):
    cid = lapsing_customer(conn)
    deps = Deps.load(conn)
    first = run_pipeline(cid, deps, day(0))
    assert first.status == "sent" and first.ladder_step == 0

    assert run_pipeline(cid, deps, day(2)).status == "skipped"           # still live / cooldown

    step1 = run_pipeline(cid, deps, day(first.final_offer.expiry_days + 1))
    assert step1.ladder_step == 1 and step1.final_offer.offer_type == "free_delivery"
    assert status(conn, first.decision_id) == "sent"  # pipeline doesn't rewrite history; the scheduler does

    t2 = step1.now + timedelta(days=step1.final_offer.expiry_days + 1)
    step2 = run_pipeline(cid, deps, t2)
    assert step2.ladder_step == 2 and step2.final_offer.offer_type == "percent_off"
    assert step2.final_offer.value <= 10

    t3 = t2 + timedelta(days=step2.final_offer.expiry_days + 1)
    step3 = run_pipeline(cid, deps, t3)
    assert step3.ladder_step == 3 and step3.final_offer.offer_type == "percent_off"
    assert step2.final_offer.value < step3.final_offer.value <= 15

    t4 = t3 + timedelta(days=step3.final_offer.expiry_days + 1)
    done = run_pipeline(cid, deps, t4)
    assert done.status == "skipped" and "ladder finished" in done.skip_reason
    rows = conn.execute("SELECT ladder_step FROM decision_log WHERE customer_id = ? AND final_offer_id IS NOT NULL "
                        "ORDER BY decision_id", (cid,)).fetchall()
    assert [r[0] for r in rows] == [0, 1, 2, 3]


def test_not_now_means_no_follow_up(conn):
    cid = lapsing_customer(conn)
    deps = Deps.load(conn)
    first = run_pipeline(cid, deps, day(0))
    conn.execute("UPDATE decision_log SET status = 'dismissed' WHERE decision_id = ?", (first.decision_id,))
    s = run_pipeline(cid, deps, day(6))
    assert s.status == "skipped" and "Cooldown" in s.skip_reason


def test_claimed_offer_resets_the_ladder(conn):
    cid = lapsing_customer(conn)
    deps = Deps.load(conn)
    first = run_pipeline(cid, deps, day(0))
    conn.execute("UPDATE decision_log SET status = 'claimed' WHERE decision_id = ?", (first.decision_id,))
    assert "Cooldown" in run_pipeline(cid, deps, day(6)).skip_reason
    later = run_pipeline(cid, deps, day(8))
    assert later.ladder_step == 0


def test_ladder_waits_its_wait_days(conn):
    cid = lapsing_customer(conn)
    data = yaml.safe_load(open("config/rules.yaml", encoding="utf-8"))
    data["escalation_ladder"][0]["wait_days"] = 20
    deps = Deps.load(conn)
    deps.rules = parse_rules(data)
    first = run_pipeline(cid, deps, day(0))
    s = run_pipeline(cid, deps, day(first.final_offer.expiry_days + 1))
    assert s.status == "skipped" and "Escalation: waiting 20 days" in s.skip_reason


def test_step_not_allowed_for_segment_stops_the_ladder(conn):
    """VIPs never get % off, so ladder step 2 (10% off) ends their follow-ups."""
    deps = Deps.load(conn)
    vips = [cid for cid, seg in BotService(conn, deps, now=lambda: NOON).segments.items() if seg == "vip"]
    found = False
    for cid in vips:
        for k in (2, 1):  # two ignored offers, 20 and 10 days ago
            conn.execute(
                "INSERT INTO decision_log (customer_id, ts, status, offer_type, expires_on, final_offer_id) "
                "VALUES (?, ?, 'ignored', 'bonus_points', ?, 'AI')",
                (cid, (NOON - timedelta(days=10 * k)).isoformat(sep=" "), (NOON - timedelta(days=10 * k - 3)).date().isoformat()))
        s = run_pipeline(cid, deps, NOON)
        if s.skip_reason and "Escalation step 2" in s.skip_reason:
            found = True
            assert "isn't allowed" in s.skip_reason
            break
    assert found


# ------------------------------------------------------------------ scheduler
def test_expire_offers_marks_ignored_and_logs_event(conn):
    cid = lapsing_customer(conn)
    first = run_pipeline(cid, Deps.load(conn), day(0))
    assert scheduler.expire_offers(conn, day(1)) == 0
    assert scheduler.expire_offers(conn, day(first.final_offer.expiry_days + 1)) == 1
    assert status(conn, first.decision_id) == "ignored"
    events = [r[0] for r in conn.execute("SELECT event FROM offer_events WHERE decision_id = ?", (first.decision_id,))]
    assert events == ["sent", "expired"]


def test_queued_offer_is_delivered_when_its_time_comes(conn):
    cid = lapsing_customer(conn)
    s = run_pipeline(cid, Deps.load(conn), day(0).replace(hour=23))
    assert s.status == "queued"
    assert scheduler.due_queued(conn, day(0).replace(hour=23, minute=30)) == []
    due = scheduler.due_queued(conn, s.send_at)
    assert [r["decision_id"] for r in due] == [s.decision_id] and due[0]["card_text"]
    scheduler.mark_delivered(conn, s.decision_id, cid, s.send_at)
    assert status(conn, s.decision_id) == "sent"


def test_tick_runs_follow_ups_without_flooding_the_log(conn):
    cid = lapsing_customer(conn)
    deps = Deps.load(conn)
    first = run_pipeline(cid, deps, day(0))
    rows_before = conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    scheduler.tick(deps, day(first.final_offer.expiry_days))       # not expired yet: nothing happens
    assert conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == rows_before
    r = scheduler.tick(deps, day(first.final_offer.expiry_days + 1))
    assert r.expired == 1
    assert [s.customer_id for s in r.follow_ups] == [cid] and r.follow_ups[0].ladder_step == 1


# ------------------------------------------------------------------ bot: /timetravel and pushes
def test_timetravel_shows_the_ladder_in_the_bot(conn):
    svc = BotService(conn, Deps.load(conn, on_demand=True, channel="telegram"), now=lambda: NOON)
    svc.link(111, "lapsing")
    card, d = svc.my_offer(111)
    assert d is not None
    expiry = conn.execute("SELECT expires_on FROM decision_log WHERE decision_id = ?", (d,)).fetchone()[0]
    days = (datetime.fromisoformat(expiry).date() - TODAY).days + 1
    text = svc.time_travel(111, days)
    assert "count as ignored" in text and status(conn, d) == "ignored"
    follow, d2 = svc.my_offer(111)
    assert d2 is not None and "slipped by" in follow
    assert "ignored" in svc.history(111)
    assert "Back to the real date" in svc.time_travel(111, 0)


def test_tick_pushes_due_queued_offers_to_linked_chats(conn):
    late = datetime.combine(TODAY, time(23, 0))
    clock = {"now": late}
    svc = BotService(conn, Deps.load(conn, channel="telegram"), now=lambda: clock["now"])
    cid = svc.link(111, "lapsing")
    s = run_pipeline(cid, Deps.load(conn, channel="telegram"), late)
    assert s.status == "queued"
    assert svc.tick() == []
    clock["now"] = s.send_at
    pushes = svc.tick()
    assert pushes == [(111, s.card_text, s.decision_id)]
    assert status(conn, s.decision_id) == "sent"
