"""Rule editor: comment-preserving saves, validation, preview, and hot reload."""
import copy
import os
import shutil
import time
from datetime import datetime, time as dtime

import pytest
import yaml

from src import db
from src.engine.state import Deps
from src.guardrails import RULES_PATH, load_rules, parse_rules
from src.rule_editor import apply_changes, preview_change, readable_errors, save_rules
from tests.conftest import TODAY

NOON = datetime.combine(TODAY, dtime(14, 0))


@pytest.fixture
def rules_copy(tmp_path):
    path = tmp_path / "rules.yaml"
    shutil.copy(RULES_PATH, path)
    return path


def current():
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def test_only_changed_values_move_and_comments_stay():
    text = RULES_PATH.read_text(encoding="utf-8")
    new = current()
    new["global"]["max_discount_pct"] = 15
    new["global"]["cooldown_days"] = 5
    out = apply_changes(text, new)
    changed = [(a, b) for a, b in zip(text.splitlines(), out.splitlines()) if a != b]
    assert len(changed) == 2
    assert "max_discount_pct: 15" in out and "# never give more than 15% off" in out  # comment follows the value
    assert "cooldown_days: 5" in out and "per 5 days" in out
    assert out.count("#") == text.count("#")
    assert yaml.safe_load(out) == new


def test_lists_and_segment_rules():
    text = RULES_PATH.read_text(encoding="utf-8")
    new = current()
    new["global"]["offer_validity_days"] = [3, 6]
    new["segment_rules"]["loyal"]["allowed_types"] = ["bonus_points"]
    new["message"]["banned_phrases"] = new["message"]["banned_phrases"] + ['say "act now"']
    out = apply_changes(text, new)
    assert "offer_validity_days: [3, 6]" in out
    assert "loyal: { max_discount_pct: 5, allowed_types: [bonus_points] }" in out
    assert yaml.safe_load(out) == new
    parse_rules(out)


def test_unchanged_rules_produce_identical_file():
    text = RULES_PATH.read_text(encoding="utf-8")
    assert apply_changes(text, current()) == text


def test_save_validates_and_backs_up(rules_copy):
    new = yaml.safe_load(rules_copy.read_text(encoding="utf-8"))
    new["global"]["daily_budget_inr"] = 30000
    backup = save_rules(new, rules_copy)
    assert backup.exists() and backup.parent.name == "history"
    assert "daily_budget_inr: 30000" not in backup.read_text(encoding="utf-8")
    assert load_rules(rules_copy).global_.daily_budget_inr == 30000


def test_invalid_rules_are_not_saved(rules_copy):
    before = rules_copy.read_text(encoding="utf-8")
    bad = yaml.safe_load(before)
    bad["global"]["max_discount_pct"] = 150
    with pytest.raises(Exception) as exc:
        save_rules(bad, rules_copy)
    assert rules_copy.read_text(encoding="utf-8") == before
    assert any("max_discount_pct" in e for e in readable_errors(exc.value))


@pytest.fixture
def live(gen_db_path, tmp_path):
    path = tmp_path / "live.db"
    shutil.copy(gen_db_path, path)
    c = db.connect(path)
    yield c
    c.close()


def test_preview_stricter_rules_mean_cheaper_offers(live):
    old = load_rules()
    stricter = copy.deepcopy(current())
    stricter["global"]["daily_budget_inr"] = 5000
    p = preview_change(live, old, parse_rules(stricter), now=NOON, sample=300)
    assert p.cost_new < p.cost_old and p.offers_new <= p.offers_old
    assert p.affected > 0 and p.examples
    assert {"segment", "offers_old", "offers_new", "cost_old", "cost_new"} <= set(p.by_segment[0])


def test_preview_bigger_holdout_means_fewer_offers(live):
    """With money to spare, a bigger control group means fewer offers. (When the budget is the limit, the
    money freed by the holdout goes to other customers instead, which the preview also shows.)"""
    roomy = copy.deepcopy(current())
    roomy["global"]["daily_budget_inr"] = 10_000_000
    bigger = copy.deepcopy(roomy)
    bigger["global"]["holdout_pct"] = 50
    p = preview_change(live, parse_rules(roomy), parse_rules(bigger), now=NOON, sample=300)
    assert p.offers_new < p.offers_old


def test_preview_same_rules_changes_nothing_and_writes_nothing(live):
    p = preview_change(live, load_rules(), load_rules(), now=NOON, sample=200)
    assert p.affected == 0 and p.offers_old == p.offers_new
    assert live.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 0


def test_deps_hot_reload(live, monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(RULES_PATH.parent, cfg, ignore=shutil.ignore_patterns("history"))
    import src.engine.state as state
    import src.guardrails as guardrails
    monkeypatch.setattr(state, "CONFIG_DIR", cfg)
    monkeypatch.setattr(guardrails, "RULES_PATH", cfg / "rules.yaml")
    monkeypatch.setattr(state, "load_rules", lambda: guardrails.load_rules(cfg / "rules.yaml"))
    deps = Deps.load(live)
    assert deps.refresh_config() is False
    new = yaml.safe_load((cfg / "rules.yaml").read_text(encoding="utf-8"))
    new["global"]["cooldown_days"] = 2
    save_rules(new, cfg / "rules.yaml")
    future = time.time() + 5
    os.utime(cfg / "rules.yaml", (future, future))
    assert deps.refresh_config() is True and deps.rules.global_.cooldown_days == 2
    (cfg / "rules.yaml").write_text("global: {broken", encoding="utf-8")
    os.utime(cfg / "rules.yaml", (future + 5, future + 5))
    assert deps.refresh_config() is False and deps.rules.global_.cooldown_days == 2  # broken file ignored


def test_rule_editor_page_loads():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60).run()
    at.sidebar.radio[1].set_value("Rule editor").run()
    assert not at.exception, at.exception
    assert any("No changes yet" in i.value for i in at.info)
