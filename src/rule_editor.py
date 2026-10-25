"""Rule editing for marketing: preview the effect of a change, then save it safely.

Business reason: a marketer should be able to change "max 20% off" to "max 15% off"
without a developer, and see BEFORE saving how many customers it affects and what
it costs. Saving keeps every plain-English comment in rules.yaml (only the values
change), validates the result, and keeps a timestamped backup.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import yaml

from src.engine.pipeline import run_pipeline
from src.engine.state import Deps
from src.features import compute_features
from src.guardrails import RULES_PATH, Rules, parse_rules


# ------------------------------------------------------------------ writing YAML without losing comments
def _fmt(value, bare: bool = False) -> str:
    """YAML text for a value. `bare` writes list items without quotes (for simple names like bonus_points)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join((v if bare else json.dumps(v, ensure_ascii=False)) if isinstance(v, str) else _fmt(v)
                               for v in value) + "]"
    return str(value)


def _section_span(lines: list[str], section: str) -> tuple[int, int]:
    """Line range [start, end) of a top-level section."""
    start = next(i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i]) and not lines[i].startswith("#")),
               len(lines))
    return start, end


def _refresh_comment(comment: str, old_value: str, new_value: str) -> str:
    """Keep comments truthful: 'never give more than 20% off' follows the value from 20 to 15."""
    if not comment or not re.fullmatch(r"\d+(\.\d+)?", old_value or ""):
        return comment
    pattern = rf"(?<![\d.,]){re.escape(old_value)}(?![\d.,]\d)"
    return re.sub(pattern, new_value, comment)


def _replace_value(lines: list[str], section: str, key: str, new_value: str) -> None:
    start, end = _section_span(lines, section)
    pat = re.compile(rf"^(\s+{re.escape(key)}:\s*)(\[[^\]]*\]|[^#\n]*?)(\s*#.*)?$")
    for i in range(start + 1, end):
        m = pat.match(lines[i])
        if m:
            comment = _refresh_comment(m.group(3) or "", m.group(2).strip(), new_value)
            lines[i] = f"{m.group(1)}{new_value}{comment}"
            return
    raise KeyError(f"{section}.{key} not found in rules.yaml")


