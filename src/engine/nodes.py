"""The 10 pipeline nodes. Each is one plain function: (State, Deps) -> State.

    1 Load customer -> 2 Detect trigger -> 3 Segment & score -> 4 Eligibility
        not eligible / holdout -> 9 Skip or holdout
        eligible -> 5 AI offer agent -> 6 Guardrail validator
            fails / AI error -> 10 Fallback offer
        -> 7 Send on Telegram -> 8 Track & learn

Who decides: nodes 4, 6, 9 and 10 are RULES (config/*.yaml), node 5 is the AI,
everything else is plain code. Nodes 8 and 9 write the one decision_log row per pass.
"""
from __future__ import annotations

import hashlib
import html
import json
import secrets
from datetime import datetime, timedelta

import pandas as pd

from src import db
from src.features import customer_features
from src.guardrails import DISCOUNT_TYPES, ValidationContext, allowed_options, in_quiet_hours, validate
from src.offers import describe_offer, fallback_offer as pick_fallback
from src.segments import assign_segment
from src.engine.state import Deps, State

# Statuses that mean "an offer actually went (or is going) out".
OFFER_STATUSES = ("sent", "queued", "claimed", "dismissed", "ignored", "expired")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
CODE_PREFIX = {"percent_off": "PCT", "flat_off": "FLAT", "free_delivery": "SHIP",
               "bonus_points": "PTS", "early_access": "EARLY"}


def _clean(v):
    """NaN/NaT -> None so features are plain Python values."""
    if v is None or isinstance(v, (str, list, dict)):
        return v
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def _offer_history(deps: Deps, customer_id: str) -> list[dict]:
    rows = deps.conn.execute(
        f"""SELECT ts, status, offer_type, expires_on FROM decision_log
            WHERE customer_id = ? AND status IN ({','.join('?' * len(OFFER_STATUSES))})
            ORDER BY ts DESC, decision_id DESC""", (customer_id, *OFFER_STATUSES)).fetchall()
    return [dict(r) for r in rows]


def _spent_today(deps: Deps, now: datetime) -> float:
    """Discount money already committed today (cached; Node 8 adds to it as offers go out)."""
    day = now.date()
    if day not in deps._spent_today:
        row = deps.conn.execute(
            f"""SELECT COALESCE(SUM(expected_cost), 0) AS s FROM decision_log
                WHERE DATE(ts) = ? AND status IN ({','.join('?' * len(OFFER_STATUSES))})""",
            (day.isoformat(), *OFFER_STATUSES)).fetchone()
        deps._spent_today[day] = float(row["s"])
    return deps._spent_today[day]


def in_holdout(customer_id: str, holdout_pct: float) -> bool:
    """Stable random split: the same customer is always in (or out of) the control group."""
    bucket = int(hashlib.sha256(f"holdout:{customer_id}".encode()).hexdigest(), 16) % 100
    return bucket < holdout_pct


# ===================================================================== 1
def load_customer(s: State, deps: Deps) -> State:
    """Node 1 (data): profile, orders, browsing, cart and points -> features."""
    s.path.append("1 Load customer")
    if deps.features is not None and s.customer_id in deps.features.index:
        f = {k: _clean(v) for k, v in deps.features.loc[s.customer_id].to_dict().items()}
        f["customer_id"] = s.customer_id
    else:
        f = customer_features(deps.conn, s.customer_id, s.now.date())
    s.found = bool(f)
    s.features = f
    return s


