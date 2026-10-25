"""Numbers for the dashboard, computed from decision_log (live bot or simulation copy).

Kept separate from the Streamlit page so it can be tested without a browser.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd

from src.guardrails import CHECK_LABELS

OFFER_STATUSES = ("sent", "queued", "claimed", "dismissed", "ignored", "expired")


def decisions(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM decision_log ORDER BY decision_id", conn)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def funnel(log: pd.DataFrame) -> pd.DataFrame:
    """Decisions -> had a reason -> eligible -> offer sent -> claimed."""
    if log.empty:
        return pd.DataFrame(columns=["stage", "count"])
    offers = log["status"].isin(OFFER_STATUSES)
    stages = [
        ("Decisions made", len(log)),
        ("Had a reason for an offer", int((log["trigger"].fillna("none") != "none").sum())),
        ("Passed eligibility rules", int((log["eligible"] == 1).sum() + log["is_holdout"].fillna(0).sum())),
        ("Offer sent", int(offers.sum())),
        ("Claimed", int((log["status"] == "claimed").sum())),
    ]
    return pd.DataFrame(stages, columns=["stage", "count"])


def blocked_by_rule(log: pd.DataFrame) -> pd.DataFrame:
    """How often each guardrail check blocked an AI proposal (plain-English labels)."""
    counts: Counter = Counter()
    for v in log.get("violations", pd.Series(dtype=str)).dropna():
        for item in json.loads(v):
            name = item.split(":", 1)[0]
            counts["AI error / timeout" if name == "ai_error" else CHECK_LABELS.get(name, name)] += 1
    return pd.DataFrame(counts.most_common(), columns=["rule", "times"])


def skip_reasons(log: pd.DataFrame, top: int = 8) -> pd.DataFrame:
    s = log.loc[log["status"].isin(["skipped", "no_offer"]), "skip_reason"].dropna()
    s = (s.str.replace(r"\(.*?\)", "", regex=True)          # drop details in brackets
          .str.replace(r"\s*,\s*so .*$", "", regex=True)    # "..., so nothing was sent" = same reason
          .str.replace(r"for this \w+ customer", "for this segment", regex=True)
          .str.replace(r"\d+(\.\d+)?", "N", regex=True)
          .str.replace(r"\s+", " ", regex=True).str.strip(" ."))
    return s.value_counts().head(top).rename_axis("reason").reset_index(name="customers")


def summary(log: pd.DataFrame) -> dict:
    """Headline numbers that don't need a holdout comparison."""
    if log.empty:
        return {"decisions": 0, "offers": 0, "claimed": 0, "claim_rate": 0.0, "committed_inr": 0.0,
                "fallback_rate": 0.0, "ai_pass_rate": 0.0, "holdout": 0}
    offers = log[log["status"].isin(OFFER_STATUSES)]
    ai_tried = log[log["ai_proposal_json"].notna() | log["violations"].fillna("").str.startswith('["ai_error')]
    ai_passed = (offers["final_offer_id"] == "AI").sum()
    return {
        "decisions": int(len(log)),
        "offers": int(len(offers)),
        "claimed": int((log["status"] == "claimed").sum()),
        "claim_rate": float((offers["status"] == "claimed").mean()) if len(offers) else 0.0,
        "committed_inr": float(offers["expected_cost"].fillna(0).sum()),
        "fallback_rate": float((offers["final_offer_id"] != "AI").mean()) if len(offers) else 0.0,
        "ai_pass_rate": float(ai_passed / len(ai_tried)) if len(ai_tried) else 0.0,
        "holdout": int(log["is_holdout"].fillna(0).sum()),
    }


def offers_by(log: pd.DataFrame, column: str) -> pd.DataFrame:
    """Offers sent and claimed per segment / offer type / trigger."""
    offers = log[log["status"].isin(OFFER_STATUSES)]
    if offers.empty:
        return pd.DataFrame(columns=[column, "sent", "claimed", "claim_rate"])
    g = offers.groupby(column).agg(sent=("decision_id", "size"),
                                   claimed=("status", lambda s: int((s == "claimed").sum())))
    g["claim_rate"] = (g["claimed"] / g["sent"]).round(3)
    return g.sort_values("sent", ascending=False).reset_index()


def load_results(path: Path | str) -> dict | None:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
