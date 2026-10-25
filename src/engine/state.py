"""State carried through the 10-node pipeline, plus the shared dependencies (Deps).

One `State` = one offer decision for one customer. Each node reads what earlier
nodes filled in and adds its own part, so the finished State is a complete record
of why we did (or did not) send an offer — it becomes one row in decision_log.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from src.features import load_segment_config
from src.guardrails import OfferProposal, Rules, ValidationContext, ValidationResult, load_rules
from src.occasions import Calendar, load_calendar
from src.offers import Catalogue, load_catalogue

# An agent turns (customer summary, allowed options) into raw JSON text.
Agent = Callable[[dict, list[dict]], str]


class State(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    customer_id: str
    now: datetime
    path: list[str] = Field(default_factory=list)          # nodes visited, in order

    # 1 Load customer
    found: bool = False
    features: dict[str, Any] = Field(default_factory=dict)

    # 2 Detect trigger
    trigger: str | None = None
    trigger_detail: str = ""
    occasion_key: str | None = None

    # 3 Segment & score
    segment: str | None = None
    segment_reason: str = ""
    churn_risk: float | None = None
    buy_anyway: float | None = None
    price_sensitivity: float | None = None
    customer_value: float | None = None

    # 4 Eligibility
    eligible: bool = False
    skip_reason: str | None = None
    is_holdout: bool = False
    ctx: ValidationContext | None = None
    allowed_options: list[dict] = Field(default_factory=list)
    ladder_step: int = 0                    # escalation step (0 = first offer, not a follow-up)

    # 5 AI offer agent
    ai_input: dict[str, Any] = Field(default_factory=dict)
    ai_raw: str | None = None
    ai_error: str | None = None
    ai_source: str | None = None

    # 6 Guardrail validator
    validation: ValidationResult | None = None

    # 10 Fallback
    fallback_used: bool = False
    fallback_tried: list[tuple[str, list[str]]] = Field(default_factory=list)

    # Final offer (from 6 or 10)
    final_offer: OfferProposal | None = None
    final_offer_id: str | None = None       # "AI" or a catalogue id
    final_validation: ValidationResult | None = None
    headline: str = ""

    # 7 Send
    offer_summary: str = ""
    coupon_code: str | None = None
    expires_on: date | None = None
    send_at: datetime | None = None
    card_text: str = ""
    status: str | None = None               # sent / queued / skipped / holdout / no_offer

    # 8 Track
    decision_id: int | None = None

    @property
    def first_name(self) -> str:
        return self.features.get("first_name") or ""


@dataclass
class Deps:
    """Everything the nodes need that is not about one customer. Loaded once, reused."""
    conn: sqlite3.Connection
    rules: Rules
    catalogue: Catalogue
    calendar: Calendar
    segment_config: dict
    category_margins: dict[str, float]
    all_names: frozenset[str]
    agent: Agent
    agent_name: str = "stub"
    features: pd.DataFrame | None = None    # precomputed features for batch runs (optional)
    dry_run: bool = False                    # True = decide but write nothing (previews, tests)
    on_demand: bool = False                  # customer asked (/myoffer): send now even in quiet hours
    channel: str = "cli"
    _spent_today: dict[date, float] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: sqlite3.Connection, agent: Agent | None = None, agent_name: str | None = None,
             **kwargs) -> "Deps":
        if agent is None:
            from src.agent import stub_agent
            agent, agent_name = stub_agent, "stub"
        margins = {r["category"]: r["margin_pct"] for r in conn.execute("SELECT category, margin_pct FROM categories")}
        names = frozenset(r["name"] for r in conn.execute("SELECT name FROM customers"))
        return cls(conn=conn, rules=load_rules(), catalogue=load_catalogue(), calendar=load_calendar(),
                   segment_config=load_segment_config(), category_margins=margins, all_names=names,
                   agent=agent, agent_name=agent_name or getattr(agent, "__name__", "agent"), **kwargs)