# ===================================================================== 2
def detect_trigger(s: State, deps: Deps) -> State:
    """Node 2 (code): why might this customer want an offer today? First matching trigger wins."""
    s.path.append("2 Detect trigger")
    f, cfg = s.features, deps.segment_config
    seg_cfg, trig = cfg["segments"], cfg.get("triggers", {})
    recurring = deps.calendar.recurring
    n = int(f.get("n_orders") or 0)
    since = f.get("days_since_last_order")
    gap = f.get("typical_gap")
    favourites = [c for c in (f.get("favourite_categories") or "").split(",") if c]

    def check(name: str):
        if name == "abandoned_cart" and (f.get("open_cart_count") or 0) > 0:
            return f"{f['open_cart_count']} item(s) left in cart: {f.get('open_cart_items')}"
        if (name == "lapsed_buyer" and n >= seg_cfg["min_orders_for_habit"] and gap
                and (f.get("gap_ratio") or 0) > seg_cfg["lapsed_gap_ratio"] and since < seg_cfg["hibernating_days"]):
            return f"{since} days since last order (usually every {gap:.0f} days)"
        if (name == "points_expiring" and (f.get("points_expiring") or 0) > 0
                and f.get("days_to_points_expiry") is not None
                and 0 <= f["days_to_points_expiry"] <= trig.get("points_expiring_within_days", 14)):
            return f"{f['points_expiring']} points expire in {f['days_to_points_expiry']:.0f} days"
        if name == "birthday" and f.get("days_to_birthday") is not None:
            if f["days_to_birthday"] <= recurring.get("birthday", {}).get("days_before", 3):
                return f"birthday in {f['days_to_birthday']} days"
        if name == "festival":
            for occ in deps.calendar.active(s.now.date(), f.get("region")):
                liked = set(favourites) & set(occ.boosted_categories)
                if liked:
                    s.occasion_key = occ.key
                    return f"{occ.name} is on and they like {', '.join(sorted(liked))}"
        if name == "new_customer" and (f.get("days_since_signup") or 999) < seg_cfg["new_customer_days"]:
            return f"joined {f['days_since_signup']} days ago"
        if name == "long_inactive" and since is not None and since >= seg_cfg["hibernating_days"]:
            return f"no order for {since} days"
        if (name == "browsing_no_buy" and (f.get("sessions_since_last_order") or 0) >= trig.get("browsing_min_sessions", 3)
                and (f.get("days_since_last_session") or 999) <= 14):
            return f"{f['sessions_since_last_order']} visits since last order without buying"
        if name == "salary_day":
            lo, hi = recurring.get("salary_days", {}).get("days_of_month", [1, 5])
            if lo <= s.now.day <= hi:
                return f"payday window ({lo}-{hi} of the month)"
        return None

    for name in trig.get("order", []):
        detail = check(name)
        if detail:
            s.trigger, s.trigger_detail = name, detail
            return s
    s.trigger, s.trigger_detail = "none", "nothing suggests an offer would help today"
    return s


# ===================================================================== 3
def segment_and_score(s: State, deps: Deps) -> State:
    """Node 3 (code): segment label + scores that drive the offer choice."""
    s.path.append("3 Segment & score")
    f = s.features
    s.segment, s.segment_reason = assign_segment(f, deps.segment_config)
    s.churn_risk = f.get("churn_risk")
    s.buy_anyway = f.get("buy_anyway")
    s.price_sensitivity = f.get("price_sensitivity")
    s.customer_value = f.get("customer_value")
    return s


