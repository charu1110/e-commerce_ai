"""Pipeline runner: sends one customer through the 10 nodes in order.

    python -m src.engine.pipeline --customer C00001          # one customer, show every step
    python -m src.engine.pipeline --sample 200               # many customers, summary
    add --write to log decisions (default is a dry run that writes nothing)
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import date, datetime, time

import numpy as np

from src import db
from src.engine import nodes
from src.engine.state import Deps, State
from src.features import compute_features


def run_pipeline(customer_id: str, deps: Deps, now: datetime | None = None) -> State:
    """One offer decision for one customer. Always ends in exactly one decision_log row (unless dry run)."""
    s = State(customer_id=customer_id, now=now or datetime.now())
    s = nodes.load_customer(s, deps)
    if not s.found:
        return nodes.skip_or_holdout(s, deps)
    s = nodes.detect_trigger(s, deps)
    s = nodes.segment_and_score(s, deps)
    s = nodes.eligibility_check(s, deps)
    if not s.eligible:
        return nodes.skip_or_holdout(s, deps)
    s = nodes.ai_offer_agent(s, deps)
    s = nodes.guardrail_validator(s, deps)
    if s.final_offer is None:
        s = nodes.fallback_offer(s, deps)
    if s.final_offer is not None:
        s = nodes.send_on_telegram(s, deps)
    return nodes.track_and_learn(s, deps)


def priority_order(customer_ids: list[str], features) -> list[str]:
    """Who gets the daily budget first: likely to churn x valuable x NOT going to buy anyway.

    Business reason: the budget runs out, so it should go to win-backs that change the
    most revenue, not to whoever happens to be processed first.
    """
    f = features.reindex(customer_ids)
    value = np.log1p(f["customer_value"].fillna(0) + f["aov"].fillna(0))
    score = f["churn_risk"].fillna(0.5) * (1 - f["buy_anyway"].fillna(0)) * value
    return list(score.sort_values(ascending=False, kind="stable").index)


def run_batch(customer_ids: list[str], deps: Deps, now: datetime | None = None) -> list[State]:
    """Run many customers in budget-priority order, computing features once for all (fast)."""
    now = now or datetime.now()
    if deps.features is None:
        deps.features = compute_features(deps.conn, now.date(), customer_ids)
    return [run_pipeline(cid, deps, now) for cid in priority_order(customer_ids, deps.features)]


# ---------------------------------------------------------------- CLI
def _plain(card: str) -> str:
    return re.sub(r"<[^>]+>", "", card)


def explain(s: State) -> str:
    """Human-readable walk through one decision."""
    lines = [f"Customer {s.customer_id} ({s.features.get('name', '?')})  at {s.now:%Y-%m-%d %H:%M}",
             f"Path: {' -> '.join(s.path)}",
             f"Trigger: {s.trigger} - {s.trigger_detail}",
             f"Segment: {s.segment} - {s.segment_reason}",
             f"Scores: churn risk {s.churn_risk}, buy anyway {s.buy_anyway}, price sensitivity {s.price_sensitivity}"]
    if not s.eligible and s.status in ("skipped", "holdout"):
        lines.append(f"Result: {s.status.upper()} - {s.skip_reason}")
        return "\n".join(lines)
    lines.append(f"Allowed options: {len(s.allowed_options)}")
    if s.ai_error:
        lines.append(f"AI error: {s.ai_error}")
    elif s.validation:
        lines.append(f"Validator: {'PASSED' if s.validation.passed else 'FAILED'}")
        lines += [f"   - {v}" for v in s.validation.violations]
    if s.fallback_used:
        lines.append(f"Fallback: {s.final_offer_id or 'none passed'}")
        lines += [f"   tried {oid}: {'; '.join(v)}" for oid, v in s.fallback_tried]
    if s.final_offer:
        lines += [f"Result: {s.status.upper()} (send at {s.send_at:%a %H:%M}, est. cost Rs {s.final_validation.expected_cost_inr:,.0f})",
                  "-" * 60, _plain(s.card_text), "-" * 60]
    else:
        lines.append(f"Result: NO OFFER - {s.skip_reason}")
    return "\n".join(lines)


def summarise(states: list[State]) -> str:
    n = len(states)
    status = Counter(s.status for s in states)
    out = [f"{n} customers", "Outcome: " + ", ".join(f"{k} {v}" for k, v in status.most_common())]
    out.append("Triggers: " + ", ".join(f"{k} {v}" for k, v in Counter(s.trigger for s in states).most_common()))
    skips = Counter(re.sub(r"\d+", "N", s.skip_reason or "") for s in states if s.status == "skipped")
    out.append("Top skip reasons:")
    out += [f"   {v:>4}  {k}" for k, v in skips.most_common(6)]
    ai = [s for s in states if s.validation is not None or s.ai_error]
    if ai:
        passed = sum(1 for s in ai if s.validation and s.validation.passed)
        out.append(f"AI proposals: {len(ai)}, passed guardrails {passed} ({passed / len(ai):.0%}), "
                   f"fallback used {sum(s.fallback_used for s in states)}")
        blocked = Counter(c for s in ai if s.validation for c in s.validation.failed_checks)
        if blocked:
            out.append("Blocked by: " + ", ".join(f"{k} {v}" for k, v in blocked.most_common()))
    offers = [s for s in states if s.final_offer]
    if offers:
        cost = sum(s.final_validation.expected_cost_inr for s in offers)
        out.append(f"Offers: {len(offers)}, est. cost Rs {cost:,.0f}; by type: " +
                   ", ".join(f"{k} {v}" for k, v in Counter(s.final_offer.offer_type for s in offers).most_common()))
        out.append("By segment: " + ", ".join(f"{k} {v}" for k, v in Counter(s.segment for s in offers).most_common()))
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offer pipeline.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--customer", help="customer id, e.g. C00001")
    group.add_argument("--sample", type=int, help="run N random customers and print a summary")
    parser.add_argument("--today", type=date.fromisoformat, help="pretend today is YYYY-MM-DD")
    parser.add_argument("--hour", type=int, default=None, help="pretend it is this hour (0-23)")
    parser.add_argument("--write", action="store_true", help="log decisions to the database")
    parser.add_argument("--agent", choices=["stub", "claude", "auto"], default="stub",
                        help="who writes offers: stub (default, free), claude (uses the API), auto")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--db", default=str(db.DB_PATH))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # rupee sign on the Windows console

    now = datetime.now()
    if args.today:
        now = datetime.combine(args.today, now.time())
    if args.hour is not None:
        now = datetime.combine(now.date(), time(args.hour, 0))
    conn = db.connect(args.db)
    from dotenv import load_dotenv
    from src.agent import make_agent
    load_dotenv()
    agent, agent_name = make_agent(args.agent)
    deps = Deps.load(conn, agent=agent, agent_name=agent_name, dry_run=not args.write, channel="cli")

    if args.customer:
        print(explain(run_pipeline(args.customer, deps, now)))
        return
    import random
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    sample = random.Random(args.seed).sample(ids, min(args.sample, len(ids)))
    states = run_batch(sample, deps, now)
    print(summarise(states))
    shown = [s for s in states if s.final_offer][:3]
    for s in shown:
        print("\n" + explain(s))


if __name__ == "__main__":
    main()
