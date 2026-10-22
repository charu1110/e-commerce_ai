"""Print the distributions that prove the synthetic data behaves like real e-commerce data.

Each check prints PASS or WARN against a target range taken from the brief
(CLAUDE.md section 5, "Realism rules"). Judges can run this to see the evidence.

Run:  python scripts/validate_data.py [--db data/offers.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.db import DB_PATH  # noqa: E402
from src.occasions import load_calendar  # noqa: E402

RESULTS: list[bool] = []


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check(label: str, value: float, lo: float, hi: float, fmt: str = "{:.1%}") -> None:
    ok = lo <= value <= hi
    RESULTS.append(ok)
    target = f"{fmt.format(lo)} - {fmt.format(hi)}"
    print(f"  [{'PASS' if ok else 'WARN'}] {label:<48} {fmt.format(value):>9}   (target {target})")


def table(df: pd.DataFrame) -> None:
    print("  " + df.to_string().replace("\n", "\n  "))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()
    if not Path(args.db).exists():
        sys.exit(f"No database at {args.db}. Run: python -m src.generate_data")

    conn = sqlite3.connect(args.db)
    q = lambda sql: pd.read_sql_query(sql, conn)  # noqa: E731
    customers = q("SELECT * FROM customers")
    truth = q("SELECT * FROM customer_truth")
    orders = q("SELECT * FROM orders")
    orders["order_ts"] = pd.to_datetime(orders["order_ts"])
    orders["day"] = orders["order_ts"].dt.normalize()
    carts = q("SELECT status, category FROM cart_events")
    sessions = q("SELECT customer_id, ts FROM sessions")
    sessions["ts"] = pd.to_datetime(sessions["ts"])
    points = q("SELECT * FROM reward_points")
    today = orders["day"].max()

    cust = customers.merge(truth, on="customer_id")
    o = orders.merge(cust[["customer_id", "persona", "city_tier", "region", "typical_gap_days"]],
                     on="customer_id")

    print(f"Database: {args.db}")
    print(f"{len(customers):,} customers, {len(orders):,} orders, {len(sessions):,} sessions, "
          f"{len(carts):,} cart events")
    print(f"Order history: {orders['day'].min().date()} to {today.date()}")

    # ---- personas -------------------------------------------------------
    section("Personas (hidden ground truth)")
    per = o.groupby("persona").agg(orders=("order_id", "count"), aov=("net_amount", "mean"),
                                   coupon_rate=("coupon_code", lambda s: s.notna().mean()))
    per["customers"] = truth["persona"].value_counts()
    per["share"] = per["customers"] / len(truth)
    per["orders_per_cust"] = per["orders"] / per["customers"]
    per = per[["customers", "share", "orders_per_cust", "aov", "coupon_rate"]]
    table(per.round({"share": 3, "orders_per_cust": 1, "aov": 0, "coupon_rate": 2}))
    check("Deal hunters use coupons more than loyal",
          per.loc["deal_hunter", "coupon_rate"] - per.loc["loyal", "coupon_rate"], 0.3, 1.0, "{:+.2f}")
    check("VIP order value vs average (x)", per.loc["vip", "aov"] / orders["net_amount"].mean(), 1.8, 5.0, "{:.1f}x")

    # ---- spend concentration -------------------------------------------
    section("Spend concentration (Pareto)")
    rev = orders.groupby("customer_id")["net_amount"].sum().sort_values(ascending=False)
    top20 = rev.head(int(len(customers) * 0.2)).sum() / rev.sum()
    check("Revenue share of top 20% customers", top20, 0.70, 0.80)

    # ---- purchase gaps --------------------------------------------------
    section("Time between purchases (Gamma around typical gap)")
    oo = o.sort_values(["customer_id", "order_ts"])
    oo["gap"] = oo.groupby("customer_id")["order_ts"].diff().dt.days
    g = oo.dropna(subset=["gap"]).groupby("customer_id").agg(
        n=("gap", "size"), mean_gap=("gap", "mean"), std_gap=("gap", "std"),
        typical=("typical_gap_days", "first"))
    g = g[g["n"] >= 5]
    check("Observed mean gap / typical gap (median)", (g["mean_gap"] / g["typical"]).median(), 0.75, 1.15, "{:.2f}")
    check("Gap variability, CV (Gamma k=4 -> ~0.5)", (g["std_gap"] / g["mean_gap"]).median(), 0.35, 0.70, "{:.2f}")

    # ---- seasonality ----------------------------------------------------
    section("Seasonality")
    monthly = orders.groupby(orders["order_ts"].dt.to_period("M")).size().rename("orders")
    table(monthly.to_frame().T)
    daily = orders.groupby("day").size()
    baseline = daily.median()
    cal = load_calendar()
    diwali = next(oc for oc in cal.occasions if oc.key == "diwali")
    for start, end in diwali.windows:
        if pd.Timestamp(start) >= daily.index.min() and pd.Timestamp(end) <= today:
            lift = daily[(daily.index >= pd.Timestamp(start)) & (daily.index <= pd.Timestamp(end))].mean() / baseline
            check(f"Diwali {start.year} daily orders vs normal day", lift, 1.3, 2.5, "{:.2f}x")
    punjab_only = [oc for oc in cal.occasions if oc.regions]
    for oc in punjab_only:
        for start, end in oc.windows:
            if pd.Timestamp(start) >= daily.index.min() and pd.Timestamp(end) <= today:
                sub = o[(o["day"] >= pd.Timestamp(start)) & (o["day"] <= pd.Timestamp(end))]
                share = (sub["region"] == "punjab").mean()
                base = (o["region"] == "punjab").mean()
                print(f"  [info] {oc.name} {start.year}: Punjab share of orders {share:.1%} vs normal {base:.1%}")
    weekend = (orders["order_ts"].dt.weekday >= 5).mean()
    check("Weekend share of orders (no effect = 28.6%)", weekend, 0.31, 0.42)
    salary = (orders["order_ts"].dt.day <= 5).mean()
    check("Salary days (1st-5th) share (no effect = 16.4%)", salary, 0.18, 0.26)

    # ---- carts & returns ------------------------------------------------
    section("Carts and returns")
    abandon = (carts["status"] != "purchased").mean()
    check("Cart abandonment rate", abandon, 0.65, 0.75)
    ret = orders.groupby("category")["returned"].mean()
    table(ret.rename("return_rate").round(3).to_frame().T)
    check("Fashion return rate", ret.get("fashion", 0), 0.20, 0.30)
    check("Electronics return rate", ret.get("electronics", 0), 0.05, 0.08)

    # ---- payments -------------------------------------------------------
    section("Payment methods by city tier")
    pay = pd.crosstab(o["city_tier"], o["payment_method"], normalize="index").round(3)
    table(pay)
    check("UPI share overall", (orders["payment_method"] == "UPI").mean(), 0.40, 0.65)
    check("COD share: tier 3 minus tier 1", pay.loc[3, "COD"] - pay.loc[1, "COD"], 0.10, 0.40, "{:+.2f}")

    # ---- lapsing / hibernating / new ------------------------------------
    section("Lapsing, hibernating and new customers")
    last = orders.groupby("customer_id")["day"].max().rename("last_order")
    c = cust.merge(last, on="customer_id", how="left")
    c["days_since"] = (today - c["last_order"]).dt.days
    lap = c[c["persona"] == "lapsing"]
    check("Lapsers: days since last order / typical gap > 2", (lap["days_since"] / lap["typical_gap_days"] > 2).mean(), 0.9, 1.0)
    check("Lapsers: median days since last order", lap["days_since"].median(), 21, 56, "{:.0f}")
    last_sess = sessions.groupby("customer_id")["ts"].max().dt.normalize().rename("last_session")
    lap = lap.merge(last_sess, on="customer_id", how="left")
    check("Lapsers still browsing after they stopped buying", (lap["last_session"] > lap["last_order"]).mean(), 0.45, 0.75)
    hib = c[c["persona"] == "hibernating"]
    check("Hibernating: no order in last 180 days", (hib["days_since"] >= 180).mean(), 0.99, 1.0)
    new = c[c["persona"] == "new"]
    signup_age = (today - pd.to_datetime(new["signup_date"])).dt.days
    check("New customers signed up < 30 days ago", (signup_age < 30).mean(), 0.99, 1.0)
    new_orders = o[o["persona"] == "new"].groupby("customer_id").size().reindex(new["customer_id"], fill_value=0)
    check("New customers with 0-1 orders", (new_orders <= 1).mean(), 0.99, 1.0)
    loyal = c[c["persona"] == "loyal"]
    check("Loyal: bought within 2x their usual gap", (loyal["days_since"] / loyal["typical_gap_days"] <= 2).mean(), 0.85, 1.0)

    # ---- profile fields -------------------------------------------------
    section("Customer profile")
    check("Marketing opt-in", customers["marketing_opt_in"].mean(), 0.85, 0.95)
    check("Age 22-40 share", customers["age"].between(22, 40).mean(), 0.55, 0.85)
    check("Android share", (customers["device"] == "android").mean(), 0.55, 0.80)
    table(pd.crosstab(customers["region"], customers["preferred_language"], normalize="index").round(2))
    table(customers["city"].value_counts().rename("customers").to_frame().T)
    check("Customers with points expiring soon", (points["points_expiring"] > 0).mean(), 0.20, 0.40)

    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} realism checks passed")


if __name__ == "__main__":
    main()
