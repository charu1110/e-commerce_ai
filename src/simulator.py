"""Campaign simulator: does the engine actually change behaviour, and does it pay?

Runs the real 10-node pipeline day by day on a COPY of the database
(data/simulation.db), then lets every customer react according to their hidden
persona (customer_truth — the only module besides the generator that reads it):

- each day a customer may buy anyway (their own base rate, no offer needed);
- an offer adds a lift that depends on how generous it is, the customer's true price
  sensitivity and their persona (a lapsed regular who still browses responds well,
  a long-gone hibernator rarely does, a VIP barely cares about money off);
- unclaimed offers expire and count as ignored, so the escalation ladder kicks in.

The holdout group never gets offers, so:
    uplift = conversion(treated) - conversion(holdout)
    ROI    = extra margin from incremental orders - discount money spent

    python -m src.simulator --days 14
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src import db, scheduler
from src.engine.pipeline import run_batch
from src.engine.state import Deps
from src.features import compute_features

SIM_DB = db.DB_PATH.parent / "simulation.db"
RESULTS = db.DB_PATH.parent / "simulation_results.json"

# How strongly each persona reacts to an offer (hidden truth; the engine never sees this).
PERSONA_RESPONSE = {"lapsing": 1.2, "hibernating": 0.4, "deal_hunter": 1.5, "cart_abandoner": 1.3,
                    "window_shopper": 0.8, "new": 1.0, "loyal": 0.6, "vip": 0.5}
NON_DISCOUNT_VALUE = {"free_delivery": 0.05, "early_access": 0.06}  # felt value as a share of the basket


@dataclass
class SimResult:
    days: int
    start: str
    customers: int
    funnel: dict = field(default_factory=dict)
    treated: int = 0
    holdout: int = 0
    conv_treated: float = 0.0
    conv_holdout: float = 0.0
    uplift_pp: float = 0.0
    incremental_orders: float = 0.0
    discount_cost: float = 0.0
    wasted_cost: float = 0.0            # discounts claimed by people who would have bought anyway
    extra_margin: float = 0.0
    roi: float = 0.0
    by_segment: list = field(default_factory=list)
    by_offer_type: list = field(default_factory=list)
    blocked_by: dict = field(default_factory=dict)
    fallback_rate: float = 0.0
    daily: list = field(default_factory=list)


def offer_lift(offer_type: str, value: float, basket: float, persona: str, price_sensitivity: float,
               still_browsing: bool) -> float:
    """Extra chance of buying because of this offer (hidden-truth response model)."""
    if offer_type == "percent_off":
        felt = value / 100
    elif offer_type == "flat_off":
        felt = value / max(basket, 1)
    elif offer_type == "bonus_points":
        felt = value * 0.25 / max(basket, 1)
    else:
        felt = NON_DISCOUNT_VALUE.get(offer_type, 0.03)
    response = PERSONA_RESPONSE.get(persona, 1.0)
    if persona == "lapsing" and not still_browsing:
        response = 0.5  # gone for real: much harder to win back
    return float(np.clip(response * (0.05 + 2.5 * felt) * (0.5 + price_sensitivity), 0, 0.85))


def actual_discount(offer_type: str, value: float, basket: float, rules) -> float:
    """Money the redeemed offer really costs on this basket."""
    if offer_type == "percent_off":
        return min(basket * value / 100, rules.global_.max_flat_discount_inr)
    if offer_type == "flat_off":
        return value
    if offer_type == "free_delivery":
        return rules.costs.delivery_fee_inr
    if offer_type == "bonus_points":
        return value * rules.costs.point_value_inr
    return rules.costs.early_access_cost_inr


def run_simulation(days: int = 14, seed: int = 7, source: Path | str = db.DB_PATH, target: Path | str = SIM_DB,
                   start: date | None = None, sample: int | None = None, progress=print) -> SimResult:
    rng = np.random.default_rng(seed)
    target = Path(target)
    shutil.copy(source, target)
    conn = db.connect(target)
    conn.execute("DELETE FROM offer_events")   # events first: they point at decisions
    conn.execute("DELETE FROM decision_log")
    conn.execute("UPDATE customers SET telegram_chat_id = NULL")
    db.set_setting(conn, "paused", "0")
    conn.commit()

    truth = pd.read_sql_query("SELECT * FROM customer_truth", conn).set_index("customer_id")
    margins = {r["category"]: r["margin_pct"] for r in conn.execute("SELECT category, margin_pct FROM categories")}
    ids = list(truth.index)
    if sample:
        ids = sorted(rng.choice(ids, size=min(sample, len(ids)), replace=False).tolist())
    start = start or date.today()
    deps = Deps.load(conn, channel="simulation")
    p_daily = (1 - (1 - truth["base_buy_prob"].clip(0, 0.99)) ** (1 / 7)).to_dict()

    group: dict[str, str] = {}                  # first eligible outcome: treated / holdout
    first_day: dict[str, int] = {}
    bought_after: dict[str, bool] = defaultdict(bool)
    pending: dict[int, list] = defaultdict(list)  # day -> purchases to record
    redeemed = []                               # (customer, decision row, discount, would_have_bought, margin)
    order_seq = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    daily = []

    for d in range(days):
        now = datetime.combine(start + timedelta(days=d), time(10, 0))
        scheduler.expire_offers(conn, now)

        # 1) purchases planned for today (redemptions and organic buys) become real orders
        for cid, category, basket, discount, decision_id in pending.pop(d, []):
            order_seq += 1
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,1,?,?,?,?,'UPI',0,'')",
                (f"S{order_seq:08d}", cid, now.isoformat(sep=" "), category, basket + discount, discount, basket,
                 None if decision_id is None else "OFFER"))
            if decision_id is not None:
                conn.execute("UPDATE decision_log SET status = 'claimed' WHERE decision_id = ?", (decision_id,))
                conn.execute("INSERT INTO offer_events (decision_id, customer_id, ts, event) VALUES (?,?,?,'claimed')",
                             (decision_id, cid, now.isoformat(sep=" ")))
            if cid in first_day and d >= first_day[cid]:
                bought_after[cid] = True
        conn.commit()

        # 2) the engine decides (features recomputed so today's purchases count)
        deps.features = compute_features(conn, now.date(), ids)
        deps._spent_today = {}
        states = run_batch(ids, deps, now)
        sent_today = 0
        planned_today = {c for day_list in pending.values() for c, *_ in day_list}

        # 3) customers react
        for s in states:
            cid = s.customer_id
            t = truth.loc[cid]
            if s.status in ("sent", "queued") or s.is_holdout:
                if cid not in group:
                    group[cid] = "holdout" if s.is_holdout else "treated"
                    first_day[cid] = d
            if cid in planned_today:
                continue
            basket = float(s.features.get("aov") or 800)
            fav = (s.features.get("favourite_categories") or "fashion").split(",")[0]
            if s.status in ("sent", "queued") and s.final_offer is not None:
                sent_today += 1
                p = s.final_offer
                window = p.expiry_days
                p_anyway = 1 - (1 - p_daily[cid]) ** window
                lift = offer_lift(p.offer_type, p.value, basket, t["persona"], t["price_sensitivity"],
                                  bool(t["still_browsing"]))
                if rng.random() < 1 - (1 - p_anyway) * (1 - lift):
                    cat = p.category or fav
                    disc = actual_discount(p.offer_type, p.value, basket, deps.rules)
                    when = d + 1 + int(rng.integers(0, window))  # buys on one of the next days
                    pending[when].append((cid, cat, basket, disc if p.offer_type in ("percent_off", "flat_off") else 0.0,
                                          s.decision_id))
                    # would they have bought anyway? (used to measure wasted discount money)
                    if when < days:  # only purchases inside the simulated period count (cost and sale)
                        redeemed.append((cid, s.segment, p.offer_type, disc, rng.random() < p_anyway / max(
                            1 - (1 - p_anyway) * (1 - lift), 1e-9), basket * margins.get(cat, 0.1)))
            elif rng.random() < p_daily[cid]:
                pending[d + 1].append((cid, fav, basket, 0.0, None))
        daily.append({"day": d + 1, "date": now.date().isoformat(), "offers": sent_today,
                      "claims_so_far": len(redeemed)})
        progress(f"day {d + 1}/{days}: {sent_today} offers sent, {len(redeemed)} redeemed so far")

    result = _summarise(conn, truth, margins, group, bought_after, redeemed, days, start, len(ids), daily)
    conn.close()
    return result


def _summarise(conn, truth, margins, group, bought_after, redeemed, days, start, n, daily) -> SimResult:
    r = SimResult(days=days, start=start.isoformat(), customers=n, daily=daily)
    log = pd.read_sql_query("SELECT * FROM decision_log", conn)
    r.funnel = {
        "decisions": int(len(log)),
        "had_trigger": int((log["trigger"].fillna("none") != "none").sum()),
        "eligible": int((log["eligible"] == 1).sum() + log["is_holdout"].fillna(0).sum()),
        "offers_sent": int(log["status"].isin(["sent", "queued", "claimed", "ignored", "expired", "dismissed"]).sum()),
        "claimed": int((log["status"] == "claimed").sum()),
    }
    ai = log[log["ai_proposal_json"].notna()]
    blocked = defaultdict(int)
    for v in ai["violations"].dropna():
        for item in json.loads(v):
            blocked[item.split(":", 1)[0]] += 1
    r.blocked_by = dict(sorted(blocked.items(), key=lambda kv: -kv[1]))
    offers = log[log["final_offer_id"].notna()]
    r.fallback_rate = float((offers["final_offer_id"] != "AI").mean()) if len(offers) else 0.0

    g = pd.Series(group, name="group")
    outcome = pd.Series({c: bought_after.get(c, False) for c in g.index}, name="bought")
    df = pd.concat([g, outcome], axis=1).join(truth["persona"])
    seg_first = (log.sort_values("decision_id").groupby("customer_id")["segment"].first())
    df = df.join(seg_first.rename("segment"))
    treated, hold = df[df["group"] == "treated"], df[df["group"] == "holdout"]
    r.treated, r.holdout = len(treated), len(hold)
    r.conv_treated = float(treated["bought"].mean()) if len(treated) else 0.0
    r.conv_holdout = float(hold["bought"].mean()) if len(hold) else 0.0
    r.uplift_pp = (r.conv_treated - r.conv_holdout) * 100
    r.incremental_orders = (r.conv_treated - r.conv_holdout) * r.treated

    red = pd.DataFrame(redeemed, columns=["customer_id", "segment", "offer_type", "cost", "anyway", "margin"])
    r.discount_cost = float(red["cost"].sum()) if len(red) else 0.0
    r.wasted_cost = float(red.loc[red["anyway"], "cost"].sum()) if len(red) else 0.0
    avg_margin = float(red["margin"].mean()) if len(red) else 0.0
    r.extra_margin = max(r.incremental_orders, 0) * avg_margin
    r.roi = r.extra_margin - r.discount_cost

    rows = []
    for seg, part in df.groupby("segment"):
        t_, h_ = part[part["group"] == "treated"], part[part["group"] == "holdout"]
        seg_red = red[red["segment"] == seg]
        ct = float(t_["bought"].mean()) if len(t_) else 0.0
        ch = float(h_["bought"].mean()) if len(h_) else float("nan")
        inc = (ct - ch) * len(t_) if len(h_) else float("nan")
        margin = (max(inc, 0) * float(seg_red["margin"].mean())) if len(seg_red) and not math.isnan(inc) else 0.0
        rows.append({"segment": seg, "treated": len(t_), "holdout": len(h_), "conv_treated": round(ct, 3),
                     "conv_holdout": None if math.isnan(ch) else round(ch, 3),
                     "uplift_pp": None if math.isnan(ch) else round((ct - ch) * 100, 1),
                     "discount_cost": round(float(seg_red["cost"].sum()), 0), "extra_margin": round(margin, 0)})
    r.by_segment = rows
    if len(red):
        bt = red.groupby("offer_type").agg(redeemed=("cost", "size"), cost=("cost", "sum")).reset_index()
        sent = offers.groupby("offer_type").size().rename("sent").reset_index()
        bt = sent.merge(bt, on="offer_type", how="left").fillna(0)
        bt["claim_rate"] = (bt["redeemed"] / bt["sent"]).round(3)
        r.by_offer_type = bt.to_dict("records")
    return r


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a campaign with a holdout group.")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--sample", type=int, default=2000,
                        help="simulate N random customers (default 2000, about 4 minutes; 0 = everyone)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    r = run_simulation(args.days, args.seed, sample=args.sample or None)
    RESULTS.write_text(json.dumps(asdict(r), indent=1, default=float), encoding="utf-8")
    print(f"\nSimulated {r.days} days for {r.customers:,} customers (copy: {SIM_DB.name})")
    print(f"Funnel: {r.funnel}")
    print(f"Treated {r.treated:,}: {r.conv_treated:.1%} bought | Holdout {r.holdout:,}: {r.conv_holdout:.1%} bought")
    print(f"Uplift: {r.uplift_pp:+.1f} percentage points -> {r.incremental_orders:,.0f} extra orders")
    print(f"Discount spent: Rs {r.discount_cost:,.0f} (of which Rs {r.wasted_cost:,.0f} went to people who'd buy anyway)")
    print(f"Extra margin: Rs {r.extra_margin:,.0f} | ROI: Rs {r.roi:,.0f}")
    print(f"Fallback rate: {r.fallback_rate:.0%} | Blocked by: {r.blocked_by}")
    print(pd.DataFrame(r.by_segment).to_string(index=False))
    print(f"Saved {RESULTS}")


if __name__ == "__main__":
    main()
