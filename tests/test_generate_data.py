"""Invariants the synthetic data must always respect (small dataset, fast)."""
from datetime import date

import pandas as pd
import pytest

from src import db
from src.generate_data import generate
from src.occasions import load_calendar
from tests.conftest import TODAY


def read(conn, sql):
    return pd.read_sql_query(sql, conn)


def test_row_counts_and_unique_ids(conn):
    customers = read(conn, "SELECT customer_id FROM customers")
    assert len(customers) == 600
    assert customers["customer_id"].is_unique
    assert len(read(conn, "SELECT * FROM customer_truth")) == 600
    assert read(conn, "SELECT order_id FROM orders")["order_id"].is_unique


def test_persona_shares_are_exact(conn):
    shares = read(conn, "SELECT persona FROM customer_truth")["persona"].value_counts(normalize=True)
    assert shares["lapsing"] == pytest.approx(0.12, abs=0.01)
    assert shares["vip"] == pytest.approx(0.05, abs=0.01)


def test_no_orders_in_the_future_and_amounts_add_up(conn):
    orders = read(conn, "SELECT * FROM orders")
    assert pd.to_datetime(orders["order_ts"]).dt.date.max() <= TODAY
    assert (orders["net_amount"] == orders["gross_amount"] - orders["discount_amount"]).all()
    assert (orders["discount_amount"] >= 0).all()
    assert (orders["net_amount"] > 0).all()


def test_lapsers_stop_hard_at_churn_date(conn):
    df = read(conn, """
        SELECT t.customer_id, t.churned_on, t.typical_gap_days, MAX(DATE(o.order_ts)) AS last_order
        FROM customer_truth t JOIN orders o USING (customer_id)
        WHERE t.persona = 'lapsing' GROUP BY t.customer_id""")
    assert not df.empty
    assert (df["last_order"] == df["churned_on"]).all()
    days_since = (pd.Timestamp(TODAY) - pd.to_datetime(df["last_order"])).dt.days
    assert (days_since / df["typical_gap_days"] > 2).all()


def test_new_customers_are_new(conn):
    df = read(conn, """
        SELECT c.signup_date, COUNT(o.order_id) AS n
        FROM customers c JOIN customer_truth t USING (customer_id)
        LEFT JOIN orders o USING (customer_id)
        WHERE t.persona = 'new' GROUP BY c.customer_id""")
    assert ((pd.Timestamp(TODAY) - pd.to_datetime(df["signup_date"])).dt.days < 30).all()
    assert (df["n"] <= 1).all()


def test_engine_tables_hide_ground_truth(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}
    assert not cols & {"persona", "price_sensitivity", "typical_gap_days", "base_buy_prob", "churned_on"}


def test_same_seed_same_data(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    generate(50, seed=1, today=TODAY, db_path=a)
    generate(50, seed=1, today=TODAY, db_path=b)
    q = "SELECT * FROM orders ORDER BY order_id"
    pd.testing.assert_frame_equal(read(db.connect(a), q), read(db.connect(b), q))


def test_calendar_loads_and_respects_regions():
    cal = load_calendar()
    assert cal.demand_multiplier(date(2025, 10, 18)) >= 2.0          # Dhanteras
    assert cal.demand_multiplier(date(2025, 7, 15)) == 1.0           # ordinary day
    assert any(o.key == "lohri" for o in cal.active(date(2026, 1, 13), "punjab"))
    assert not any(o.key == "lohri" for o in cal.active(date(2026, 1, 13), "metro"))
