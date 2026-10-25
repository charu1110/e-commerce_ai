"""Bot behaviour without Telegram: linking, offers, buttons, opt-out."""
import json
import shutil
from datetime import datetime, time

import pytest

from src import db
from src.bot_logic import BotService
from src.engine.state import Deps
from tests.conftest import TODAY

NOON = datetime.combine(TODAY, time(14, 0))
ALICE, BOB = 111, 222


@pytest.fixture
def svc(gen_db_path, tmp_path):
    path = tmp_path / "bot.db"
    shutil.copy(gen_db_path, path)
    conn = db.connect(path)
    yield BotService(conn, Deps.load(conn, on_demand=True, channel="telegram"), now=lambda: NOON)
    conn.close()


def status_of(svc, decision_id):
    return svc.conn.execute("SELECT status FROM decision_log WHERE decision_id = ?", (decision_id,)).fetchone()[0]


def test_link_picks_requested_type_and_relinks_cleanly(svc):
    cid = svc.link(ALICE, "lapsing")
    assert svc.segments[cid] == "lapsing"
    assert svc.linked_customer(ALICE) == cid
    other = svc.link(ALICE, "deal_hunter")
    assert other != cid and svc.segments[other] == "deal_hunter"
    assert svc.conn.execute("SELECT telegram_chat_id FROM customers WHERE customer_id = ?", (cid,)).fetchone()[0] is None


def test_two_classmates_never_share_a_customer(svc):
    assert svc.link(ALICE, "lapsing") != svc.link(BOB, "lapsing")


def test_link_prefers_customers_who_get_an_offer(svc):
    svc.link(ALICE, "lapsing")
    text, decision_id = svc.my_offer(ALICE)
    assert decision_id is not None, text


def test_who_card(svc):
    cid = svc.link(ALICE, "lapsing")
    card = svc.who_card(cid)
    name = svc.conn.execute("SELECT name FROM customers WHERE customer_id = ?", (cid,)).fetchone()[0]
    assert name in card and "Lapsing regular" in card and "/myoffer" in card


def test_myoffer_requires_link(svc):
    text, decision_id = svc.my_offer(ALICE)
    assert decision_id is None and "/start" in text


def test_offer_then_cooldown(svc):
    svc.link(ALICE, "lapsing")
    card, decision_id = svc.my_offer(ALICE)
    assert "Code:" in card and status_of(svc, decision_id) == "sent"
    text, again = svc.my_offer(ALICE)
    assert again is None and "No offer" in text


def test_claim_is_recorded_once(svc):
    svc.link(ALICE, "lapsing")
    _, d = svc.my_offer(ALICE)
    r = svc.handle_button(ALICE, "claim", d)
    assert r.remove_buttons and "Claimed" in r.append_to_card
    assert status_of(svc, d) == "claimed"
    second = svc.handle_button(ALICE, "claim", d)
    assert "already claimed" in second.toast
    events = [e[0] for e in svc.conn.execute("SELECT event FROM offer_events WHERE decision_id = ? ORDER BY event_id", (d,))]
    assert events == ["sent", "claimed"]


def test_not_now(svc):
    svc.link(ALICE, "lapsing")
    _, d = svc.my_offer(ALICE)
    assert "Not now" in svc.handle_button(ALICE, "dismiss", d).append_to_card
    assert status_of(svc, d) == "dismissed"


def test_someone_else_cannot_claim_your_offer(svc):
    svc.link(ALICE, "lapsing")
    svc.link(BOB, "deal_hunter")
    _, d = svc.my_offer(ALICE)
    assert svc.handle_button(BOB, "claim", d).toast == "This offer isn't yours."
    assert status_of(svc, d) == "sent"


def test_why_explains_and_is_logged(svc):
    svc.link(ALICE, "lapsing")
    _, d = svc.my_offer(ALICE)
    r = svc.handle_button(ALICE, "why", d)
    assert "Why this offer" in r.reply and "What we noticed" in r.reply and "rule checks" in r.reply
    assert status_of(svc, d) == "sent"  # asking why doesn't use up the offer
    assert svc.conn.execute("SELECT COUNT(*) FROM offer_events WHERE decision_id = ? AND event = 'why'", (d,)).fetchone()[0] == 1