# ===================================================================== 4
def _build_context(s: State, deps: Deps, history: list[dict], spent: float) -> ValidationContext:
    f = s.features
    today = s.now.date()
    last_offer_days = None
    if history:
        last_offer_days = (today - datetime.fromisoformat(history[0]["ts"]).date()).days
    active = any(h["status"] in ("sent", "queued") and h["expires_on"] and h["expires_on"] >= today.isoformat()
                 for h in history)
    streak = 0
    for h in history:                       # consecutive claimed discounts, most recent first
        if h["status"] == "claimed" and h["offer_type"] in DISCOUNT_TYPES:
            streak += 1
        elif h["status"] in ("claimed", "dismissed", "ignored", "expired"):
            break
    cart_cats = [f["open_cart_category"]] if f.get("open_cart_category") else []
    cart_prices = []
    if s.trigger == "abandoned_cart":
        rows = deps.conn.execute(
            "SELECT price FROM cart_events WHERE customer_id = ? AND status = 'abandoned' ORDER BY added_ts DESC LIMIT 3",
            (s.customer_id,)).fetchall()
        cart_prices = [float(r["price"]) for r in rows]
    basket = f.get("open_cart_value") if s.trigger == "abandoned_cart" and f.get("open_cart_value") else f.get("aov")
    occ = next((o for o in deps.calendar.occasions if o.key == s.occasion_key), None)
    return ValidationContext(
        customer_id=s.customer_id, segment=s.segment, first_name=s.first_name, full_name=f.get("name") or "",
        marketing_opt_in=bool(f.get("marketing_opt_in")), is_holdout=False, paused=False,
        days_since_last_order=f.get("days_since_last_order"), days_since_last_offer=last_offer_days,
        has_active_offer=active, budget_spent_today_inr=spent,
        budget_multiplier=occ.budget_multiplier if occ else 1.0,
        basket_value_inr=basket, category_margins=deps.category_margins,
        favourite_categories=[c for c in (f.get("favourite_categories") or "").split(",") if c],
        cart_categories=cart_cats, cart_prices_inr=cart_prices,
        points_balance=int(f.get("points_balance") or 0), points_expiring=int(f.get("points_expiring") or 0),
        return_rate=f.get("return_rate"), consecutive_discount_claims=streak, other_names=deps.all_names,
    )


def eligibility_check(s: State, deps: Deps) -> State:
    """Node 4 (rules): should this customer get ANY offer right now? First failing rule = skip reason."""
    s.path.append("4 Eligibility check")
    g, e = deps.rules.global_, deps.rules.eligibility
    history = _offer_history(deps, s.customer_id)
    spent = _spent_today(deps, s.now)
    s.ctx = _build_context(s, deps, history, spent)
    ctx = s.ctx

    reasons = [
        (db.is_paused(deps.conn), "All offers are paused (kill switch)."),
        (not ctx.marketing_opt_in, "Customer opted out of offers."),
        (s.trigger in (None, "none"), "No trigger: nothing suggests an offer would change behaviour today."),
        (ctx.days_since_last_offer is not None and ctx.days_since_last_offer < g.cooldown_days,
         f"Cooldown: last offer {ctx.days_since_last_offer} days ago (wait {g.cooldown_days} days)."),
        (ctx.has_active_offer and not g.allow_stacking, "Already holds a live offer (no stacking)."),
        (ctx.days_since_last_order is not None and ctx.days_since_last_order < g.skip_if_bought_within_days,
         f"Just bought {int(ctx.days_since_last_order or 0)} days ago."),
        ((s.buy_anyway or 0) > e.skip_if_buy_anyway_above,
         f"Would buy anyway ({(s.buy_anyway or 0):.0%} chance this week) - a discount would be wasted."),
        (spent >= g.daily_budget_inr * ctx.budget_multiplier, "Today's offer budget is used up."),
    ]
    for failed, reason in reasons:
        if failed:
            s.eligible, s.skip_reason = False, reason
            return s

    s.allowed_options = allowed_options(ctx, deps.rules)
    if not s.allowed_options:
        s.eligible, s.skip_reason = False, f"No offer type is allowed for this {s.segment} customer right now."
        return s
    if in_holdout(s.customer_id, g.holdout_pct):
        s.eligible, s.is_holdout = False, True
        s.skip_reason = "Holdout group: no offer, so we can measure what offers really change."
        return s
    s.eligible = True
    return s


