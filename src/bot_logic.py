"""Everything the Telegram bot does, as plain functions (no Telegram code here).

Business reason: one bot serves the whole class. Each classmate is linked to one
synthetic customer, asks for an offer, and taps Claim / Not now / Why. Keeping the
logic here (instead of inside Telegram handlers) means it is fully testable and
the bot file stays a thin layer.
"""
from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from src import db
from src.engine.nodes import OFFER_STATUSES, in_holdout
from src.engine.pipeline import run_pipeline
from src import scheduler
from src.engine.state import Deps
from src.features import compute_features, customer_features
from src.formatting import indian_number, plain_number
from src.guardrails import CHECK_LABELS
from src.segments import assign_segment, segment_all

SEGMENT_LABELS = {
    "lapsing": "😴 Lapsing regular",
    "occasional": "🛒 Cart abandoner / occasional buyer",
    "deal_hunter": "🏷️ Deal hunter",
    "new": "🌱 New customer",
    "vip": "💎 VIP big spender",
    "loyal": "❤️ Loyal regular",
    "hibernating": "💤 Hibernating (gone quiet)",
}
LANGUAGE_LABELS = {"english": "English", "hinglish": "Hinglish", "punjabi": "Punjabi"}


@dataclass
class ButtonResult:
    toast: str                      # short pop-up on the button
    remove_buttons: bool = False
    append_to_card: str | None = None
    reply: str | None = None        # separate message


def _e(text) -> str:
    return html.escape(str(text), quote=False)


