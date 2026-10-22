"""Festival & occasion calendar, loaded from config/occasions.yaml.

Business reason: demand jumps around Diwali, Karwa Chauth, Lohri etc. The data
generator uses this calendar to create realistic spikes, and the engine (Node 2)
uses the same file to fire `festival` triggers — so marketing edits one file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

OCCASIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "occasions.yaml"


@dataclass(frozen=True)
class Occasion:
    key: str
    name: str
    windows: tuple[tuple[date, date], ...]
    demand_boost: float = 1.0
    boosted_categories: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()  # empty = celebrated everywhere
    budget_multiplier: float = 1.0
    target_segments: tuple[str, ...] = ()
    theme: str = ""
    emoji: str = ""

    def is_active(self, day: date, region: str | None = None) -> bool:
        """True if `day` falls inside one of the windows (and the region celebrates it)."""
        if self.regions and region not in self.regions:
            return False
        return any(start <= day <= end for start, end in self.windows)


@dataclass(frozen=True)
class Calendar:
    occasions: tuple[Occasion, ...]
    recurring: dict = field(default_factory=dict)

    def active(self, day: date, region: str | None = None) -> list[Occasion]:
        return [o for o in self.occasions if o.is_active(day, region)]

    def demand_multiplier(self, day: date, region: str | None = None) -> float:
        """Strongest demand boost active on `day` (boosts don't stack, the biggest wins)."""
        return max((o.demand_boost for o in self.active(day, region)), default=1.0)

    def boosted_categories(self, day: date, region: str | None = None) -> set[str]:
        return {c for o in self.active(day, region) for c in o.boosted_categories}


def load_calendar(path: Path | str = OCCASIONS_PATH) -> Calendar:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    occasions = []
    for key, cfg in (raw.get("occasions") or {}).items():
        windows = tuple(
            (_as_date(w["start"]), _as_date(w["end"])) for w in cfg.get("windows", [])
        )
        occasions.append(
            Occasion(
                key=key,
                name=cfg.get("name", key),
                windows=windows,
                demand_boost=float(cfg.get("demand_boost", 1.0)),
                boosted_categories=tuple(cfg.get("boosted_categories", [])),
                regions=tuple(cfg.get("regions", [])),
                budget_multiplier=float(cfg.get("budget_multiplier", 1.0)),
                target_segments=tuple(cfg.get("target_segments", [])),
                theme=cfg.get("theme", ""),
                emoji=cfg.get("emoji", ""),
            )
        )
    return Calendar(occasions=tuple(occasions), recurring=raw.get("recurring") or {})


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
