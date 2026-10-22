"""Customer features (Node 3 inputs): gap ratio, RFM, churn risk, price sensitivity.

Everything here is computed from OBSERVABLE behaviour only — orders, sessions,
carts, points and the profile. The hidden `customer_truth` table is never read,
so the engine has to discover who is lapsing just like a real store would.

Key business signals:
- gap_ratio      days since last order / that customer's usual gap. > 2 means "stopped".
- churn_risk     0-1, rises steeply once someone is past their usual rhythm.
- buy_anyway     chance they buy in the next 7 days without any offer. High = don't
                 waste a discount on them.
- price_sensitivity  0-1 from coupon use and cart abandonment.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

SEGMENTS_PATH = Path(__file__).resolve().parent.parent / "config" / "segments.yaml"


def load_segment_config(path: Path | str = SEGMENTS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read(conn: sqlite3.Connection, table: str, columns: str, ids: list[str] | None) -> pd.DataFrame:
    sql = f"SELECT {columns} FROM {table}"
    params: list = []
    if ids is not None:
        sql += f" WHERE customer_id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    return pd.read_sql_query(sql, conn, params=params)


def _rfm_score(values: pd.Series, thresholds: list[float], higher_is_better: bool) -> pd.Series:
    """Map values to 1-5 using [t5, t4, t3, t2] thresholds."""
    score = pd.Series(1, index=values.index)
    for s, t in zip((2, 3, 4, 5), reversed(thresholds)):
        hit = values >= t if higher_is_better else values <= t
        score = score.mask(hit.fillna(False), s)
    return score


def compute_features(conn: sqlite3.Connection, today: date | None = None,
                     customer_ids: list[str] | None = None,
                     config: dict | None = None) -> pd.DataFrame:
    """One row per customer (index = customer_id) with every feature the engine uses.

    Pass `customer_ids` for a single customer (pipeline) or None for everyone (dashboard,
    simulator). `today` lets demos time-travel; data after `today` is ignored.
    """
    cfg = config or load_segment_config()
    sc, rfm = cfg["scoring"], cfg["rfm"]
    today = today or date.today()
    now = pd.Timestamp(datetime.combine(today, datetime.max.time()))
    today_ts = pd.Timestamp(today)

    cust = _read(conn, "customers", "*", customer_ids).set_index("customer_id")
    if cust.empty:
        return cust
    f = pd.DataFrame(index=cust.index)

    # ---- profile -----------------------------------------------------------
    f["name"] = cust["name"]
    f["first_name"] = cust["name"].str.split().str[0]
    for col in ("gender", "age", "city", "city_tier", "region", "device", "preferred_language",
                "favourite_categories", "membership_tier", "active_hour", "telegram_chat_id"):
        f[col] = cust[col]
    f["marketing_opt_in"] = cust["marketing_opt_in"].astype(bool)
    signup = pd.to_datetime(cust["signup_date"])
    f["days_since_signup"] = (today_ts - signup).dt.days
    f["days_to_birthday"] = [_days_to_birthday(date.fromisoformat(b), today) for b in cust["birth_date"]]

    # ---- orders ------------------------------------------------------------
    o = _read(conn, "orders", "customer_id, order_ts, category, gross_amount, discount_amount, "
                              "net_amount, coupon_code, returned", customer_ids)
    o["order_ts"] = pd.to_datetime(o["order_ts"])
    o = o[o["order_ts"] <= now].sort_values(["customer_id", "order_ts"])
    o["day"] = o["order_ts"].dt.normalize()
    o["kept_net"] = o["net_amount"].where(o["returned"] == 0, 0.0)
    o["disc_pct"] = o["discount_amount"] / o["gross_amount"]
    o["gap"] = o.groupby("customer_id")["day"].diff().dt.days
    last365 = o["order_ts"] > now - pd.Timedelta(days=365)
    last90 = o["order_ts"] > now - pd.Timedelta(days=90)

    g = o.groupby("customer_id")
    f["n_orders"] = g.size()
    f["first_order"] = g["order_ts"].min()
    f["last_order"] = g["order_ts"].max()
    f["last_order_category"] = g["category"].last()
    f["total_spend"] = g["kept_net"].sum()
    f["aov"] = g["net_amount"].mean()
    f["coupon_share"] = g["coupon_code"].apply(lambda s: s.notna().mean())
    f["avg_discount_pct"] = g["disc_pct"].mean()
    f["return_rate"] = g["returned"].mean()
    f["typical_gap"] = g["gap"].median()
    f["orders_365d"] = o[last365].groupby("customer_id").size()
    f["spend_365d"] = o[last365].groupby("customer_id")["kept_net"].sum()
    f["orders_90d"] = o[last90].groupby("customer_id").size()
    # Customers with no orders leave these columns empty; force numbers before filling gaps.
    numeric = ["n_orders", "total_spend", "aov", "coupon_share", "avg_discount_pct", "return_rate",
               "typical_gap", "orders_365d", "spend_365d", "orders_90d"]
    f[numeric] = f[numeric].apply(pd.to_numeric, errors="coerce")
    for col in ("n_orders", "total_spend", "orders_365d", "spend_365d", "orders_90d"):
        f[col] = f[col].fillna(0)
    f["n_orders"] = f["n_orders"].astype(int)
    f["days_since_last_order"] = (today_ts - f["last_order"].dt.normalize()).dt.days
    # Fall back to a default gap when there is only one order to learn from.
    gap_used = f["typical_gap"].clip(lower=1).fillna(sc["default_gap_days"])
    f["gap_ratio"] = f["days_since_last_order"] / gap_used

    # ---- sessions ----------------------------------------------------------
    s = _read(conn, "sessions", "customer_id, ts, categories_viewed", customer_ids)
    s["ts"] = pd.to_datetime(s["ts"])
    s = s[s["ts"] <= now]
    sg = s.groupby("customer_id")
    f["sessions_30d"] = s[s["ts"] > now - pd.Timedelta(days=30)].groupby("customer_id").size()
    f["sessions_30d"] = f["sessions_30d"].fillna(0).astype(int)
    f["days_since_last_session"] = (today_ts - sg["ts"].max().dt.normalize()).dt.days
    after = s.merge(f["last_order"].rename("lo").reset_index(), on="customer_id", how="left")
    after = after[after["lo"].isna() | (after["ts"] > after["lo"])]
    f["sessions_since_last_order"] = after.groupby("customer_id").size()
    f["sessions_since_last_order"] = f["sessions_since_last_order"].fillna(0).astype(int)
    recent_s = s[s["ts"] > now - pd.Timedelta(days=30)]
    f["recent_categories_viewed"] = recent_s.groupby("customer_id")["categories_viewed"].apply(
        lambda v: ",".join(pd.Series(",".join(v).split(",")).value_counts().index[:3]))

    # ---- carts -------------------------------------------------------------
    c = _read(conn, "cart_events", "customer_id, product_name, category, price, added_ts, status", customer_ids)
    c["added_ts"] = pd.to_datetime(c["added_ts"])
    c = c[c["added_ts"] <= now]
    c90 = c[c["added_ts"] > now - pd.Timedelta(days=90)]
    f["cart_abandon_rate_90d"] = c90.groupby("customer_id")["status"].apply(lambda st: (st != "purchased").mean())
    open_c = c[(c["status"] == "abandoned") & (c["added_ts"] > now - pd.Timedelta(days=sc["open_cart_days"]))]
    open_c = open_c.sort_values("added_ts", ascending=False)
    og = open_c.groupby("customer_id")
    f["open_cart_count"] = pd.to_numeric(og.size().reindex(f.index)).fillna(0).astype(int)
    f["open_cart_value"] = pd.to_numeric(og["price"].sum().reindex(f.index)).fillna(0.0)
    f["open_cart_items"] = og["product_name"].apply(lambda v: "; ".join(v.head(3)))
    f["open_cart_category"] = og["category"].first()
    f["cart_abandon_rate_90d"] = pd.to_numeric(f["cart_abandon_rate_90d"], errors="coerce")

    # ---- points ------------------------------------------------------------
    p = _read(conn, "reward_points", "*", customer_ids).set_index("customer_id")
    f["points_balance"] = pd.to_numeric(p["balance"].reindex(f.index)).fillna(0).astype(int)
    f["points_expiring"] = pd.to_numeric(p["points_expiring"].reindex(f.index)).fillna(0).astype(int)
    expiry = pd.to_datetime(p["expiry_date"].reindex(f.index))
    f["days_to_points_expiry"] = (expiry - today_ts).dt.days

    # ---- scores ------------------------------------------------------------
    f["r_score"] = _rfm_score(f["days_since_last_order"], rfm["recency_days"], higher_is_better=False)
    f["f_score"] = _rfm_score(f["orders_365d"], rfm["frequency_orders_365d"], higher_is_better=True)
    f["m_score"] = _rfm_score(f["spend_365d"], rfm["monetary_spend_365d"], higher_is_better=True)
    f["rfm"] = f["r_score"].astype(str) + f["f_score"].astype(str) + f["m_score"].astype(str)
    f["customer_value"] = f["spend_365d"]

    coupon_part = f["coupon_share"].fillna(0.5)
    cart_part = f["cart_abandon_rate_90d"].fillna(0.5)
    f["price_sensitivity"] = (0.65 * coupon_part + 0.35 * cart_part).clip(0, 1).round(3)

    still_browsing = f["days_since_last_session"].le(14) & (f["sessions_since_last_order"] > 0)
    f["still_browsing"] = still_browsing
    risk = 1 / (1 + np.exp(-sc["churn_steepness"] * (f["gap_ratio"] - sc["churn_midpoint_ratio"])))
    risk = risk.where(~still_browsing, risk * sc["still_browsing_factor"])
    f["churn_risk"] = risk.fillna(0.5).round(3)  # no orders yet: unknown, call it 50/50

    base = 1 - np.exp(-7 / gap_used)
    buy_anyway = (base * (1 - f["churn_risk"])).where(f["n_orders"] > 0, 0.05)
    f["buy_anyway"] = buy_anyway.round(3)
    return f


def _days_to_birthday(birth: date, today: date) -> int:
    """Days until the next birthday (0 = today). Feb 29 birthdays move to Feb 28."""
    for year in (today.year, today.year + 1):
        try:
            nxt = birth.replace(year=year)
        except ValueError:
            nxt = date(year, 2, 28)
        if nxt >= today:
            return (nxt - today).days
    return 365  # pragma: no cover


def customer_features(conn: sqlite3.Connection, customer_id: str, today: date | None = None) -> dict:
    """Features for one customer as a plain dict (what Node 1/3 use). Empty dict if unknown."""
    df = compute_features(conn, today, [customer_id])
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    row["customer_id"] = customer_id
    return {k: (None if _is_missing(v) else v) for k, v in row.items()}


def _is_missing(v) -> bool:
    return v is None or (not isinstance(v, str) and bool(pd.isna(v)))