class BotService:
    def __init__(self, conn: sqlite3.Connection, deps: Deps, now: Callable[[], datetime] = datetime.now,
                 admin_ids: set[int] | None = None):
        self.conn = conn
        self.deps = deps
        self.now = now
        self.admin_ids = set(admin_ids or ())
        self.offsets: dict[int, int] = {}  # /timetravel: days each chat has jumped ahead (demo only)
        # Segment of every customer, computed once at start-up (used to offer persona choices).
        feats = compute_features(conn, now().date())
        self.segments: dict[str, str] = segment_all(feats, deps.segment_config)["segment"].to_dict()

    # ------------------------------------------------------------ linking
    def linked_customer(self, chat_id: int) -> str | None:
        row = self.conn.execute("SELECT customer_id FROM customers WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
        return row["customer_id"] if row else None

    def _candidates(self, segment: str | None) -> list[str]:
        rows = self.conn.execute(
            "SELECT customer_id FROM customers WHERE telegram_chat_id IS NULL AND marketing_opt_in = 1 "
            "ORDER BY customer_id").fetchall()
        holdout = self.deps.rules.global_.holdout_pct
        return [r["customer_id"] for r in rows
                if (segment is None or self.segments.get(r["customer_id"]) == segment)
                and not in_holdout(r["customer_id"], holdout)]

    def link(self, chat_id: int, segment: str | None = None) -> str | None:
        """Give this classmate an unused customer (of the chosen type) who would get an offer today.

        Prefers customers whose dry-run decision is "send", so the demo shows a real offer.
        Returns the customer id, or None if nobody of that type is free.
        """
        candidates = self._candidates(segment)
        if not candidates:
            return None
        chosen = candidates[0]
        preview = replace(self.deps, dry_run=True, on_demand=True, _spent_today={})
        for cid in candidates[:60]:
            if run_pipeline(cid, preview, self.now()).status == "sent":
                chosen = cid
                break
        self.conn.execute("UPDATE customers SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (chat_id,))
        self.conn.execute("UPDATE customers SET telegram_chat_id = ? WHERE customer_id = ?", (chat_id, chosen))
        self.conn.commit()
        return chosen

    def set_opt_in(self, chat_id: int, opt_in: bool) -> bool:
        cid = self.linked_customer(chat_id)
        if not cid:
            return False
        self.conn.execute("UPDATE customers SET marketing_opt_in = ? WHERE customer_id = ?", (int(opt_in), cid))
        self.conn.commit()
        return True

    # ------------------------------------------------------------ cards
    def who_card(self, customer_id: str) -> str:
        f = customer_features(self.conn, customer_id, self.now().date())
        seg = self.segments.get(customer_id, "occasional")
        _, reason = assign_segment(f, self.deps.segment_config)
        lines = [
            f"👤 <b>You are now {_e(f['name'])}</b>",
            f"{f['age']} · {_e(f['city'])} · prefers {LANGUAGE_LABELS.get(f['preferred_language'], 'English')}",
            "",
            f"Shopper type: <b>{SEGMENT_LABELS.get(seg, seg)}</b>",
            f"<i>{_e(reason)}</i>",
            "",
            f"❤️ Loves: {_e((f.get('favourite_categories') or '').replace(',', ', '))}",
        ]
        if f.get("days_since_last_order") is not None:
            lines.append(f"🛍 Last order: {int(f['days_since_last_order'])} days ago ({f['n_orders']} orders in total)")
        else:
            lines.append("🛍 No orders yet")
        pts = f"⭐ Points: {f['points_balance']}"
        if f.get("points_expiring"):
            pts += f" ({f['points_expiring']} expiring in {int(f['days_to_points_expiry'])} days)"
        lines.append(pts)
        if f.get("open_cart_items"):
            lines.append(f"🛒 In your cart: {_e(f['open_cart_items'])}")
        lines += ["", "Tap /myoffer to see if we have something for you."]
        return "\n".join(lines)

    # ------------------------------------------------------------ offers
    def my_offer(self, chat_id: int) -> tuple[str, int | None]:
        """Run the full pipeline now. Returns (message HTML, decision id if an offer card was made)."""
        cid = self.linked_customer(chat_id)
        if not cid:
            return "You're not linked to a customer yet. Tap /start first.", None
        self.deps.refresh_config()  # rules saved in the dashboard apply right away
        self.deps._spent_today.clear()  # always read today's real spend from the log
        now = self.now_for(chat_id)
        scheduler.expire_offers(self.conn, now, cid)
        s = run_pipeline(cid, self.deps, now)
        if s.final_offer is not None:
            return s.card_text, s.decision_id
        if s.is_holdout:
            return ("🧪 <b>No offer today: you're in our control group.</b>\n"
                    "We keep 10% of customers offer-free so we can measure what offers really change. "
                    "Try /persona to become someone else."), None
        hint = "\n\nTry /persona to become a different customer."
        return f"🙅 <b>No offer for you right now.</b>\n{_e(s.skip_reason)}{hint}", None

    def _decision(self, decision_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM decision_log WHERE decision_id = ?", (decision_id,)).fetchone()
        return dict(row) if row else None

    def _set_status(self, row: dict, status: str) -> None:
        now = self.now().isoformat(sep=" ", timespec="seconds")
        self.conn.execute("UPDATE decision_log SET status = ? WHERE decision_id = ?", (status, row["decision_id"]))
        self.conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,?)",
                          (row["decision_id"], row["customer_id"], now, status))
        self.conn.commit()

    def handle_button(self, chat_id: int, action: str, decision_id: int) -> ButtonResult:
        """Claim / Not now / Why on an offer card. Safe to tap twice; only the owner can act."""
        row = self._decision(decision_id)
        if row is None or row["customer_id"] != self.linked_customer(chat_id):
            return ButtonResult("This offer isn't yours.")
        if action == "why":
            self.conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,?)",
                              (decision_id, row["customer_id"], self.now().isoformat(sep=" ", timespec="seconds"),
                               "why"))
            self.conn.commit()
            return ButtonResult("Here's why 👇", reply=self.why_text(row))
        if row["status"] not in ("sent", "queued"):
            return ButtonResult(f"This offer was already {row['status']}.", remove_buttons=True)
        if row["expires_on"] and row["expires_on"] < self.now_for(chat_id).date().isoformat():
            self._set_status(row, "expired")
            return ButtonResult("Sorry, this offer has expired.", remove_buttons=True,
                                append_to_card="⌛ <i>Expired</i>")
        if action == "claim":
            self._set_status(row, "claimed")
            return ButtonResult("Claimed! 🎉", remove_buttons=True,
                                append_to_card=f"✅ <b>Claimed!</b> Use code <code>{_e(row['coupon_code'])}</code> at checkout.")
        if action == "dismiss":
            self._set_status(row, "dismissed")
            return ButtonResult("No problem!", remove_buttons=True,
                                append_to_card="👌 <i>Not now. We won't bother you for a while.</i>")
        return ButtonResult("Unknown action.")

    def why_text(self, row: dict) -> str:
        """Plain-words explanation of one decision, straight from the audit log."""
        seg = row["segment"]
        lines = ["🤔 <b>Why this offer?</b>", "",
                 f"• <b>What we noticed:</b> {_e(row['trigger_detail'] or row['trigger'])}",
                 f"• <b>Your shopper type:</b> {SEGMENT_LABELS.get(seg, seg)}: {_e(row['segment_reason'] or '')}"]
        if row["final_offer_id"] == "AI":
            reason = ""
            try:
                reason = json.loads(row["ai_proposal_json"] or "{}").get("reason", "")
            except (json.JSONDecodeError, AttributeError):
                pass
            lines.append(f"• <b>Why this offer:</b> {_e(reason)}" if reason else "• Picked by our offer assistant.")
        else:
            lines.append(f"• {self._fallback_reason(row)}")
        lines.append(f"• <b>Safety:</b> it passed all {len(CHECK_LABELS)} rule checks (discount caps, budget, "
                     "profit margin, message accuracy) before it was sent.")
        return "\n".join(lines)

    def _fallback_reason(self, row: dict) -> str:
        try:
            violations = json.loads(row["violations"] or "[]")
        except json.JSONDecodeError:
            violations = []
        if violations and violations[0].startswith("ai_error"):
            return "Our offer assistant didn't answer in time, so we sent a pre-approved offer instead."
        blocked = [CHECK_LABELS.get(v.split(":", 1)[0], v.split(":", 1)[0]) for v in violations]
        if blocked:
            return ("Our assistant's first idea was blocked by our rules (" + "; ".join(dict.fromkeys(blocked)).lower()
                    + "), so we sent a pre-approved offer instead.")
        return "We sent one of our pre-approved offers."

    # ------------------------------------------------------------ /why and /history
    def _latest(self, customer_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM decision_log WHERE customer_id = ? "
                                "ORDER BY ts DESC, decision_id DESC LIMIT 1", (customer_id,)).fetchone()
        return dict(row) if row else None

    def why_last(self, chat_id: int) -> str:
        """Explain the most recent decision for this classmate, offer or no offer."""
        cid = self.linked_customer(chat_id)
        if not cid:
            return "Tap /start first."
        row = self._latest(cid)
        if row is None:
            return "We haven't made a decision for you yet. Try /myoffer."
        if row["final_offer_id"]:
            return self.why_text(row)
        seg = row["segment"]
        title = "🧪 <b>Why no offer?</b>" if row["is_holdout"] else "🙅 <b>Why no offer?</b>"
        lines = [title, ""]
        if row["trigger"] and row["trigger"] != "none":
            lines.append(f"• <b>What we noticed:</b> {_e(row['trigger_detail'] or row['trigger'])}")
        if seg:
            lines.append(f"• <b>Your shopper type:</b> {SEGMENT_LABELS.get(seg, seg)}: {_e(row['segment_reason'] or '')}")
        lines.append(f"• <b>Decision:</b> {_e(row['skip_reason'] or 'no offer')}")
        return "\n".join(lines)

    @staticmethod
    def _short_offer(row: dict) -> str:
        t, v, cat = row["offer_type"], row["offer_value"] or 0, row["offer_category"] or "everything"
        if t == "percent_off":
            return f"{plain_number(v)}% off {cat}"
        if t == "flat_off":
            return f"₹{indian_number(v)} off {cat}"
        if t == "bonus_points":
            return f"{plain_number(v)} bonus points"
        if t == "free_delivery":
            return "Free delivery"
        return "Early access"

    STATUS_ICONS = {"sent": "📨 waiting", "queued": "🕗 queued", "claimed": "✅ claimed", "dismissed": "👌 not now",
                    "ignored": "💤 ignored", "expired": "⌛ expired"}

    def history(self, chat_id: int, limit: int = 5) -> str:
        cid = self.linked_customer(chat_id)
        if not cid:
            return "Tap /start first."
        rows = self.conn.execute(
            f"""SELECT * FROM decision_log WHERE customer_id = ? AND status IN ({','.join('?' * len(OFFER_STATUSES))})
                ORDER BY ts DESC, decision_id DESC LIMIT ?""", (cid, *OFFER_STATUSES, limit)).fetchall()
        if not rows:
            return "No offers yet. Try /myoffer."
        lines = ["🧾 <b>Your last offers</b>", ""]
        for r in map(dict, rows):
            day = datetime.fromisoformat(r["ts"]).strftime("%d %b")
            src = "AI" if r["final_offer_id"] == "AI" else "pre-approved"
            lines.append(f"• {day}: <b>{_e(self._short_offer(r))}</b> ({src}), "
                         f"{self.STATUS_ICONS.get(r['status'], r['status'])}")
        return "\n".join(lines)

    # ------------------------------------------------------------ admin
    def is_admin(self, chat_id: int) -> bool:
        return chat_id in self.admin_ids

    def set_paused(self, paused: bool) -> None:
        db.set_setting(self.conn, "paused", "1" if paused else "0")

    def stats_text(self) -> str:
        """Today's numbers for the admin: decisions, offers, claims, money, safety."""
        today = self.now().date().isoformat()
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM decision_log WHERE DATE(ts) = ?", (today,))]
        by = {}
        for r in rows:
            by[r["status"]] = by.get(r["status"], 0) + 1
        offers = [r for r in rows if r["status"] in OFFER_STATUSES]
        claimed = by.get("claimed", 0)
        spent = sum(r["expected_cost"] or 0 for r in offers)
        budget = self.deps.rules.global_.daily_budget_inr
        ai_tried = [r for r in rows if r["ai_proposal_json"] is not None or (r["violations"] or "").startswith('["ai_error')]
        ai_passed = sum(1 for r in offers if r["final_offer_id"] == "AI")
        blocked: dict[str, int] = {}
        for r in ai_tried:
            for v in json.loads(r["violations"] or "[]"):
                name = v.split(":", 1)[0]
                blocked[name] = blocked.get(name, 0) + 1
        linked = self.conn.execute("SELECT COUNT(*) FROM customers WHERE telegram_chat_id IS NOT NULL").fetchone()[0]
        paused = db.is_paused(self.conn)
        lines = [f"📊 <b>Today ({self.now():%a %d %b})</b>", "",
                 f"{'⏸ <b>ALL OFFERS PAUSED</b>' if paused else '▶️ Offers running'} · {linked} classmates linked",
                 f"Decisions: {len(rows)} · offers out: {len(offers)} · skipped: {by.get('skipped', 0)} · "
                 f"holdout: {by.get('holdout', 0)} · no offer: {by.get('no_offer', 0)}",
                 f"Claimed: {claimed} ({claimed / len(offers):.0%} of offers)" if offers else "Claimed: 0",
                 f"Budget used: ₹{indian_number(spent)} of ₹{indian_number(budget)}",
                 f"AI offers that passed the rules: {ai_passed} of {len(ai_tried)}"]
        if blocked:
            top = sorted(blocked.items(), key=lambda kv: -kv[1])[:4]
            lines.append("Blocked by: " + ", ".join(f"{CHECK_LABELS.get(k, k).lower()} ({n})" for k, n in top))
        return "\n".join(lines)

    def audit_text(self, customer_id: str, limit: int = 6) -> str:
        """A customer's decision trail, newest first, for the admin."""
        customer_id = customer_id.strip().upper()
        cust = self.conn.execute("SELECT name FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
        if not cust:
            return f"No customer {_e(customer_id)}."
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM decision_log WHERE customer_id = ? ORDER BY ts DESC, decision_id DESC LIMIT ?",
            (customer_id, limit))]
        lines = [f"🔎 <b>Audit: {customer_id} ({_e(cust['name'])})</b>"]
        if not rows:
            return lines[0] + "\nNo decisions yet."
        for r in rows:
            when = datetime.fromisoformat(r["ts"]).strftime("%d %b %H:%M")
            lines += ["", f"<b>#{r['decision_id']} · {when} · {_e(r['status'])}</b>",
                      f"trigger: {_e(r['trigger'])} ({_e(r['trigger_detail'] or '')})",
                      f"segment: {_e(r['segment'])}, churn risk {r['churn_risk']}"]
            if r["final_offer_id"]:
                lines.append(f"offer: {_e(self._short_offer(r))} via {_e(r['final_offer_id'])}, "
                             f"code {_e(r['coupon_code'])}, est. ₹{indian_number(r['expected_cost'] or 0)}")
            if r["skip_reason"]:
                lines.append(f"reason: {_e(r['skip_reason'])}")
            violations = json.loads(r["violations"] or "[]")
            if violations:
                lines.append("AI blocked: " + _e("; ".join(violations))[:300])
        return "\n".join(lines)

    # ------------------------------------------------------------ clock, time travel, scheduler
    def now_for(self, chat_id: int | None = None) -> datetime:
        return self.now() + timedelta(days=self.offsets.get(chat_id, 0))

    def time_travel(self, chat_id: int, days: int) -> str:
        """Demo: jump this chat's clock ahead so expiry and the escalation ladder can be shown live."""
        cid = self.linked_customer(chat_id)
        if not cid:
            return "Tap /start first."
        days = max(0, min(int(days), 60))
        self.offsets[chat_id] = days
        now = self.now_for(chat_id)
        expired = scheduler.expire_offers(self.conn, now, cid)
        lines = [f"🕰 <b>It is now {now:%a %d %b}</b> ({days} day(s) ahead for you)."]
        if expired:
            lines.append(f"{expired} offer(s) expired without a tap, so they count as ignored.")
        lines.append("Try /myoffer to see what the engine does next." if days else "Back to the real date.")
        return "\n".join(lines)

    def tick(self) -> list[tuple[int, str, int]]:
        """Scheduler round for linked classmates. Returns (chat_id, card, decision_id) to push."""
        now = self.now()
        self.deps.refresh_config()
        deps = replace(self.deps, on_demand=False, _spent_today={})
        result = scheduler.tick(deps, now, linked_only=True)
        pushes = []
        for row in result.due_queued:
            scheduler.mark_delivered(self.conn, row["decision_id"], row["customer_id"], now)
            if row.get("card_text") and row.get("telegram_chat_id"):
                pushes.append((row["telegram_chat_id"], row["card_text"], row["decision_id"]))
        for s in result.follow_ups:
            chat = s.features.get("telegram_chat_id")
            if s.status == "sent" and chat:
                pushes.append((int(chat), s.card_text, s.decision_id))
        return pushes

