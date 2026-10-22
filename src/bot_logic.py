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
from datetime import datetime
from typing import Callable

from src.engine.nodes import in_holdout
from src.engine.pipeline import run_pipeline
from src.engine.state import Deps
from src.features import compute_features, customer_features
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
    def __init__(self, conn: sqlite3.Connection, deps: Deps, now: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.deps = deps
        self.now = now
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
        self.deps._spent_today.clear()  # always read today's real spend from the log
        s = run_pipeline(cid, self.deps, self.now())
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
        if row["expires_on"] and row["expires_on"] < self.now().date().isoformat():
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
            lines.append("• Our assistant's first idea broke one of our rules, so we sent a pre-approved offer instead.")
        lines.append(f"• <b>Safety:</b> it passed all {len(CHECK_LABELS)} rule checks (discount caps, budget, "
                     "profit margin, message accuracy) before it was sent.")
        return "\n".join(lines)
