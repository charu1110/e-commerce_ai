"""Features and segments.

Part 1 builds tiny hand-made customers to pin exact behaviour and edge cases.
Part 2 checks, on generated data, that segments recover the hidden personas
without the engine ever reading them.
"""
import shutil
from datetime import date, timedelta

import pandas as pd
import pytest

from src import db
from src.features import _days_to_birthday, compute_features, customer_features
from src.segments import assign_segment, segment_all
from tests.conftest import TODAY


# ---------------------------------------------------------------- helpers
@pytest.fixture
def mini(tmp_path):
    conn = db.connect(tmp_path / "mini.db")
    yield conn
    conn.close()


def add_customer(conn, cid, signup_days_ago=400, birth="1995-10-25"):
    conn.execute(
        """INSERT INTO customers (customer_id, name, gender, age, birth_date, city, city_tier, region,
           signup_date, acquisition_channel, device, preferred_language, favourite_categories,
           membership_tier, marketing_opt_in, active_hour)
           VALUES (?, 'Test Singh', 'male', 30, ?, 'Ludhiana', 2, 'punjab', ?, 'organic', 'android',
                   'english', 'fashion', 'none', 1, 19)""",
        (cid, birth, (TODAY - timedelta(days=signup_days_ago)).isoformat()))


def add_orders(conn, cid, days_ago, net=1000.0, coupon=None, category="fashion"):
    for d in days_ago:
        ts = f"{TODAY - timedelta(days=d)} 19:00:00"
        conn.execute(
            """INSERT INTO orders VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 'UPI', 0, 'Ludhiana')""",
            (f"{cid}-{d}", cid, ts, category, net + (100 if coupon else 0),
             100 if coupon else 0, net, coupon))


def segment_of(conn, cid, today=TODAY):
    f = customer_features(conn, cid, today)
    seg, reason = assign_segment(f)
    return seg, reason, f


# ---------------------------------------------------------------- part 1: exact cases
def test_lapsed_regular_is_lapsing_with_high_churn(mini):
    add_customer(mini, "L1")
    add_orders(mini, "L1", range(200, 39, -10))       # every 10 days, stopped 40 days ago
    seg, reason, f = segment_of(mini, "L1")
    assert f["typical_gap"] == 10
    assert f["days_since_last_order"] == 40
    assert f["gap_ratio"] == pytest.approx(4.0)
    assert seg == "lapsing"
    assert "4.0x" in reason
    assert f["churn_risk"] > 0.9
    assert f["buy_anyway"] < 0.05


def test_steady_regular_is_loyal_and_would_buy_anyway(mini):
    add_customer(mini, "R1")
    add_orders(mini, "R1", range(355, 4, -14))        # every 2 weeks, last 5 days ago
    seg, _, f = segment_of(mini, "R1")
    assert seg == "loyal"
    assert f["churn_risk"] < 0.2
    assert f["buy_anyway"] > 0.25


def test_big_frequent_spender_is_vip(mini):
    add_customer(mini, "V1")
    add_orders(mini, "V1", range(360, 1, -7), net=5000.0)
    seg, _, f = segment_of(mini, "V1")
    assert seg == "vip"
    assert f["m_score"] == 5 and f["f_score"] == 5 and f["r_score"] == 5


def test_coupon_user_is_deal_hunter_and_price_sensitive(mini):
    add_customer(mini, "D1")
    add_orders(mini, "D1", range(160, 9, -30), coupon="SAVE10")
    seg, _, f = segment_of(mini, "D1")
    assert seg == "deal_hunter"
    assert f["coupon_share"] == 1.0
    assert f["price_sensitivity"] >= 0.65


def test_long_silent_customer_is_hibernating(mini):
    add_customer(mini, "H1", signup_days_ago=500)
    add_orders(mini, "H1", range(400, 199, -20))
    seg, _, f = segment_of(mini, "H1")
    assert seg == "hibernating"
    assert f["churn_risk"] > 0.95


def test_brand_new_customer_without_orders(mini):
    add_customer(mini, "N1", signup_days_ago=5)
    seg, _, f = segment_of(mini, "N1")
    assert seg == "new"
    assert f["n_orders"] == 0
    assert f["last_order"] is None and f["gap_ratio"] is None
    assert f["churn_risk"] == 0.5


def test_single_order_uses_default_gap_and_is_occasional(mini):
    add_customer(mini, "S1", signup_days_ago=100)
    add_orders(mini, "S1", [45])
    seg, _, f = segment_of(mini, "S1")
    assert f["typical_gap"] is None
    assert f["gap_ratio"] == pytest.approx(45 / 30)
    assert seg == "occasional"


