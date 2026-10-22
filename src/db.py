"""SQLite access: one database file (data/offers.db) holds customers and the audit trail.

Hidden ground truth (persona, true price sensitivity, ...) lives in its own table
`customer_truth`. Only the data generator and the campaign simulator read it —
the engine and the AI agent never do, so offers are decided from observable behaviour only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "offers.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    category      TEXT PRIMARY KEY,
    avg_price     REAL NOT NULL,
    margin_pct    REAL NOT NULL,   -- 0.50 = 50% gross margin
    return_rate   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    gender               TEXT NOT NULL,
    age                  INTEGER NOT NULL,
    birth_date           TEXT NOT NULL,
    city                 TEXT NOT NULL,
    city_tier            INTEGER NOT NULL,
    region               TEXT NOT NULL,      -- punjab / metro
    signup_date          TEXT NOT NULL,
    acquisition_channel  TEXT NOT NULL,
    device               TEXT NOT NULL,
    preferred_language   TEXT NOT NULL,
    favourite_categories TEXT NOT NULL,      -- comma separated
    membership_tier      TEXT NOT NULL,
    marketing_opt_in     INTEGER NOT NULL,
    active_hour          INTEGER NOT NULL,
    telegram_chat_id     INTEGER UNIQUE
);

-- Hidden ground truth: NEVER shown to the engine or the AI. Simulator only.
CREATE TABLE IF NOT EXISTS customer_truth (
    customer_id       TEXT PRIMARY KEY REFERENCES customers(customer_id),
    persona           TEXT NOT NULL,
    price_sensitivity REAL NOT NULL,
    typical_gap_days  REAL NOT NULL,
    base_buy_prob     REAL NOT NULL,   -- chance of buying in the next 7 days with no offer
    churned_on        TEXT,            -- last order before going quiet (lapsing / hibernating)
    still_browsing    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    order_ts        TEXT NOT NULL,
    category        TEXT NOT NULL,
    n_items         INTEGER NOT NULL,
    gross_amount    REAL NOT NULL,
    discount_amount REAL NOT NULL,
    net_amount      REAL NOT NULL,
    coupon_code     TEXT,
    payment_method  TEXT NOT NULL,
    returned        INTEGER NOT NULL,
    delivery_city   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    customer_id       TEXT NOT NULL REFERENCES customers(customer_id),
    ts                TEXT NOT NULL,
    device            TEXT NOT NULL,
    categories_viewed TEXT NOT NULL,   -- comma separated
    product_views     INTEGER NOT NULL,
    searched_terms    TEXT,
    added_to_cart     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_events (
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    price        REAL NOT NULL,
    added_ts     TEXT NOT NULL,
    status       TEXT NOT NULL      -- purchased / abandoned / removed
);

CREATE TABLE IF NOT EXISTS reward_points (
    customer_id     TEXT PRIMARY KEY REFERENCES customers(customer_id),
    balance         INTEGER NOT NULL,
    lifetime_earned INTEGER NOT NULL,
    points_expiring INTEGER NOT NULL,
    expiry_date     TEXT
);

-- One row per pass through the 10-node pipeline (audit trail).
CREATE TABLE IF NOT EXISTS decision_log (
    decision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id      TEXT NOT NULL REFERENCES customers(customer_id),
    ts               TEXT NOT NULL,
    trigger          TEXT,
    segment          TEXT,
    churn_risk       REAL,
    eligible         INTEGER,
    skip_reason      TEXT,
    ai_proposal_json TEXT,
    validator_result TEXT,
    violations       TEXT,
    fallback_used    INTEGER,
    final_offer_id   TEXT,
    message_text     TEXT,
    coupon_code      TEXT,
    expected_cost    REAL,
    status           TEXT,   -- sent / claimed / dismissed / ignored / expired / skipped / holdout
    is_holdout       INTEGER
);

-- What happened to each sent offer (button clicks, expiry).
CREATE TABLE IF NOT EXISTS offer_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decision_log(decision_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL     -- sent / claimed / dismissed / why / expired
);

-- Runtime switches set by admins (e.g. paused = kill switch).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer   ON orders(customer_id, order_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_cart_customer     ON cart_events(customer_id, added_ts);
CREATE INDEX IF NOT EXISTS idx_decision_customer ON decision_log(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_offer_events_dec  ON offer_events(decision_id);
"""


# Columns added after the first release; added in place so existing databases keep their data.
EXTRA_COLUMNS = {
    "decision_log": {
        "trigger_detail": "TEXT",
        "segment_reason": "TEXT",
        "ai_source": "TEXT",          # stub / claude
        "offer_type": "TEXT",
        "offer_value": "REAL",
        "offer_category": "TEXT",
        "headline": "TEXT",
        "expires_on": "TEXT",
        "send_at": "TEXT",
        "channel": "TEXT",            # telegram / simulation / cli
    },
}


def connect(path: Path | str = DB_PATH, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the database with dict-like rows and foreign keys on, creating tables if needed."""
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, cols in EXTRA_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def is_paused(conn: sqlite3.Connection) -> bool:
    """Kill switch: when on, no offers go out to anyone."""
    return get_setting(conn, "paused", "0") == "1"


def reset(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Delete and recreate the database file (used by the data generator)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    return connect(path)
