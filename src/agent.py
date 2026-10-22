"""Offer agent (Node 5).

Two agents with the same interface - (customer summary, allowed options) -> raw JSON text:
- `ClaudeAgent`: Claude picks an offer and writes the message (prompt in prompts/offer_agent.md).
- `stub_agent`: a rule-based stand-in that keeps the system usable with no API key.

Neither agent's output is trusted: the guardrail validator (Node 6) checks every proposal.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

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


# ============================================================ real AI agent (Claude)
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "offer_agent.md"
DEFAULT_MODEL = "claude-sonnet-5-5"
# Models that accept the server-side refusal fallback ("default" routing).
SERVER_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

# JSON shape Claude must return. The API enforces the shape; our validator still checks every value.
OFFER_SCHEMA = {
    "type": "object",
    "properties": {
        "offer_type": {"type": "string",
                       "enum": ["percent_off", "flat_off", "free_delivery", "bonus_points", "early_access"]},
        "value": {"type": "number"},
        "category": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "expiry_days": {"type": "integer"},
        "min_order_inr": {"type": "number"},
        "message": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["offer_type", "value", "category", "expiry_days", "min_order_inr", "message", "reason"],
    "additionalProperties": False,
}


class AgentRefusal(RuntimeError):
    """Claude declined the request; the pipeline falls back to a pre-approved offer."""


def build_user_message(summary: dict, options: list[dict]) -> str:
    """Customer facts go in as JSON inside tags, so they are read as data, never as instructions."""
    return (
        "<customer_data>\n" + json.dumps(summary, ensure_ascii=False, indent=1) + "\n</customer_data>\n\n"
        "<allowed_options>\n" + json.dumps(options, ensure_ascii=False, indent=1) + "\n</allowed_options>\n\n"
        "Write the offer as JSON."
    )


class ClaudeAgent:
    """Node 5 with Claude: same interface as stub_agent (summary, options) -> raw JSON text.

    10 s timeout and 1 retry, as the brief requires; any error, refusal or truncation
    raises, and the pipeline sends a pre-approved fallback instead.
    """

    def __init__(self, model: str | None = None, client=None, timeout: float = 10.0, max_retries: int = 1,
                 effort: str = "low", prompt_path: Path = PROMPT_PATH):
        self.model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.effort = effort
        self.system_prompt = prompt_path.read_text(encoding="utf-8")
        if client is None:
            import anthropic
            import truststore
            truststore.inject_into_ssl()  # trust the Windows certificate store (antivirus / proxy SSL)
            client = anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
        self.client = client
        self.__name__ = f"claude:{self.model}"

    def request(self, summary: dict, options: list[dict]) -> dict:
        """The exact API request (kept separate so tests can inspect it)."""
        req = dict(
            model=self.model,
            max_tokens=2000,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user_message(summary, options)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": OFFER_SCHEMA}},
        )
        if self.model in SERVER_FALLBACK_MODELS:
            # If Claude declines (safety classifier), the API retries on a fallback model in the same call.
            req.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return req

    def __call__(self, summary: dict, options: list[dict]) -> str:
        req = self.request(summary, options)
        if "betas" in req:
            response = self.client.beta.messages.create(**req)
        else:
            response = self.client.messages.create(**req)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AgentRefusal(f"declined ({getattr(details, 'category', None) or 'no category'})")
        if response.stop_reason == "max_tokens":
            raise ValueError("response was cut off before the offer was complete")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise ValueError("no text in the response")
        return text


def has_claude_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def make_agent(kind: str = "auto"):
    """Pick the agent: 'claude', 'stub', or 'auto' (Claude if a key is configured, else the stub).

    Returns (agent, name).
    """
    if kind == "stub" or (kind == "auto" and not has_claude_credentials()):
        return stub_agent, "stub"
    agent = ClaudeAgent()
    return agent, agent.__name__