def test_old_signup_never_ordered_is_occasional(mini):
    add_customer(mini, "Z1", signup_days_ago=100)
    seg, reason, _ = segment_of(mini, "Z1")
    assert seg == "occasional"
    assert "never ordered" in reason


def test_time_travel_ignores_future_data(mini):
    add_customer(mini, "L1")
    add_orders(mini, "L1", range(200, 39, -10))
    seg_then, _, f_then = segment_of(mini, "L1", today=TODAY - timedelta(days=38))
    assert f_then["days_since_last_order"] == 2
    assert seg_then != "lapsing"


def test_open_cart_counts_only_recent_abandoned_items(mini):
    add_customer(mini, "C1")
    add_orders(mini, "C1", [60, 30])
    for days, status, name in [(2, "abandoned", "Punjabi jutti"), (20, "abandoned", "Old item"),
                               (1, "removed", "Removed item")]:
        mini.execute("INSERT INTO cart_events VALUES ('C1', ?, 'footwear', 1200, ?, ?)",
                     (name, f"{TODAY - timedelta(days=days)} 12:00:00", status))
    f = customer_features(mini, "C1", TODAY)
    assert f["open_cart_count"] == 1
    assert f["open_cart_items"] == "Punjabi jutti"
    assert f["open_cart_category"] == "footwear"


def test_points_and_birthday(mini):
    add_customer(mini, "P1", birth="1995-10-25")
    mini.execute("INSERT INTO reward_points VALUES ('P1', 800, 2000, 300, ?)",
                 ((TODAY + timedelta(days=10)).isoformat(),))
    f = customer_features(mini, "P1", TODAY)
    assert f["points_expiring"] == 300 and f["days_to_points_expiry"] == 10
    assert f["days_to_birthday"] == 3
    assert _days_to_birthday(date(2000, 2, 29), date(2027, 3, 1)) == 365  # next: 28 Feb 2028
    assert _days_to_birthday(date(2000, 10, 22), TODAY) == 0


def test_unknown_customer_returns_empty(mini):
    assert customer_features(mini, "NOPE", TODAY) == {}


# ---------------------------------------------------------------- part 2: generated data
@pytest.fixture(scope="module")
def segmented(conn):
    seg = segment_all(compute_features(conn, TODAY))
    truth = pd.read_sql_query("SELECT customer_id, persona, price_sensitivity AS true_ps FROM customer_truth",
                              conn).set_index("customer_id")
    return seg.join(truth)


@pytest.mark.parametrize("persona,segment,min_share", [
    ("lapsing", "lapsing", 0.90), ("hibernating", "hibernating", 0.95), ("new", "new", 0.99),
    ("vip", "vip", 0.75), ("deal_hunter", "deal_hunter", 0.85), ("loyal", "loyal", 0.70),
    ("window_shopper", "occasional", 0.70), ("cart_abandoner", "occasional", 0.60),
])
def test_segments_recover_hidden_personas(segmented, persona, segment, min_share):
    rows = segmented[segmented["persona"] == persona]
    assert (rows["segment"] == segment).mean() >= min_share


def test_scores_make_business_sense(segmented):
    by = segmented.groupby("segment")
    assert by["churn_risk"].mean()["lapsing"] > 0.8
    assert by["churn_risk"].mean()["loyal"] < 0.3
    assert by["buy_anyway"].mean()["vip"] > by["buy_anyway"].mean()["lapsing"]
    corr = segmented["price_sensitivity"].rank().corr(segmented["true_ps"].rank())  # Spearman
    assert corr > 0.5


def test_features_do_not_depend_on_hidden_truth(gen_db_path, tmp_path):
    """Wiping customer_truth must not change a single feature."""
    copy = tmp_path / "no_truth.db"
    shutil.copy(gen_db_path, copy)
    c = db.connect(copy)
    c.execute("DELETE FROM customer_truth")
    c.commit()
    original = compute_features(db.connect(gen_db_path), TODAY)
    pd.testing.assert_frame_equal(original, compute_features(c, TODAY))


def test_rupees_use_indian_grouping():
    from src.segments import _inr
    assert _inr(817139) == "Rs 8,17,139"
    assert _inr(150000) == "Rs 1,50,000"
    assert _inr(999) == "Rs 999"
    assert _inr(12345678) == "Rs 1,23,45,678"