def _replace_segment(lines: list[str], segment: str, rule: dict) -> None:
    start, end = _section_span(lines, "segment_rules")
    pat = re.compile(rf"^(\s+){re.escape(segment)}:\s*\{{.*\}}(\s*#.*)?$")
    for i in range(start + 1, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = (f"{m.group(1)}{segment}: {{ max_discount_pct: {_fmt(rule['max_discount_pct'])}, "
                        f"allowed_types: {_fmt(rule['allowed_types'], bare=True)} }}{m.group(2) or ''}")
            return
    raise KeyError(f"segment_rules.{segment} not found in rules.yaml")


def apply_changes(text: str, new: dict) -> str:
    """Rewrite only the values that differ from `text`, keeping layout and comments. Returns new YAML text."""
    old = yaml.safe_load(text)
    lines = text.splitlines()
    for section, values in new.items():
        if section == "segment_rules":
            for seg, rule in values.items():
                if rule != old["segment_rules"].get(seg):
                    _replace_segment(lines, seg, rule)
        elif isinstance(values, dict):
            for key, value in values.items():
                if old.get(section, {}).get(key) != value:
                    _replace_value(lines, section, key, _fmt(value))
    out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if yaml.safe_load(out) != {**old, **new}:  # safety net: the file must say exactly what was asked
        raise ValueError("could not apply the change cleanly; please edit config/rules.yaml by hand")
    return out


def save_rules(new: dict, path: Path = RULES_PATH) -> Path:
    """Validate, back up the current file, then write. Returns the backup path."""
    text = path.read_text(encoding="utf-8")
    new_text = apply_changes(text, new)
    parse_rules(new_text)  # raises with a readable error if anything is invalid
    history = path.parent / "history"
    history.mkdir(exist_ok=True)
    backup = history / f"rules-{datetime.now():%Y%m%d-%H%M%S}.yaml"
    shutil.copy(path, backup)
    path.write_text(new_text, encoding="utf-8")
    return backup


def readable_errors(exc: Exception) -> list[str]:
    """pydantic errors -> short sentences a marketer can act on."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [str(exc)]
    out = []
    for e in errors():
        where = " > ".join(str(x) for x in e["loc"] if x != "global_") or "rules"
        out.append(f"{where}: {e['msg']}")
    return out


# ------------------------------------------------------------------ preview
@dataclass
class Preview:
    sample: int
    population: int
    offers_old: int = 0
    offers_new: int = 0
    cost_old: float = 0.0
    cost_new: float = 0.0
    affected: int = 0
    by_segment: list = field(default_factory=list)
    new_skip_reasons: list = field(default_factory=list)
    examples: list = field(default_factory=list)

    @property
    def scale(self) -> float:
        return self.population / max(self.sample, 1)


def _outcome(s) -> str:
    """What the customer experiences: an offer (type, value, category) or nothing."""
    if s.final_offer is None:
        return "no offer"
    p = s.final_offer
    value = f" {p.value:g}" if p.offer_type in ("percent_off", "flat_off", "bonus_points") else ""
    return f"{p.offer_type.replace('_', ' ')}{value}" + (f" on {p.category}" if p.category else "")


def _describe(s) -> str:
    if s.final_offer is None:
        reason = re.sub(r"\s*\(.*?\)", "", (s.skip_reason or s.status or "").split(".")[0])
        return f"no offer: {reason[:70]}"
    return _outcome(s)


def preview_change(conn, old: Rules, new: Rules, now: datetime | None = None, sample: int = 1000,
                   seed: int = 1) -> Preview:
    """Run today's decisions twice (dry run): current rules vs proposed rules, on a customer sample."""
    import random
    now = now or datetime.now()
    ids = [r["customer_id"] for r in conn.execute("SELECT customer_id FROM customers")]
    population = len(ids)
    ids = sorted(random.Random(seed).sample(ids, min(sample, population)))
    share = len(ids) / population
    base = Deps.load(conn, dry_run=True, channel="preview")
    base.features = compute_features(conn, now.date(), ids)

    def run(rules: Rules):
        # The daily budget is shared by everyone, so a sample gets its share of it.
        scaled = rules.model_copy(update={"global_": rules.global_.model_copy(
            update={"daily_budget_inr": rules.global_.daily_budget_inr * share})})
        deps = replace(base, rules=scaled, _spent_today={})
        from src.engine.pipeline import priority_order
        return {cid: run_pipeline(cid, deps, now) for cid in priority_order(ids, base.features)}

    before, after = run(old), run(new)
    p = Preview(sample=len(ids), population=population)
    segs: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    for cid in ids:
        a, b = before[cid], after[cid]
        seg = segs.setdefault(b.segment or "?", {"segment": b.segment, "offers_old": 0, "offers_new": 0,
                                                  "cost_old": 0.0, "cost_new": 0.0})
        for s, tag in ((a, "old"), (b, "new")):
            if s.final_offer is not None:
                cost = s.final_validation.expected_cost_inr
                setattr(p, f"offers_{tag}", getattr(p, f"offers_{tag}") + 1)
                setattr(p, f"cost_{tag}", getattr(p, f"cost_{tag}") + cost)
                seg[f"offers_{tag}"] += 1
                seg[f"cost_{tag}"] += cost
        if _outcome(a) != _outcome(b):
            p.affected += 1
            if len(p.examples) < 8:
                p.examples.append({"customer": cid, "segment": b.segment, "before": _describe(a), "after": _describe(b)})
            if b.final_offer is None and a.final_offer is not None:
                key = re.sub(r"\d+", "N", (b.skip_reason or "").split(".")[0])
                reasons[key] = reasons.get(key, 0) + 1
    k = p.scale
    p.offers_old, p.offers_new = round(p.offers_old * k), round(p.offers_new * k)
    p.cost_old, p.cost_new, p.affected = p.cost_old * k, p.cost_new * k, round(p.affected * k)
    p.by_segment = [{**v, "offers_old": round(v["offers_old"] * k), "offers_new": round(v["offers_new"] * k),
                     "cost_old": round(v["cost_old"] * k), "cost_new": round(v["cost_new"] * k)}
                    for v in sorted(segs.values(), key=lambda v: str(v["segment"]))]
    p.new_skip_reasons = [{"reason": r, "customers": round(n * k)} for r, n in
                          sorted(reasons.items(), key=lambda kv: -kv[1])[:5]]
    return p
