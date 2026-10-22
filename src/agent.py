"""Offer agent (Node 5).

`stub_agent` is a rule-based stand-in with the SAME interface as the real Claude agent
(Step 7): it receives a compact customer summary plus the menu of allowed options and
returns raw JSON text. It also keeps the system usable with no API key.

Like the real agent, its output is never trusted: the guardrail validator checks it.
"""
from __future__ import annotations

import json
import math

from src.formatting import indian_number, plain_number
from src.offers import category_label

# Which offer types suit each trigger, best first.
PREFERENCES = {
    "abandoned_cart": ["free_delivery", "percent_off", "bonus_points", "flat_off"],
    "lapsed_buyer": ["percent_off", "flat_off", "free_delivery", "bonus_points"],
    "long_inactive": ["percent_off", "flat_off"],
    "points_expiring": ["bonus_points", "free_delivery", "percent_off"],
    "birthday": ["bonus_points", "early_access", "free_delivery", "percent_off"],
    "festival": ["percent_off", "early_access", "bonus_points", "free_delivery"],
    "new_customer": ["percent_off", "free_delivery"],
    "browsing_no_buy": ["free_delivery", "percent_off", "bonus_points"],
    "salary_day": ["free_delivery", "bonus_points", "percent_off"],
}
EXPIRY = {"abandoned_cart": 3, "lapsed_buyer": 5, "long_inactive": 7, "points_expiring": 3, "birthday": 5,
          "festival": 4, "new_customer": 7, "browsing_no_buy": 3, "salary_day": 3}
GREETING = {"english": "Hi {n}", "hinglish": "Hi {n}", "punjabi": "Sat Sri Akal {n}"}


def _hook(s: dict) -> str:
    """The personal sentence: why we're writing to them today."""
    t = s.get("trigger")
    hinglish = s.get("language") == "hinglish"
    if t == "abandoned_cart" and s.get("cart_items"):
        item = s["cart_items"].split(";")[0].strip()
        return f"your {item} is still waiting in your cart" + (", bas ek click door!" if hinglish else ".")
    if t == "lapsed_buyer":
        return "aapki bahut yaad aayi!" if hinglish else "we've missed you!"
    if t == "long_inactive":
        return "it's been a while since we saw you."
    if t == "points_expiring":
        return f"{s.get('points_expiring')} of your points expire soon."
    if t == "birthday":
        return "happy birthday in advance! 🎂"
    if t == "festival":
        return f"Happy {s.get('occasion_name') or 'festive season'}! {s.get('occasion_emoji') or ''}".strip()
    if t == "new_customer":
        return "welcome to the family!"
    if t == "browsing_no_buy":
        return "spotted something you like?"
    if t == "salary_day":
        return "it's payday, treat yourself!"
    return "we picked something for you."


def _offer_sentence(o: dict) -> str:
    cat = category_label(o["category"])
    above = f"on orders above ₹{indian_number(o['min_order_inr'])}"
    v = o["value"]
    if o["offer_type"] == "percent_off":
        return f"Here's {plain_number(v)}% off {cat} {above}."
    if o["offer_type"] == "flat_off":
        return f"Here's ₹{indian_number(v)} off {cat} {above}."
    if o["offer_type"] == "free_delivery":
        return f"Your next order {above} ships with free delivery."
    if o["offer_type"] == "bonus_points":
        return f"Enjoy {plain_number(v)} bonus points {above}."
    return "You get early access to our next big sale."


def _pick(summary: dict, options: list[dict]) -> dict | None:
    prefs = list(PREFERENCES.get(summary.get("trigger"), ["free_delivery", "bonus_points", "percent_off"]))
    if (summary.get("price_sensitivity") or 0) >= 0.6 and "percent_off" in prefs:
        prefs.remove("percent_off")
        prefs.insert(0, "percent_off")   # price-sensitive customers respond to money off
    for t in prefs:
        matches = [o for o in options if o["offer_type"] == t]
        if not matches:
            continue
        if summary.get("trigger") == "abandoned_cart":
            return matches[0]            # options list the cart category first
        return max(matches, key=lambda o: o["max_value"])  # favourite where a real discount is allowed
    return options[0] if options else None


def build_offer(summary: dict, options: list[dict]) -> dict | None:
    opt = _pick(summary, options)
    if opt is None:
        return None
    t, trigger = opt["offer_type"], summary.get("trigger")
    min_order = 499 if trigger == "new_customer" else 999
    if t == "free_delivery":
        min_order = 799
    value = 0.0
    if t == "percent_off":
        wanted = 15 if trigger in ("lapsed_buyer", "long_inactive") else 10
        value = float(min(wanted, opt["max_value"]))
    elif t == "flat_off":
        value = float(min(opt["max_value"], math.floor(opt["max_pct_of_min_order"] / 100 * min_order / 50) * 50))
    elif t == "bonus_points":
        value = float(min(200, opt["max_value"]))
    expiry = min(max(EXPIRY.get(trigger, 5), opt["expiry_days_min"]), opt["expiry_days_max"])
    offer = {"offer_type": t, "value": value, "category": opt["category"], "expiry_days": expiry,
             "min_order_inr": float(min_order)}
    greeting = GREETING.get(summary.get("language"), "Hi {n}").format(n=summary.get("first_name") or "there")
    offer["message"] = f"{greeting}, {_hook(summary)} {_offer_sentence(offer)} Valid for {expiry} days."
    offer["reason"] = (f"Trigger: {trigger} ({summary.get('trigger_detail')}). Segment: {summary.get('segment')}. "
                       f"Chose {t} as the best allowed fit for this trigger.")
    return offer


def stub_agent(summary: dict, options: list[dict]) -> str:
    """Rule-based agent: same input/output as the real AI agent (raw JSON text)."""
    offer = build_offer(summary, options)
    if offer is None:
        raise ValueError("no allowed options to choose from")
    return json.dumps(offer, ensure_ascii=False)
