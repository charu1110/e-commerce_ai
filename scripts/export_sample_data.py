"""Export a small, readable testing dataset as CSV files (for evaluators).

    python scripts/export_sample_data.py

Writes sample_data/*.csv for 300 random customers (all their orders, browsing, carts, points),
plus the hidden personas in a separate file that the engine never reads.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "offers.db"
OUT = ROOT / "sample_data"
N, SEED = 300, 42


def main() -> None:
    if not SOURCE.exists():
        sys.exit("Run `python -m src.generate_data --customers 5000 --seed 42` first.")
    conn = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    ids = pd.read_sql_query("SELECT customer_id FROM customers", conn).sample(N, random_state=SEED)["customer_id"]
    marks = ",".join("?" * len(ids))
    OUT.mkdir(exist_ok=True)
    for name in ["customers", "orders", "sessions", "cart_events", "reward_points", "customer_truth"]:
        df = pd.read_sql_query(f"SELECT * FROM {name} WHERE customer_id IN ({marks})", conn, params=list(ids))
        if name == "customers":
            df = df.drop(columns=["telegram_chat_id"])
        df.to_csv(OUT / f"{name}.csv", index=False)
        print(f"{name:<15} {len(df):>7,} rows")
    pd.read_sql_query("SELECT * FROM categories", conn).to_csv(OUT / "categories.csv", index=False)


if __name__ == "__main__":
    main()
