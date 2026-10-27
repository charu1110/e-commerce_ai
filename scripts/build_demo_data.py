"""Build the small demo database that ships with the repo (for the hosted dashboard and evaluators).

The full databases are 100+ MB and git-ignored. This copies what the dashboard needs from the
simulation run (customers, orders, points, categories, every decision and click) and leaves out
the bulky browsing and cart history, so it fits comfortably on GitHub.

    python scripts/build_demo_data.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import db  # noqa: E402

SOURCE = ROOT / "data" / "simulation.db"
TARGET = ROOT / "demo" / "demo.db"
KEEP = ["categories", "customers", "orders", "reward_points", "decision_log", "offer_events", "settings"]


def main() -> None:
    if not SOURCE.exists():
        sys.exit("Run `python -m src.simulator --days 14` first.")
    TARGET.parent.mkdir(exist_ok=True)
    if TARGET.exists():
        TARGET.unlink()
    out = db.connect(TARGET)  # creates every table (empty), so the app's code finds them all
    out.execute("ATTACH DATABASE ? AS src", (str(SOURCE),))
    for table in KEEP:
        out.execute(f"DELETE FROM {table}")
        cols = [r[1] for r in out.execute(f"PRAGMA table_info({table})")]
        src_cols = {r[1] for r in out.execute(f"PRAGMA src.table_info({table})")}
        use = [c for c in cols if c in src_cols]
        out.execute(f"INSERT INTO {table} ({','.join(use)}) SELECT {','.join(use)} FROM src.{table}")
    out.commit()
    out.execute("DETACH DATABASE src")
    out.execute("VACUUM")
    out.close()
    results = ROOT / "data" / "simulation_results.json"
    if results.exists():
        shutil.copy(results, TARGET.parent / "simulation_results.json")
    print(f"Wrote {TARGET} ({TARGET.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
