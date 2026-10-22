"""Pre-approved offer catalogue and the fallback picker (Node 10).

Business reason: when the AI's offer breaks a rule, the AI is down, or it times out,
we still want to send something safe — but only something marketing approved in
config/offers.yaml, and only if it ALSO passes the guardrail validator. If no
fallback passes, we send nothing (doing nothing is always safe).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.formatting import indian_number, plain_number
from src.guardrails import OfferProposal, Rules, ValidationContext, ValidationResult, load_rules, validate

OFFERS_PATH = Path(__file__).resolve().parent.parent / "config" / "offers.yaml"
CATEGORY_LABELS = {"home": "home & kitchen"}  # how a category reads inside a sentence


@dataclass(frozen=True)
class CatalogueOffer:
    id: str
    offer_type: str
    value: float
    category: str | None          # None = store-wide, "favourite" = customer's cart/favourite category
    min_order_inr: float
    expiry_days: int
    headline: str
    message: str


@dataclass(frozen=True)
class Catalogue:
    offers: dict[str, CatalogueOffer]
    fallbacks: dict[str, list[str]]


@dataclass
class FallbackResult:
    offer: CatalogueOffer | None
    proposal: OfferProposal | None
    result: ValidationResult | None
    tried: list[tuple[str, list[str]]] = field(default_factory=list)  # (offer id, violations) that failed

    @property
    def found(self) -> bool:
        return self.offer is not None


def load_catalogue(path: Path | str = OFFERS_PATH) -> Catalogue:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    offers = {
        oid: CatalogueOffer(
            id=oid, offer_type=o["offer_type"], value=float(o.get("value") or 0),
            category=o.get("category") or None, min_order_inr=float(o["min_order_inr"]),
            expiry_days=int(o["expiry_days"]), headline=o["headline"], message=o["message"],
        )
        for oid, o in (raw.get("offers") or {}).items()
    }
    fallbacks = {seg: list(ids or []) for seg, ids in (raw.get("fallbacks") or {}).items()}
    for seg, ids in fallbacks.items():
        missing = [i for i in ids if i not in offers]
        if missing:
            raise ValueError(f"offers.yaml: fallback for '{seg}' refers to unknown offer(s) {missing}")
    return Catalogue(offers, fallbacks)


def resolve_category(spec: str | None, ctx: ValidationContext) -> str | None:
    """'favourite' -> what's in their cart, else their top favourite category."""
    if spec != "favourite":
        return spec
    for c in (*ctx.cart_categories, *ctx.favourite_categories):
        if c:
            return c
    return None


def render(offer: CatalogueOffer, ctx: ValidationContext) -> tuple[str, OfferProposal]:
    """Fill in placeholders. Returns (headline, proposal)."""
    category = resolve_category(offer.category, ctx)
    fields = dict(
        first_name=ctx.first_name or "there",
        value=plain_number(offer.value) if offer.offer_type != "flat_off" else indian_number(offer.value),
        category=CATEGORY_LABELS.get(category, category) if category else "everything",
        min_order=indian_number(offer.min_order_inr),
        expiry_days=offer.expiry_days,
    )
    proposal = OfferProposal(
        offer_type=offer.offer_type, value=offer.value, category=category,
        expiry_days=offer.expiry_days, min_order_inr=offer.min_order_inr,
        message=offer.message.format(**fields),
        reason=f"Pre-approved fallback offer {offer.id} for the {ctx.segment} segment.",
    )
    return offer.headline.format(**fields), proposal


def fallback_offer(ctx: ValidationContext, rules: Rules | None = None,
                   catalogue: Catalogue | None = None) -> FallbackResult:
    """First pre-approved offer for this segment that passes every rule, or nothing."""
    rules = rules or load_rules()
    catalogue = catalogue or load_catalogue()
    out = FallbackResult(None, None, None)
    for oid in catalogue.fallbacks.get(ctx.segment, []):
        offer = catalogue.offers[oid]
        _, proposal = render(offer, ctx)
        result = validate(proposal, ctx, rules)
        if result.passed:
            out.offer, out.proposal, out.result = offer, proposal, result
            return out
        out.tried.append((oid, result.violations))
    return out
