"""Segment labels (Node 3) with a plain-English reason for each.

Segments decide which offer types and discount caps apply (see config/rules.yaml),
so every label comes with a sentence a marketer or a customer can read — it is
reused by the audit log, the dashboard and the bot's "Why this offer?" button.

Order matters (first match wins), thresholds live in config/segments.yaml:
new -> hibernating -> lapsing -> vip -> deal_hunter -> loyal -> occasional
"""
from __future__ import annotations

import argparse
import math
from datetime import date

import pandas as pd

from src import db
from src.features import compute_features, load_segment_config
from src.formatting import indian_number

SEGMENTS = ["new", "hibernating", "lapsing", "vip", "deal_hunter", "loyal", "occasional"]


def _inr(x: float) -> str:
    return "Rs " + indian_number(x)


def assign_segment(f: dict, config: dict | None = None) -> tuple[str, str]:
    """Return (segment, reason) for one customer's features."""
    c = (config or load_segment_config())["segments"]
    n = int(f.get("n_orders") or 0)
    since = f.get("days_since_last_order")
    gap = f.get("typical_gap")
    ratio = f.get("gap_ratio")
    orders_365 = int(f.get("orders_365d") or 0)
    spend_365 = float(f.get("spend_365d") or 0)
    coupon_share = f.get("coupon_share")

    if f.get("days_since_signup", 10**6) < c["new_customer_days"]:
        return "new", f"Joined {f['days_since_signup']} days ago with {n} order(s) so far."

    if n == 0:
        return "occasional", f"Signed up {f['days_since_signup']} days ago but has never ordered."

    if since is not None and since >= c["hibernating_days"]:
        return "hibernating", f"No order for {since} days (over {c['hibernating_days']})."

    if (n >= c["min_orders_for_habit"] and gap is not None and not _nan(gap)
            and ratio is not None and ratio > c["lapsed_gap_ratio"]):
        return "lapsing", (f"Used to order every ~{gap:.0f} days but the last order was {since} days ago "
                           f"({ratio:.1f}x the usual gap).")

    if spend_365 >= c["vip_min_spend_365d"] and orders_365 >= c["vip_min_orders_365d"]:
        return "vip", f"Spent {_inr(spend_365)} across {orders_365} orders in the last year."

    if (n >= c["deal_hunter_min_orders"] and coupon_share is not None
            and coupon_share >= c["deal_hunter_min_coupon_share"]):
        return "deal_hunter", f"Used a coupon on {coupon_share:.0%} of {n} orders."

    if orders_365 >= c["loyal_min_orders_365d"]:
        return "loyal", f"{orders_365} orders in the last year and still on their usual rhythm."

    return "occasional", f"Only {orders_365} order(s) in the last year."


def _nan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)


def segment_all(features: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Add `segment` and `segment_reason` columns to a features table."""
    config = config or load_segment_config()
    out = features.copy()
    labels = [assign_segment(row, config) for row in features.to_dict("records")]
    out["segment"] = [s for s, _ in labels]
    out["segment_reason"] = [r for _, r in labels]
    return out


def main() -> None:
    """Print segment sizes and how well segments recover the hidden personas."""
    parser = argparse.ArgumentParser(description="Segment all customers and compare with hidden personas.")
    parser.add_argument("--db", default=str(db.DB_PATH))
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    conn = db.connect(args.db)
    seg = segment_all(compute_features(conn, args.today))
    print("Segment sizes:")
    print(seg["segment"].value_counts().reindex(SEGMENTS, fill_value=0).to_string())
    print("\nAverage scores by segment:")
    cols = ["churn_risk", "buy_anyway", "price_sensitivity", "gap_ratio", "spend_365d"]
    print(seg.groupby("segment")[cols].mean().reindex(SEGMENTS).round(2).to_string())

    # Evaluation only: the engine never reads customer_truth.
    truth = pd.read_sql_query("SELECT customer_id, persona FROM customer_truth", conn).set_index("customer_id")
    both = seg.join(truth, how="inner")
    print("\nHidden persona (rows) vs segment (columns), row %:")
    print((pd.crosstab(both["persona"], both["segment"], normalize="index") * 100).round(0)
          .reindex(columns=SEGMENTS, fill_value=0).to_string())


if __name__ == "__main__":
    main()