def test_expired_offer_cannot_be_claimed(svc):
    svc.link(ALICE, "lapsing")
    _, d = svc.my_offer(ALICE)
    svc.conn.execute("UPDATE decision_log SET expires_on = '2026-01-01' WHERE decision_id = ?", (d,))
    assert "expired" in svc.handle_button(ALICE, "claim", d).toast
    assert status_of(svc, d) == "expired"


def test_stop_and_resume(svc):
    svc.link(ALICE, "lapsing")
    assert svc.set_opt_in(ALICE, False)
    text, d = svc.my_offer(ALICE)
    assert d is None and "opted out" in text
    svc.set_opt_in(ALICE, True)
    assert svc.my_offer(ALICE)[1] is not None


def test_bot_app_builds_without_network():
    from src.bot import COMMANDS, build_app
    app = build_app("123456:TEST-TOKEN", svc=None)
    names = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert {c for c, _ in COMMANDS} <= names


# ------------------------------------------------------------------ Step 9: /why, /history, admin
def test_why_before_any_decision(svc):
    svc.link(ALICE, "lapsing")
    assert "Try /myoffer" in svc.why_last(ALICE)


def test_why_explains_an_offer(svc):
    svc.link(ALICE, "lapsing")
    svc.my_offer(ALICE)
    text = svc.why_last(ALICE)
    assert "Why this offer" in text and "What we noticed" in text


def test_why_explains_no_offer(svc):
    svc.link(ALICE, "lapsing")
    svc.my_offer(ALICE)
    svc.my_offer(ALICE)  # second ask: cooldown
    text = svc.why_last(ALICE)
    assert "Why no offer" in text and "Cooldown" in text


def test_why_names_the_rule_that_blocked_the_ai(gen_db_path, tmp_path):
    path = tmp_path / "why.db"
    shutil.copy(gen_db_path, path)
    conn = db.connect(path)
    greedy = lambda summary, options: json.dumps({  # noqa: E731
        "offer_type": "percent_off", "value": 90, "category": options[0]["category"], "expiry_days": 5,
        "min_order_inr": 999, "message": "Hi, 90% off! Valid for 5 days.", "reason": "greedy"})
    s = BotService(conn, Deps.load(conn, agent=greedy, agent_name="greedy", on_demand=True), now=lambda: NOON)
    s.link(ALICE, "lapsing")
    _, d = s.my_offer(ALICE)
    assert d is not None
    text = s.why_last(ALICE)
    assert "blocked by our rules" in text and "discount above a cap" in text
    conn.close()


def test_history_lists_offers_with_status(svc):
    svc.link(ALICE, "lapsing")
    _, d = svc.my_offer(ALICE)
    svc.handle_button(ALICE, "claim", d)
    text = svc.history(ALICE)
    assert "Your last offers" in text and "claimed" in text


def test_admin_only(svc):
    assert not svc.is_admin(ALICE)
    svc.admin_ids = {ALICE}
    assert svc.is_admin(ALICE) and not svc.is_admin(BOB)


def test_stats_and_kill_switch(svc):
    svc.link(ALICE, "lapsing")
    svc.my_offer(ALICE)
    text = svc.stats_text()
    assert "offers out: 1" in text and "Budget used" in text and "Offers running" in text
    svc.set_paused(True)
    svc.link(BOB, "deal_hunter")
    reply, d = svc.my_offer(BOB)
    assert d is None and "kill switch" in reply
    assert "PAUSED" in svc.stats_text()
    svc.set_paused(False)
    assert svc.my_offer(BOB)[1] is not None


def test_audit_trail(svc):
    cid = svc.link(ALICE, "lapsing")
    svc.my_offer(ALICE)
    svc.my_offer(ALICE)
    text = svc.audit_text(cid.lower())
    assert f"Audit: {cid}" in text and "sent" in text and "skipped" in text and "Cooldown" in text
    assert "No customer" in svc.audit_text("C99999")
