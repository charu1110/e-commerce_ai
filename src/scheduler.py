"""Scheduler: the jobs that run on a clock, not on a customer's request.

Business reason: offers that expire without a tap must count as "ignored" (so the
escalation ladder can follow up with a better offer), offers created during quiet
hours must go out when the customer's shopping hour comes, and follow-ups must run
even if nobody types anything. The bot runs `tick()` every 10 minutes.

    python -m src.scheduler            # dry run: shows what would happen now
    python -m src.scheduler --write    # do it (expire offers, run follow-ups)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime

from src import db
from src.engine.pipeline import run_pipeline
from src.engine.state import Deps, State


@dataclass
class TickResult:
    expired: int = 0
    due_queued: list[dict] = field(default_factory=list)   # decision rows whose send time has come
    follow_ups: list[State] = field(default_factory=list)   # pipeline runs for customers who ignored an offer


def _ts(now: datetime) -> str:
    return now.isoformat(sep=" ", timespec="seconds")


def expire_offers(conn, now: datetime, customer_id: str | None = None) -> int:
    """Unclaimed offers past their expiry date become 'ignored' (one offer_events row each)."""
    sql = "SELECT decision_id, customer_id FROM decision_log WHERE status IN ('sent', 'queued') AND expires_on < ?"
    params: list = [now.date().isoformat()]
    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        conn.execute("UPDATE decision_log SET status = 'ignored' WHERE decision_id = ?", (r["decision_id"],))
        conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,'expired')",
                     (r["decision_id"], r["customer_id"], _ts(now)))
    conn.commit()
    return len(rows)


def due_queued(conn, now: datetime, linked_only: bool = False) -> list[dict]:
    """Offers held back by quiet hours whose send time has now come."""
    sql = ("SELECT d.*, c.telegram_chat_id FROM decision_log d JOIN customers c USING (customer_id) "
           "WHERE d.status = 'queued' AND d.send_at <= ?")
    if linked_only:
        sql += " AND c.telegram_chat_id IS NOT NULL"
    return [dict(r) for r in conn.execute(sql, (_ts(now),))]


def mark_delivered(conn, decision_id: int, customer_id: str, now: datetime) -> None:
    conn.execute("UPDATE decision_log SET status = 'sent' WHERE decision_id = ? AND status = 'queued'", (decision_id,))
    conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,'sent')",
                 (decision_id, customer_id, _ts(now)))
    conn.commit()


def follow_up_candidates(conn, linked_only: bool = False) -> list[str]:
    """Customers whose most recent offer was ignored (the pipeline decides if the ladder allows a follow-up)."""
    sql = """
        SELECT d.customer_id FROM decision_log d JOIN customers c USING (customer_id)
        WHERE d.decision_id = (SELECT d2.decision_id FROM decision_log d2
                               WHERE d2.customer_id = d.customer_id
                                 AND d2.status IN ('sent','queued','claimed','dismissed','ignored','expired')
                               ORDER BY d2.ts DESC, d2.decision_id DESC LIMIT 1)
          AND d.status IN ('ignored', 'expired')"""
    if linked_only:
        sql += " AND c.telegram_chat_id IS NOT NULL"
    return [r["customer_id"] for r in conn.execute(sql)]


def tick(deps: Deps, now: datetime | None = None, linked_only: bool = False) -> TickResult:
    """One scheduler round: expire, find due queued offers, run escalation follow-ups."""
    now = now or datetime.now()
    result = TickResult()
    if not deps.dry_run:
        result.expired = expire_offers(deps.conn, now)
    result.due_queued = due_queued(deps.conn, now, linked_only)
    for cid in follow_up_candidates(deps.conn, linked_only):
        # Preview first, so customers still waiting don't get a "skipped" log row every 10 minutes.
        preview = run_pipeline(cid, replace(deps, dry_run=True, _spent_today=dict(deps._spent_today)), now)
        if preview.final_offer is None:
            continue
        result.follow_ups.append(preview if deps.dry_run else run_pipeline(cid, deps, now))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one scheduler round.")
    parser.add_argument("--write", action="store_true", help="really expire offers and send follow-ups")
    parser.add_argument("--db", default=str(db.DB_PATH))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = db.connect(args.db)
    deps = Deps.load(conn, dry_run=not args.write, channel="scheduler")
    r = tick(deps)
    print(f"Expired (now ignored): {r.expired}{'' if args.write else ' (dry run: not written)'}")
    print(f"Queued offers due to send: {len(r.due_queued)}")
    print(f"Follow-up decisions: {len(r.follow_ups)}")
    for s in r.follow_ups[:10]:
        what = f"step {s.ladder_step}: {s.offer_summary}" if s.final_offer else s.skip_reason
        print(f"  {s.customer_id}: {what}")


if __name__ == "__main__":
    main()
