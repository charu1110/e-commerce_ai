"""Marketing dashboard (Streamlit).

    streamlit run dashboard/app.py

Pages: Results (KPIs, holdout uplift, money, funnel, blocked rules), Audit log,
Customer explorer, Rules & calendar. Reads the live bot database or the
simulation copy; never writes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import reporting  # noqa: E402
from src.db import DB_PATH  # noqa: E402
from src.features import customer_features  # noqa: E402
from src.formatting import indian_number  # noqa: E402
from src.guardrails import CHECK_LABELS  # noqa: E402
from src.occasions import load_calendar  # noqa: E402
from src.segments import assign_segment  # noqa: E402
from src.simulator import RESULTS, SIM_DB  # noqa: E402

# Validated palette (dataviz reference): blue / orange categorical, blue<->red diverging.
BLUE, ORANGE, RED, MUTED = "#2a78d6", "#eb6834", "#e34948", "#898781"
FUNNEL_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]  # ordinal, light -> dark

st.set_page_config(page_title="Offer Engine", page_icon="🎁", layout="wide")


def inr(x: float) -> str:
    return "₹" + indian_number(x)


def ink() -> str:
    """Secondary text ink for chart labels: text never wears the series colour, and must suit the theme."""
    theme = getattr(getattr(st.context, "theme", None), "type", None)
    return "#c3c2b7" if theme == "dark" else "#52514e"


def labels_for(chart, field: str, fmt: str):
    """Value labels in text ink (not the bar colour), placed just past the bar end."""
    return chart.mark_text(align="left", dx=4, color=ink()).encode(
        text=alt.Text(f"{field}:Q", format=fmt), color=alt.value(ink()))


@st.cache_resource
def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=30)
def load_log(path: str) -> pd.DataFrame:
    return reporting.decisions(connect(path))


def bar(df: pd.DataFrame, x: str, y: str, color: str = BLUE, fmt: str = ",.0f", sort="-x", height=None):
    """Horizontal bar with value labels and a hover tooltip."""
    if sort == "-x":
        sort = list(df.sort_values(x, ascending=False)[y])
    base = alt.Chart(df).encode(y=alt.Y(f"{y}:N", sort=sort, title=None, axis=alt.Axis(labelLimit=300)),
                                x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(grid=False, labels=False, ticks=False)),
                                tooltip=[alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q", format=fmt)])
    bars = base.mark_bar(color=color, cornerRadiusEnd=4, size=18)
    return (bars + labels_for(base, x, fmt)).properties(height=height or max(60, 32 * len(df))).configure_view(stroke=None)


# ------------------------------------------------------------------ sidebar
st.sidebar.title("🎁 Offer Engine")
sources = {"Simulation (campaign with holdout)": str(SIM_DB), "Live (Telegram bot)": str(DB_PATH)}
available = {k: v for k, v in sources.items() if Path(v).exists()}
if not available:
    st.error("No database found. Run `python -m src.generate_data` first.")
    st.stop()
source = st.sidebar.radio("Data", list(available))
db_path = available[source]
page = st.sidebar.radio("Page", ["Results", "Audit log", "Customer explorer", "Rules & calendar"])
st.sidebar.caption("Simulation: `python -m src.simulator --days 14`\n\nRules live in `config/*.yaml`.")
log = load_log(db_path)
is_sim = db_path == str(SIM_DB)


# ================================================================== Results
def results_page():
    st.title("Results")
    s = reporting.summary(log)
    res = reporting.load_results(RESULTS) if is_sim else None

    if res:
        st.caption(f"{res['days']}-day simulated campaign from {res['start']} on {res['customers']:,} customers. "
                   "Customers react according to hidden personas the engine never sees.")
        c = st.columns(5)
        c[0].metric("Uplift vs holdout", f"{res['uplift_pp']:+.1f} pts",
                    help="Conversion of customers who got offers minus conversion of the holdout group.")
        c[1].metric("Extra orders", f"{res['incremental_orders']:,.0f}")
        c[2].metric("Discount spent", inr(res["discount_cost"]),
                    help=f"{inr(res['wasted_cost'])} of it went to people who would have bought anyway.")
        c[3].metric("Extra margin", inr(res["extra_margin"]))
        c[4].metric("ROI", inr(res["roi"]), help="Extra margin minus discount spent.")
    else:
        c = st.columns(5)
        c[0].metric("Decisions", f"{s['decisions']:,}")
        c[1].metric("Offers sent", f"{s['offers']:,}")
        c[2].metric("Claim rate", f"{s['claim_rate']:.0%}")
        c[3].metric("Budget committed", inr(s["committed_inr"]))
        c[4].metric("Fallback rate", f"{s['fallback_rate']:.0%}",
                    help="Share of offers that came from the pre-approved list instead of the AI.")
        if is_sim:
            st.info("Run `python -m src.simulator --days 14` to see uplift and ROI.")
        else:
            st.caption("Live data from the Telegram bot. Uplift and ROI need purchase outcomes: see the Simulation.")

    if res:
        left, right = st.columns(2)
        with left:
            st.subheader("Did offers change behaviour?")
            conv = pd.DataFrame({"group": ["Got offers", "Holdout (no offers)"],
                                 "bought": [res["conv_treated"], res["conv_holdout"]],
                                 "n": [res["treated"], res["holdout"]]})
            chart = alt.Chart(conv).encode(
                y=alt.Y("group:N", title=None, sort=None, axis=alt.Axis(labelLimit=240)),
                x=alt.X("bought:Q", title="Share who bought", axis=alt.Axis(format="%", grid=False)),
                color=alt.Color("group:N", scale=alt.Scale(range=[BLUE, ORANGE]), legend=None),
                tooltip=["group", alt.Tooltip("bought:Q", format=".1%"), alt.Tooltip("n:Q", title="customers")])
            labels = labels_for(chart, "bought", ".1%")
            st.altair_chart((chart.mark_bar(cornerRadiusEnd=4, size=26) + labels).properties(height=130)
                            .configure_view(stroke=None), use_container_width=True)
            st.caption(f"{res['treated']:,} customers got offers, {res['holdout']:,} were held back on purpose.")
        with right:
            st.subheader("Money: spent vs gained")
            items = ["Extra margin earned", "Discount spent", "Of that: spent on sure buyers"]
            money = pd.DataFrame({"item": items,
                                  "inr": [res["extra_margin"], res["discount_cost"], res["wasted_cost"]],
                                  "kind": ["gain", "cost", "cost"]})
            chart = alt.Chart(money).encode(
                y=alt.Y("item:N", sort=items, title=None, axis=alt.Axis(labelLimit=240)),
                x=alt.X("inr:Q", title=None, axis=alt.Axis(grid=False, format=",.0f")),
                color=alt.condition(alt.datum.kind == "gain", alt.value(BLUE), alt.value(ORANGE)),
                tooltip=["item", alt.Tooltip("inr:Q", format=",.0f", title="₹")])
            labels = labels_for(chart, "inr", ",.0f")
            st.altair_chart((chart.mark_bar(cornerRadiusEnd=4, size=16) + labels).properties(height=150)
                            .configure_view(stroke=None), use_container_width=True)

        st.subheader("Uplift by customer segment")
        seg = pd.DataFrame(res["by_segment"]).dropna(subset=["uplift_pp"])
        if not seg.empty:
            seg["direction"] = seg["uplift_pp"].map(lambda v: "positive" if v >= 0 else "negative")
            order = list(seg.sort_values("uplift_pp", ascending=False)["segment"])
            chart = alt.Chart(seg).encode(
                y=alt.Y("segment:N", sort=order, title=None),
                x=alt.X("uplift_pp:Q", title="Uplift (percentage points)", axis=alt.Axis(grid=False)),
                color=alt.Color("direction:N", scale=alt.Scale(domain=["positive", "negative"], range=[BLUE, RED]),
                                legend=None),
                tooltip=["segment", alt.Tooltip("uplift_pp:Q", title="uplift (pts)"), "treated", "holdout",
                         alt.Tooltip("discount_cost:Q", format=",.0f"), alt.Tooltip("extra_margin:Q", format=",.0f")])
            st.altair_chart((chart.mark_bar(cornerRadiusEnd=4, size=18) + labels_for(chart, "uplift_pp", "+.1f"))
                            .properties(height=32 * len(seg))
                            .configure_view(stroke=None), use_container_width=True)
            table = pd.DataFrame(res["by_segment"]).rename(columns={
                "conv_treated": "bought (offers)", "conv_holdout": "bought (holdout)", "uplift_pp": "uplift (pts)",
                "discount_cost": "discount ₹", "extra_margin": "extra margin ₹"})
            st.dataframe(table, hide_index=True, use_container_width=True)
            st.caption("Small holdout groups are noisy: read segments with few holdout customers with care.")

    left, right = st.columns(2)
    with left:
        st.subheader("Funnel")
        f = reporting.funnel(log)
        if not f.empty:
            chart = alt.Chart(f).encode(
                y=alt.Y("stage:N", sort=None, title=None, axis=alt.Axis(labelLimit=300)),
                x=alt.X("count:Q", title=None, axis=alt.Axis(grid=False, labels=False, ticks=False)),
                color=alt.Color("stage:N", scale=alt.Scale(domain=list(f["stage"]), range=FUNNEL_RAMP), legend=None),
                tooltip=["stage", alt.Tooltip("count:Q", format=",")])
            labels = labels_for(chart, "count", ",")
            st.altair_chart((chart.mark_bar(cornerRadiusEnd=4, size=22) + labels).properties(height=190)
                            .configure_view(stroke=None), use_container_width=True)
        else:
            st.caption("No decisions yet. Use the bot (/myoffer) or run the simulator.")
    with right:
        st.subheader("Why customers got no offer")
        sk = reporting.skip_reasons(log)
        if not sk.empty:
            st.altair_chart(bar(sk, "customers", "reason", color=MUTED), use_container_width=True)
        else:
            st.caption("Nobody has been skipped yet.")

    left, right = st.columns(2)
    with left:
        st.subheader("AI proposals blocked by rule")
        b = reporting.blocked_by_rule(log)
        if b.empty:
            st.caption("No AI proposal has been blocked yet.")
        else:
            st.altair_chart(bar(b, "times", "rule", color=ORANGE), use_container_width=True)
        st.caption(f"Fallback rate: {s['fallback_rate']:.0%} of offers came from the pre-approved list.")
    with right:
        st.subheader("Offers by type")
        t = reporting.offers_by(log, "offer_type")
        if not t.empty:
            st.dataframe(t.rename(columns={"offer_type": "offer", "claim_rate": "claim rate"}), hide_index=True,
                         use_container_width=True)
        else:
            st.caption("No offers sent yet.")


# ================================================================== Audit log
def audit_page():
    st.title("Audit log")
    st.caption("Every decision the engine made, with the reason. One row per pipeline run.")
    if log.empty:
        st.info("No decisions yet.")
        return
    c1, c2, c3 = st.columns([2, 2, 1])
    statuses = c1.multiselect("Status", sorted(log["status"].dropna().unique()))
    cid = c2.text_input("Customer id", placeholder="C00123").strip().upper()
    limit = c3.selectbox("Rows", [100, 500, 2000], index=0)
    view = log
    if statuses:
        view = view[view["status"].isin(statuses)]
    if cid:
        view = view[view["customer_id"] == cid]
    view = view.sort_values("decision_id", ascending=False).head(limit)
    cols = ["decision_id", "ts", "customer_id", "trigger", "segment", "status", "offer_type", "offer_value",
            "final_offer_id", "coupon_code", "expected_cost", "skip_reason"]
    st.dataframe(view[cols], hide_index=True, use_container_width=True, height=360)

    ids = view["decision_id"].tolist()
    if ids:
        pick = st.selectbox("Open a decision", ids)
        row = log[log["decision_id"] == pick].iloc[0]
        st.markdown(f"**#{pick} · {row['customer_id']} · {row['status']}** · trigger: {row['trigger']} "
                    f"({row['trigger_detail'] or ''})")
        if row["message_text"]:
            st.info(row["message_text"])
        if row["validator_result"]:
            checks = pd.DataFrame(json.loads(row["validator_result"])["checks"])
            checks["check"] = checks["name"].map(lambda n: CHECK_LABELS.get(n, n))
            checks["result"] = checks["passed"].map({True: "✅ pass", False: "❌ fail"})
            st.dataframe(checks[["check", "result", "reason"]], hide_index=True, use_container_width=True)
        violations = json.loads(row["violations"] or "[]")
        if violations:
            st.warning("AI proposal blocked:\n\n" + "\n".join(f"- {v}" for v in violations))
        if row["skip_reason"]:
            st.caption(f"Reason: {row['skip_reason']}")


# ================================================================== Customer explorer
def customer_page():
    st.title("Customer explorer")
    conn = connect(db_path)
    cid = st.text_input("Customer id", value="C00001").strip().upper()
    f = customer_features(conn, cid, date.today())
    if not f:
        st.warning("No such customer.")
        return
    seg, reason = assign_segment(f)
    st.subheader(f"{f['name']} · {f['city']} · {seg}")
    st.caption(reason)
    c = st.columns(5)
    c[0].metric("Orders", f["n_orders"])
    c[1].metric("Days since last order", "-" if f["days_since_last_order"] is None else int(f["days_since_last_order"]))
    c[2].metric("Churn risk", f"{f['churn_risk']:.0%}")
    c[3].metric("Would buy anyway", f"{f['buy_anyway']:.0%}")
    c[4].metric("Spend last 12 months", inr(f["spend_365d"]))
    orders = pd.read_sql_query("SELECT order_ts, category, net_amount FROM orders WHERE customer_id = ?", conn,
                               params=(cid,))
    if not orders.empty:
        orders["month"] = pd.to_datetime(orders["order_ts"]).dt.to_period("M").dt.to_timestamp()
        monthly = orders.groupby("month")["net_amount"].sum().reset_index()
        st.subheader("Spend per month")
        chart = alt.Chart(monthly).mark_bar(color=BLUE, cornerRadiusEnd=4).encode(
            x=alt.X("yearmonth(month):O", title=None, axis=alt.Axis(format="%b %y", labelAngle=0)),
            y=alt.Y("net_amount:Q", title="₹", axis=alt.Axis(grid=True)),
            tooltip=[alt.Tooltip("month:T", format="%b %Y"), alt.Tooltip("net_amount:Q", format=",.0f", title="₹")])
        st.altair_chart(chart.properties(height=200).configure_view(stroke=None), use_container_width=True)
    st.subheader("Decisions for this customer")
    mine = log[log["customer_id"] == cid].sort_values("decision_id", ascending=False)
    if mine.empty:
        st.caption("No decisions yet.")
    else:
        st.dataframe(mine[["ts", "trigger", "status", "offer_type", "offer_value", "message_text", "skip_reason"]],
                     hide_index=True, use_container_width=True)


# ================================================================== Rules & calendar
def rules_page():
    st.title("Rules & calendar")
    st.caption("These limits come from `config/rules.yaml`. Every AI offer is checked against them before sending.")
    rules = yaml.safe_load((ROOT / "config/rules.yaml").read_text(encoding="utf-8"))
    g = rules["global"]
    c = st.columns(4)
    c[0].metric("Max discount", f"{g['max_discount_pct']}%")
    c[1].metric("Max flat discount", inr(g["max_flat_discount_inr"]))
    c[2].metric("Daily budget", inr(g["daily_budget_inr"]))
    c[3].metric("Cooldown", f"{g['cooldown_days']} days")
    left, right = st.columns(2)
    with left:
        st.subheader("Max discount by category")
        cats = pd.DataFrame(rules["category_max_discount_pct"].items(), columns=["category", "max %"])
        st.altair_chart(bar(cats, "max %", "category"), use_container_width=True)
    with right:
        st.subheader("What each segment may get")
        seg = pd.DataFrame([{"segment": k, "max discount %": v["max_discount_pct"],
                             "allowed offers": ", ".join(v["allowed_types"])}
                            for k, v in rules["segment_rules"].items()])
        st.dataframe(seg, hide_index=True, use_container_width=True)
        st.subheader("Follow-up ladder (if an offer is ignored)")
        st.dataframe(pd.DataFrame(rules["escalation_ladder"]), hide_index=True, use_container_width=True)
    st.subheader("Festival calendar")
    cal = load_calendar()
    rows = [{"occasion": o.name, "from": a, "to": b, "demand boost": f"{o.demand_boost:g}x",
             "budget x": f"{o.budget_multiplier:g}", "categories": ", ".join(o.boosted_categories),
             "regions": ", ".join(o.regions) or "all"}
            for o in cal.occasions for a, b in o.windows if b >= date.today()]
    st.dataframe(pd.DataFrame(rows).sort_values("from"), hide_index=True, use_container_width=True)


{"Results": results_page, "Audit log": audit_page, "Customer explorer": customer_page,
 "Rules & calendar": rules_page}[page]()