# ===================================================================== 5
def ai_offer_agent(s: State, deps: Deps) -> State:
    """Node 5 (AI): pick one allowed offer and write the message. Its output is NOT trusted yet."""
    s.path.append("5 AI offer agent")
    f = s.features
    occ = next((o for o in deps.calendar.occasions if o.key == s.occasion_key), None)
    s.ai_input = {
        "first_name": s.first_name, "language": f.get("preferred_language"), "segment": s.segment,
        "trigger": s.trigger, "trigger_detail": s.trigger_detail,
        "favourite_categories": f.get("favourite_categories"),
        "days_since_last_order": f.get("days_since_last_order"),
        "usual_gap_days": round(f["typical_gap"]) if f.get("typical_gap") else None,
        "points_balance": f.get("points_balance"), "points_expiring": f.get("points_expiring"),
        "cart_items": f.get("open_cart_items"), "price_sensitivity": s.price_sensitivity,
        "occasion_name": occ.name if occ else None, "occasion_theme": occ.theme if occ else None,
        "occasion_emoji": occ.emoji if occ else None,
    }
    s.ai_source = deps.agent_name
    try:
        s.ai_raw = deps.agent(s.ai_input, s.allowed_options)
    except Exception as exc:  # timeout, API error, bad output: all go to the fallback
        s.ai_error = f"{type(exc).__name__}: {exc}"
    return s


# ===================================================================== 6
def guardrail_validator(s: State, deps: Deps) -> State:
    """Node 6 (rules): deterministic checks on the AI's proposal. The AI never judges itself."""
    s.path.append("6 Guardrail validator")
    if s.ai_error or s.ai_raw is None:
        return s
    s.validation = validate(s.ai_raw, s.ctx, deps.rules)
    if s.validation.passed:
        s.final_offer, s.final_offer_id = s.validation.proposal, "AI"
        s.final_validation = s.validation
        s.headline = deps.catalogue.headline_for(s.trigger, s.first_name)
    return s


# ===================================================================== 10
def fallback_offer(s: State, deps: Deps) -> State:
    """Node 10 (rules): safest pre-approved offer for the segment that passes every rule, else nothing."""
    s.path.append("10 Fallback offer")
    s.fallback_used = True
    fb = pick_fallback(s.ctx, deps.rules, deps.catalogue)
    s.fallback_tried = fb.tried
    if fb.found:
        s.final_offer, s.final_offer_id, s.final_validation = fb.proposal, fb.offer.id, fb.result
        s.headline = fb.offer.headline.format(first_name=s.first_name or "there")
    else:
        s.status = "no_offer"
        blocked = [v for _, vs in fb.tried for v in vs]
        if blocked and all(any(v.startswith("daily_budget") for v in vs) for _, vs in fb.tried):
            left = deps.rules.global_.daily_budget_inr * s.ctx.budget_multiplier - s.ctx.budget_spent_today_inr
            s.skip_reason = f"Today's offer budget is used up (Rs {max(left, 0):,.0f} left), so nothing was sent."
        else:
            s.skip_reason = "No AI or fallback offer passed every rule, so nothing was sent."
    return s


# ===================================================================== 7
def _send_time(s: State, deps: Deps) -> datetime:
    """Respect quiet hours: wait for the customer's usual shopping hour (or the end of quiet hours)."""
    if deps.on_demand or not in_quiet_hours(s.now.hour, deps.rules):
        return s.now
    active = int(s.features.get("active_hour") or 19)
    hour = active if not in_quiet_hours(active, deps.rules) else deps.rules.global_.quiet_hours[1]
    at = s.now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at > s.now else at + timedelta(days=1)


def _coupon_code(s: State, deps: Deps) -> str:
    p = s.final_offer
    value = f"{p.value:g}" if p.value and p.offer_type in ("percent_off", "flat_off") else ""
    while True:
        tail = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        code = f"{CODE_PREFIX[p.offer_type]}{value}-{s.customer_id[1:]}-{tail}"
        if not deps.conn.execute("SELECT 1 FROM decision_log WHERE coupon_code = ?", (code,)).fetchone():
            return code


