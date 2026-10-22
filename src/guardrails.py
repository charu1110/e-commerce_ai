"""Guardrail validator (Node 6) — pure deterministic Python, no AI.

Business reason: the AI may choose and word offers, but it must never be the judge
of its own output. Every proposal (from the AI *and* from the fallback catalogue)
passes through `validate()` before anything is sent. Each check returns pass/fail
plus a plain-English reason, so the audit log and the dashboard can show exactly
which rule blocked what.

All limits come from config/rules.yaml (marketing-editable).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"

OFFER_TYPES = ("percent_off", "flat_off", "free_delivery", "bonus_points", "early_access")
DISCOUNT_TYPES = {"percent_off", "flat_off"}
OfferType = Literal[OFFER_TYPES]

# Plain-English names for each check (dashboard "blocked by rule" chart).
CHECK_LABELS = {
    "schema": "AI output is not a valid offer",
    "offer_type_allowed": "Offer type not allowed for this segment",
    "discount_caps": "Discount above a cap (global / category / segment)",
    "flat_cap_min_order": "Flat amount or minimum order out of range",
    "daily_budget": "Daily budget would be exceeded",
    "customer_eligibility": "Customer should not get this offer now",
    "expiry_stacking": "Expiry out of range or offers stacked",
    "profit_margin": "Order would lose money after the discount",
    "message_length": "Message too long",
    "message_banned_phrases": "Message uses a banned phrase",
    "message_numbers_match": "Numbers in message don't match the offer",
    "message_privacy": "Message contains someone else's data",
    "message_claims": "Message makes a claim we can't back up",
    "message_style": "Message style (emoji, links, name)",
}


# ============================================================ rules model
class _Strict(BaseModel):
    """Unknown keys are errors, so a typo in rules.yaml is caught instead of silently ignored."""
    model_config = ConfigDict(extra="forbid")


class GlobalRules(_Strict):
    max_discount_pct: float = Field(ge=0, le=100)
    max_flat_discount_inr: float = Field(ge=0)
    min_order_value_inr: float = Field(ge=0)
    daily_budget_inr: float = Field(ge=0)
    cooldown_days: int = Field(ge=0)
    skip_if_bought_within_days: int = Field(ge=0)
    holdout_pct: float = Field(ge=0, le=100)
    quiet_hours: tuple[int, int]
    offer_validity_days: tuple[int, int]
    allow_stacking: bool
    max_bonus_points: int = Field(ge=0)
    min_profit_after_discount_inr: float

    @model_validator(mode="after")
    def _ranges(self):
        if not all(0 <= h <= 23 for h in self.quiet_hours):
            raise ValueError("quiet_hours must be hours between 0 and 23")
        lo, hi = self.offer_validity_days
        if not 1 <= lo <= hi:
            raise ValueError("offer_validity_days must be [min, max] with 1 <= min <= max")
        return self


class CostRules(_Strict):
    delivery_fee_inr: float = Field(ge=0)
    point_value_inr: float = Field(ge=0)
    early_access_cost_inr: float = Field(ge=0)


class SegmentRule(_Strict):
    max_discount_pct: float = Field(ge=0, le=100)
    allowed_types: list[OfferType]


class LadderStep(_Strict):
    step: int
    type: Literal[OFFER_TYPES + ("stop",)]
    value: float | None = None
    wait_days: int | None = None


class EligibilityRules(_Strict):
    skip_if_buy_anyway_above: float = Field(ge=0, le=1)
    discount_addiction_claims: int = Field(ge=1)
    return_abuse_rate: float = Field(ge=0, le=1)
    return_abuse_categories: list[str]


class MessageRules(_Strict):
    max_length_chars: int = Field(ge=20)
    max_emojis: int = Field(ge=0)
    banned_phrases: list[str]
    banned_claims: list[str]


class Rules(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    global_: GlobalRules = Field(alias="global")
    costs: CostRules
    category_max_discount_pct: dict[str, float]
    segment_rules: dict[str, SegmentRule]
    eligibility: EligibilityRules
    escalation_ladder: list[LadderStep]
    message: MessageRules


def parse_rules(data: dict | str) -> Rules:
    """Parse rules from a dict or YAML text. Raises pydantic.ValidationError with readable errors."""
    if isinstance(data, str):
        data = yaml.safe_load(data)
    return Rules.model_validate(data)


def load_rules(path: Path | str = RULES_PATH) -> Rules:
    with open(path, encoding="utf-8") as f:
        return parse_rules(f.read())


def in_quiet_hours(hour: int, rules: Rules) -> bool:
    """True if `hour` is inside quiet hours (handles windows that wrap midnight, e.g. 22 -> 8)."""
    start, end = rules.global_.quiet_hours
    if start == end:
        return False
    return start <= hour or hour < end if start > end else start <= hour < end


def discount_limit_pct(rules: Rules, segment: str, category: str | None) -> float:
    """Strictest of: global cap, category cap (store-wide = strictest category), segment cap."""
    caps = rules.category_max_discount_pct
    cat_cap = caps[category] if category in caps else min(caps.values(), default=0.0)
    seg = rules.segment_rules.get(segment)
    seg_cap = seg.max_discount_pct if seg else 0.0
    return min(rules.global_.max_discount_pct, cat_cap, seg_cap)


# ============================================================ offer + context
class OfferProposal(BaseModel):
    """The ONLY shape an offer may take. Strict: no extra fields, no "10" for 10."""
    model_config = ConfigDict(extra="forbid", strict=True)
    offer_type: OfferType
    value: float = Field(ge=0)          # % for percent_off, Rs for flat_off, points for bonus_points
    category: str | None = None         # None = store-wide
    expiry_days: int
    min_order_inr: float | None = None  # None = global minimum
    message: str = Field(min_length=1)
    reason: str = Field(min_length=1)


@dataclass
class ValidationContext:
    """Facts about the customer and today, gathered by the pipeline (Nodes 1-4)."""
    customer_id: str
    segment: str
    first_name: str = ""
    full_name: str = ""
    marketing_opt_in: bool = True
    is_holdout: bool = False
    paused: bool = False                        # kill switch
    days_since_last_order: int | None = None
    days_since_last_offer: int | None = None
    has_active_offer: bool = False
    budget_spent_today_inr: float = 0.0
    budget_multiplier: float = 1.0              # festival boost from occasions.yaml
    basket_value_inr: float | None = None       # expected order value (AOV or open cart)
    category_margins: dict[str, float] = field(default_factory=dict)
    favourite_categories: list[str] = field(default_factory=list)
    cart_categories: list[str] = field(default_factory=list)
    cart_prices_inr: list[float] = field(default_factory=list)
    points_balance: int = 0
    points_expiring: int = 0
    return_rate: float | None = None
    consecutive_discount_claims: int = 0
    other_names: frozenset[str] = frozenset()   # full names of OTHER customers (privacy check)


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class ValidationResult:
    passed: bool
    checks: list[CheckResult]
    proposal: OfferProposal | None = None
    expected_cost_inr: float = 0.0

    @property
    def violations(self) -> list[str]:
        return [f"{c.name}: {c.reason}" for c in self.checks if not c.passed]

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def to_json(self) -> str:
        return json.dumps({"passed": self.passed, "expected_cost_inr": round(self.expected_cost_inr, 2),
                           "checks": [c.__dict__ for c in self.checks]}, ensure_ascii=False)


# ============================================================ money helpers
def _min_order(p: OfferProposal, rules: Rules) -> float:
    return p.min_order_inr if p.min_order_inr is not None else rules.global_.min_order_value_inr


def basket_value(p: OfferProposal, ctx: ValidationContext, rules: Rules) -> float:
    """Order value we expect if the offer is used: their usual basket, never below the minimum order."""
    return max(ctx.basket_value_inr or 0.0, _min_order(p, rules))


def expected_cost(p: OfferProposal, ctx: ValidationContext, rules: Rules) -> float:
    """What the offer costs us if redeemed (conservative: assume it is redeemed)."""
    c = rules.costs
    if p.offer_type == "percent_off":
        return min(basket_value(p, ctx, rules) * p.value / 100, rules.global_.max_flat_discount_inr)
    if p.offer_type == "flat_off":
        return p.value
    if p.offer_type == "free_delivery":
        return c.delivery_fee_inr
    if p.offer_type == "bonus_points":
        return p.value * c.point_value_inr
    return c.early_access_cost_inr


def _margin_pct(category: str | None, ctx: ValidationContext) -> float:
    m = ctx.category_margins
    if category in m:
        return m[category]
    return min(m.values()) if m else 0.10  # unknown / store-wide: assume the thinnest margin


# ============================================================ checks
def _ok(name: str) -> CheckResult:
    return CheckResult(name, True)


def _fail(name: str, *reasons: str) -> CheckResult:
    return CheckResult(name, False, "; ".join(reasons))


def check_offer_type(p, ctx, rules) -> CheckResult:
    seg = rules.segment_rules.get(ctx.segment)
    if seg is None:
        return _fail("offer_type_allowed", f"Segment '{ctx.segment}' has no rules, so no offer is allowed.")
    if p.offer_type not in seg.allowed_types:
        return _fail("offer_type_allowed",
                     f"{p.offer_type} is not allowed for {ctx.segment} (allowed: {', '.join(seg.allowed_types)}).")
    return _ok("offer_type_allowed")


def check_discount_caps(p, ctx, rules) -> CheckResult:
    caps = rules.category_max_discount_pct
    if p.category is not None and p.category not in caps:
        return _fail("discount_caps", f"Unknown category '{p.category}'.")
    if p.offer_type not in DISCOUNT_TYPES:
        return _ok("discount_caps")
    if p.value <= 0:
        return _fail("discount_caps", "A money-off offer must have a value above 0.")
    pct = p.value if p.offer_type == "percent_off" else p.value / _min_order(p, rules) * 100
    g = rules.global_.max_discount_pct
    cat_cap = caps[p.category] if p.category else min(caps.values())
    seg = rules.segment_rules.get(ctx.segment)
    seg_cap = seg.max_discount_pct if seg else 0.0
    what = f"{pct:g}%" if p.offer_type == "percent_off" else f"Rs {p.value:g} on a Rs {_min_order(p, rules):g} order (= {pct:.0f}%)"
    reasons = []
    if pct > g:
        reasons.append(f"{what} is above the global max of {g:g}%")
    if pct > cat_cap:
        where = p.category or "store-wide (strictest category)"
        reasons.append(f"{what} is above the {where} max of {cat_cap:g}%")
    if pct > seg_cap:
        reasons.append(f"{what} is above the {ctx.segment} max of {seg_cap:g}%")
    return _fail("discount_caps", *reasons) if reasons else _ok("discount_caps")


def check_flat_and_min_order(p, ctx, rules) -> CheckResult:
    g = rules.global_
    reasons = []
    if p.offer_type == "flat_off" and p.value > g.max_flat_discount_inr:
        reasons.append(f"Rs {p.value:g} off is above the Rs {g.max_flat_discount_inr:g} cap")
    if _min_order(p, rules) < g.min_order_value_inr:
        reasons.append(f"minimum order Rs {_min_order(p, rules):g} is below the Rs {g.min_order_value_inr:g} floor")
    if p.offer_type == "bonus_points":
        if p.value <= 0:
            reasons.append("bonus points must be above 0")
        if p.value > g.max_bonus_points:
            reasons.append(f"{p.value:g} bonus points is above the {g.max_bonus_points} cap")
    return _fail("flat_cap_min_order", *reasons) if reasons else _ok("flat_cap_min_order")


def check_budget(p, ctx, rules, cost: float) -> CheckResult:
    limit = rules.global_.daily_budget_inr * ctx.budget_multiplier
    if ctx.budget_spent_today_inr + cost > limit:
        return _fail("daily_budget", f"Rs {ctx.budget_spent_today_inr:,.0f} spent today + Rs {cost:,.0f} "
                                     f"would exceed the Rs {limit:,.0f} daily budget.")
    return _ok("daily_budget")


def check_customer(p, ctx, rules) -> CheckResult:
    g, e = rules.global_, rules.eligibility
    reasons = []
    if ctx.paused:
        reasons.append("all offers are paused (kill switch)")
    if not ctx.marketing_opt_in:
        reasons.append("customer opted out of offers")
    if ctx.is_holdout:
        reasons.append("customer is in the holdout (control) group")
    if ctx.days_since_last_offer is not None and ctx.days_since_last_offer < g.cooldown_days:
        reasons.append(f"last offer was {ctx.days_since_last_offer} days ago (cooldown {g.cooldown_days} days)")
    if ctx.days_since_last_order is not None and ctx.days_since_last_order < g.skip_if_bought_within_days:
        reasons.append(f"customer bought {ctx.days_since_last_order} days ago")
    if p.offer_type in DISCOUNT_TYPES:
        if ctx.segment == "deal_hunter" and ctx.consecutive_discount_claims >= e.discount_addiction_claims:
            reasons.append(f"deal hunter claimed {ctx.consecutive_discount_claims} discounts in a row "
                           "(discount-addiction guard: points only)")
        risky_cat = p.category is None or p.category in e.return_abuse_categories
        if ctx.return_rate is not None and ctx.return_rate >= e.return_abuse_rate and risky_cat:
            reasons.append(f"customer returns {ctx.return_rate:.0%} of orders (no money-off on "
                           f"{p.category or 'store-wide'})")
    return _fail("customer_eligibility", *reasons) if reasons else _ok("customer_eligibility")


def check_expiry_stacking(p, ctx, rules) -> CheckResult:
    lo, hi = rules.global_.offer_validity_days
    reasons = []
    if not lo <= p.expiry_days <= hi:
        reasons.append(f"expiry of {p.expiry_days} days is outside {lo}-{hi} days")
    if not rules.global_.allow_stacking and ctx.has_active_offer:
        reasons.append("customer already has a live offer and stacking is off")
    return _fail("expiry_stacking", *reasons) if reasons else _ok("expiry_stacking")


def check_margin(p, ctx, rules, cost: float) -> CheckResult:
    basket = basket_value(p, ctx, rules)
    margin = basket * _margin_pct(p.category, ctx)
    profit = margin - cost
    floor = rules.global_.min_profit_after_discount_inr
    if profit <= floor:
        return _fail("profit_margin", f"expected margin Rs {margin:,.0f} on a Rs {basket:,.0f} order minus "
                                      f"Rs {cost:,.0f} offer cost = Rs {profit:,.0f} (must be above Rs {floor:g}).")
    return _ok("profit_margin")


# ---------- message checks ----------
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
PCT_RE = re.compile(_NUM + r"\s*(?:%|percent\b)", re.I)
INR_RE = re.compile(r"(?:₹|\brs\.?|\binr)\s*" + _NUM + r"|" + _NUM + r"\s*(?:rupees|rupaye|/-)", re.I)
POINTS_RE = re.compile(_NUM + r"\s*(?:bonus\s+|reward\s+|extra\s+)?(?:points|pts)\b", re.I)
DAYS_RE = re.compile(_NUM + r"\s*(?:days?|din)\b", re.I)
CUSTOMER_ID_RE = re.compile(r"\bC\d{5}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
LINK_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|in|net|org|ly|io)\b", re.I)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
SIMPLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]{0,19}$")
FREE_DELIVERY_RE = re.compile(r"free\s+(?:delivery|shipping)", re.I)
# Text that looks like leaked instructions / prompt injection, never acceptable in a customer message.
INJECTION_MARKERS = ["ignore previous", "ignore all", "ignore the above", "system prompt", "as an ai",
                     "developer mode", "instructions", "<script"]
# Words that point at a category; a message may only talk about the offer's / customer's categories.
CATEGORY_WORDS = {
    "electronics": ["electronics", "gadget", "gadgets"],
    "mobiles": ["mobile", "mobiles", "smartphone", "smartphones", "phone", "phones"],
    "grocery": ["grocery", "groceries"],
    "home": ["home decor", "homeware", "kitchen"],
    "beauty": ["beauty", "makeup", "skincare", "cosmetics"],
    "fashion": ["fashion", "clothes", "clothing", "apparel", "outfit", "outfits"],
    "footwear": ["footwear", "shoes", "sneakers", "sandals"],
}


def _nums(regex: re.Pattern, text: str) -> list[float]:
    out = []
    for m in regex.finditer(text):
        raw = next(g for g in m.groups() if g)
        out.append(float(raw.replace(",", "")))
    return out


def _close(x: float, allowed: set[float]) -> bool:
    return any(abs(x - a) < 0.51 for a in allowed)


def check_message_length(p, ctx, rules) -> CheckResult:
    n, limit = len(p.message), rules.message.max_length_chars
    return _ok("message_length") if n <= limit else _fail("message_length", f"{n} characters (max {limit}).")


def check_banned_phrases(p, ctx, rules) -> CheckResult:
    low = p.message.lower()
    hits = [ph for ph in rules.message.banned_phrases if ph.lower() in low]
    leaks = [m for m in INJECTION_MARKERS if m in low]
    reasons = [f"banned phrase '{h}'" for h in hits] + [f"looks like leaked instructions ('{m}')" for m in leaks]
    return _fail("message_banned_phrases", *reasons) if reasons else _ok("message_banned_phrases")


def check_numbers_match(p, ctx, rules) -> CheckResult:
    """Every number in the text must match the structured offer (no '50% off' when the offer is 10%)."""
    text = p.message
    reasons = []

    allowed_pct = {p.value} if p.offer_type == "percent_off" else set()
    for x in _nums(PCT_RE, text):
        if not _close(x, allowed_pct):
            reasons.append(f"text says {x:g}% but the offer is " +
                           (f"{p.value:g}%" if allowed_pct else f"{p.offer_type}, not a % discount"))

    allowed_inr = {_min_order(p, rules), *ctx.cart_prices_inr}
    if p.offer_type == "flat_off":
        allowed_inr.add(p.value)
    if p.offer_type == "percent_off":
        allowed_inr.add(rules.global_.max_flat_discount_inr)  # "up to Rs 500"
    for x in _nums(INR_RE, text):
        if not _close(x, allowed_inr):
            reasons.append(f"text mentions Rs {x:g}, which is not the offer value, minimum order or a cart price")

    allowed_pts = {float(ctx.points_balance), float(ctx.points_expiring)}
    if p.offer_type == "bonus_points":
        allowed_pts.add(p.value)
    for x in _nums(POINTS_RE, text):
        if not _close(x, allowed_pts):
            reasons.append(f"text mentions {x:g} points, which is not the offer or the customer's balance")

    allowed_days = {float(p.expiry_days)}
    if ctx.days_since_last_order is not None:
        allowed_days.add(float(ctx.days_since_last_order))
    for x in _nums(DAYS_RE, text):
        if not _close(x, allowed_days):
            reasons.append(f"text mentions {x:g} days but the offer is valid for {p.expiry_days} days")

    return _fail("message_numbers_match", *reasons) if reasons else _ok("message_numbers_match")


def check_privacy(p, ctx, rules) -> CheckResult:
    text = p.message
    reasons = []
    ids = {i for i in CUSTOMER_ID_RE.findall(text) if i != ctx.customer_id}
    if ids:
        reasons.append(f"mentions another customer ID ({', '.join(sorted(ids))})")
    if CUSTOMER_ID_RE.search(text) and not ids:
        reasons.append("shows an internal customer ID")
    if PHONE_RE.search(text):
        reasons.append("contains a phone number")
    if EMAIL_RE.search(text):
        reasons.append("contains an email address")
    low = text.lower()
    own = ctx.full_name.lower()
    others = [n for n in ctx.other_names if n and n.lower() != own and n.lower() in low]
    if others:
        reasons.append(f"mentions another customer's name ({others[0]})")
    return _fail("message_privacy", *reasons) if reasons else _ok("message_privacy")


def check_claims(p, ctx, rules) -> CheckResult:
    low = p.message.lower()
    reasons = [f"unsupported claim '{c}'" for c in rules.message.banned_claims if c.lower() in low]
    if FREE_DELIVERY_RE.search(p.message) and p.offer_type != "free_delivery":
        reasons.append("promises free delivery but the offer is not free delivery")
    if "early access" in low and p.offer_type != "early_access":
        reasons.append("promises early access but the offer is not early access")
    ok_cats = {c for c in [p.category, *ctx.favourite_categories, *ctx.cart_categories] if c}
    for cat, words in CATEGORY_WORDS.items():
        if cat in ok_cats:
            continue
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            reasons.append(f"talks about {cat}, which is not the offer's or the customer's category")
            break
    return _fail("message_claims", *reasons) if reasons else _ok("message_claims")


def check_style(p, ctx, rules) -> CheckResult:
    reasons = []
    n_emoji = len(EMOJI_RE.findall(p.message))
    if n_emoji > rules.message.max_emojis:
        reasons.append(f"{n_emoji} emojis (max {rules.message.max_emojis})")
    if LINK_RE.search(p.message):
        reasons.append("contains a link (links are added by the bot, never by the message)")
    if SIMPLE_NAME_RE.match(ctx.first_name or "") and ctx.first_name.lower() not in p.message.lower():
        reasons.append(f"does not greet the customer by first name ({ctx.first_name})")
    return _fail("message_style", *reasons) if reasons else _ok("message_style")


# ============================================================ entry point
def parse_proposal(raw: OfferProposal | dict | str) -> OfferProposal:
    """Parse AI output into the strict schema. Raises ValueError with a readable reason."""
    if isinstance(raw, OfferProposal):
        return raw
    try:
        if isinstance(raw, str):
            return OfferProposal.model_validate_json(raw)
        return OfferProposal.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'input'}: {err['msg']}" for err in e.errors())
        raise ValueError(problems) from None


def validate(raw: OfferProposal | dict | str, ctx: ValidationContext, rules: Rules | None = None) -> ValidationResult:
    """Run every check. Any failure means the offer must not be sent (Node 10 fallback instead)."""
    rules = rules or load_rules()
    try:
        p = parse_proposal(raw)
    except ValueError as e:
        return ValidationResult(False, [_fail("schema", str(e))])

    cost = expected_cost(p, ctx, rules)
    checks = [
        _ok("schema"),
        check_offer_type(p, ctx, rules),
        check_discount_caps(p, ctx, rules),
        check_flat_and_min_order(p, ctx, rules),
        check_budget(p, ctx, rules, cost),
        check_customer(p, ctx, rules),
        check_expiry_stacking(p, ctx, rules),
        check_margin(p, ctx, rules, cost),
        check_message_length(p, ctx, rules),
        check_banned_phrases(p, ctx, rules),
        check_numbers_match(p, ctx, rules),
        check_privacy(p, ctx, rules),
        check_claims(p, ctx, rules),
        check_style(p, ctx, rules),
    ]
    return ValidationResult(all(c.passed for c in checks), checks, p, cost)