def send_on_telegram(s: State, deps: Deps) -> State:
    """Node 7 (code): build the offer card, a single-use code and the send time.

    The bot delivers the card (with Claim / Not now / Why buttons) after Node 8 has
    logged it, because the buttons need the decision id.
    """
    s.path.append("7 Send on Telegram")
    p = s.final_offer
    s.offer_summary = describe_offer(p, deps.rules)
    s.coupon_code = _coupon_code(s, deps)
    s.expires_on = (s.now + timedelta(days=p.expiry_days)).date()
    s.send_at = _send_time(s, deps)
    s.status = "queued" if s.send_at > s.now else "sent"
    s.card_text = (
        f"<b>{html.escape(s.headline, quote=False)}</b>\n\n"
        f"{html.escape(p.message, quote=False)}\n\n"
        f"🎁 <b>{html.escape(s.offer_summary, quote=False)}</b>\n"
        f"⏳ Valid till {s.expires_on.strftime('%a %d %b')}\n"
        f"🔖 Code: <code>{s.coupon_code}</code>"
    )
    return s


# ===================================================================== 8 / 9 logging
def _log_decision(s: State, deps: Deps) -> None:
    """Write the one decision_log row for this pass (skipped in dry-run mode)."""
    if deps.dry_run:
        return
    v = s.final_validation or s.validation
    violations = s.validation.violations if s.validation else []
    if s.ai_error:
        violations = [f"ai_error: {s.ai_error}"]
    p = s.final_offer
    cur = deps.conn.execute(
        """INSERT INTO decision_log (customer_id, ts, trigger, segment, churn_risk, eligible, skip_reason,
               ai_proposal_json, validator_result, violations, fallback_used, final_offer_id, message_text,
               coupon_code, expected_cost, status, is_holdout, trigger_detail, segment_reason, ai_source,
               offer_type, offer_value, offer_category, headline, expires_on, send_at, channel)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.customer_id, s.now.isoformat(sep=" ", timespec="seconds"), s.trigger, s.segment, s.churn_risk,
         int(s.eligible), s.skip_reason, s.ai_raw if s.ai_raw is not None else None,
         v.to_json() if v else None, json.dumps(violations, ensure_ascii=False), int(s.fallback_used),
         s.final_offer_id, p.message if p else None, s.coupon_code,
         round(v.expected_cost_inr, 2) if (v and p) else 0.0, s.status, int(s.is_holdout), s.trigger_detail,
         s.segment_reason, s.ai_source, p.offer_type if p else None, p.value if p else None,
         p.category if p else None, s.headline or None, s.expires_on.isoformat() if s.expires_on else None,
         s.send_at.isoformat(sep=" ", timespec="seconds") if s.send_at else None, deps.channel))
    s.decision_id = cur.lastrowid
    if s.status in ("sent", "queued"):
        deps.conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,?)",
                          (s.decision_id, s.customer_id, s.now.isoformat(sep=" ", timespec="seconds"), s.status))
    deps.conn.commit()


# ===================================================================== 8
def track_and_learn(s: State, deps: Deps) -> State:
    """Node 8 (code): log the decision and count its cost against today's budget.

    (Escalation when an offer is ignored and claim-rate learning come in Steps 10-11.)
    """
    s.path.append("8 Track & learn")
    if s.status in ("sent", "queued") and s.final_validation:
        day = s.now.date()
        deps._spent_today[day] = _spent_today(deps, s.now) + s.final_validation.expected_cost_inr
    _log_decision(s, deps)
    return s


# ===================================================================== 9
def skip_or_holdout(s: State, deps: Deps) -> State:
    """Node 9 (rules): no offer. Log why; holdout customers are the control group for uplift."""
    s.path.append("9 Skip or holdout")
    if not s.found:
        s.skip_reason = s.skip_reason or "Unknown customer."
    s.status = "holdout" if s.is_holdout else "skipped"
    if s.found:
        _log_decision(s, deps)
    return s

