"""Generate the synthetic e-commerce dataset behind Node 1 (Load customer).

Every customer gets a hidden *persona* (VIP, loyal, lapsing, ...) that drives all
their behaviour: how often they buy, how much, with coupons or not, how much they
browse, and whether they have stopped buying. The engine never sees the persona —
it must rediscover it from behaviour, exactly like a real store would.

Realism built in (checked by scripts/validate_data.py):
- gaps between orders ~ Gamma around each customer's typical gap
- Pareto-like spend (a few customers bring most revenue)
- festival spikes from config/occasions.yaml, weekend and salary-day bumps
- ~70% cart abandonment, category-specific return rates
- more cash-on-delivery in tier-2/3 cities, UPI dominant overall
- lapsing customers buy regularly, then stop hard; some keep browsing

Run:  python -m src.generate_data --customers 5000 --seed 42
"""
from __future__ import annotations

import argparse
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

from src import db
from src.occasions import Calendar, load_calendar

HISTORY_DAYS = 548  # ~18 months of history up to today
GAMMA_SHAPE = 4.0   # order gaps ~ Gamma(k=4): regular habit, but not clockwork (CV = 0.5)

# category: (avg_price_inr, margin_pct, return_rate, popularity)
CATEGORIES = {
    "electronics": (3500, 0.10, 0.065, 0.09),
    "mobiles": (15000, 0.08, 0.05, 0.03),
    "grocery": (650, 0.10, 0.01, 0.26),
    "home": (1500, 0.25, 0.08, 0.14),
    "beauty": (700, 0.38, 0.04, 0.15),
    "fashion": (1300, 0.50, 0.25, 0.23),
    "footwear": (1700, 0.40, 0.15, 0.10),
}
CAT_NAMES = list(CATEGORIES)
CAT_POPULARITY = np.array([v[3] for v in CATEGORIES.values()])

PRODUCTS = {
    "electronics": ["boAt Airdopes earbuds", "Bluetooth speaker", "Smartwatch", "Power bank 20000mAh",
                    "Mixer grinder", "LED desk lamp", "Wireless mouse", "Trimmer"],
    "mobiles": ["Redmi Note 14", "Samsung Galaxy M35", "iPhone 15", "OnePlus Nord CE4",
                "Realme Narzo 70", "Vivo T3"],
    "grocery": ["Basmati rice 5kg", "Aashirvaad atta 10kg", "Mustard oil 1L", "Desi ghee 1L",
                "Dry fruits gift box", "Masala chai 500g", "Haldiram namkeen combo"],
    "home": ["Cotton bedsheet set", "Non-stick cookware set", "Diya & decor lights", "Steel water bottles",
             "Wall clock", "Cushion covers (set of 5)", "Pressure cooker 5L"],
    "beauty": ["Vitamin C face serum", "Lakme lipstick", "Sunscreen SPF 50", "Hair oil 200ml",
               "Bridal makeup kit", "Perfume 100ml", "Mehendi cones pack"],
    "fashion": ["Cotton kurta", "Phulkari dupatta", "Denim jacket", "Anarkali suit",
                "Men's ethnic sherwani", "Printed saree", "Woollen shawl", "Graphic t-shirt"],
    "footwear": ["Punjabi jutti", "Running shoes", "Kolhapuri chappal", "Formal leather shoes",
                 "Sneakers", "Heeled sandals"],
}

SEARCH_TERMS = {
    "electronics": ["earbuds under 2000", "smartwatch", "power bank", "mixer grinder"],
    "mobiles": ["5g phone under 15000", "iphone 15", "best camera phone", "samsung m series"],
    "grocery": ["atta 10kg", "basmati rice", "dry fruits", "ghee"],
    "home": ["bedsheet double", "diwali lights", "cookware set", "home decor"],
    "beauty": ["face serum", "lipstick", "sunscreen", "bridal makeup"],
    "fashion": ["kurta for men", "phulkari dupatta", "anarkali suit", "winter jacket"],
    "footwear": ["punjabi jutti", "running shoes", "sneakers", "sandals for women"],
}

# city, tier, weight, region — Punjab-heavy because the bot users are in Punjab
CITIES = [
    ("Ludhiana", 2, 18, "punjab"), ("Amritsar", 2, 14, "punjab"), ("Jalandhar", 2, 11, "punjab"),
    ("Patiala", 3, 9, "punjab"), ("Bathinda", 3, 7, "punjab"), ("Mohali", 2, 8, "punjab"),
    ("Chandigarh", 2, 10, "punjab"), ("Delhi", 1, 12, "metro"), ("Mumbai", 1, 6, "metro"),
    ("Bengaluru", 1, 5, "metro"),
]
CITY_WEIGHTS = np.array([c[2] for c in CITIES], dtype=float) / sum(c[2] for c in CITIES)

PAYMENT_METHODS = ["UPI", "COD", "card", "wallet"]
PAYMENT_MIX = {1: [0.56, 0.12, 0.20, 0.12], 2: [0.52, 0.28, 0.10, 0.10], 3: [0.45, 0.40, 0.07, 0.08]}

# Shopping-hour profile: lunch-break bump and a big evening peak
HOUR_WEIGHTS = np.array([0.5, 0.2, 0.1, 0.1, 0.1, 0.2, 0.5, 1, 2, 3, 3.5, 4,
                         5, 5, 4, 3.5, 3.5, 4, 5, 6.5, 7, 6.5, 4.5, 2], dtype=float)
HOUR_WEIGHTS /= HOUR_WEIGHTS.sum()

PUNJABI_MALE = ["Gurpreet", "Harpreet", "Jaspreet", "Manpreet", "Amandeep", "Harjot", "Karan",
                "Arjun", "Simranjit", "Gagandeep", "Rajveer", "Sukhdeep", "Navdeep", "Ishaan"]
PUNJABI_FEMALE = ["Simran", "Harleen", "Jasleen", "Navneet", "Gurleen", "Manjot", "Amrit",
                  "Kirandeep", "Rupinder", "Sukhmani", "Ishita", "Parneet", "Mehak", "Tanvi"]
PUNJABI_SURNAMES = ["Singh", "Sandhu", "Gill", "Sidhu", "Dhillon", "Grewal", "Arora",
                    "Malhotra", "Bajwa", "Brar", "Sethi", "Chawla", "Kapoor", "Bedi"]

# Persona behaviour. gap = typical days between orders while active.
PERSONAS = {
    "vip":            dict(share=0.05, gap=(6, 12),   spend=2.6,  sens=(0.05, 0.25), coupon=0.05,
                           signup=(400, 900), browse=4,  cart_p=0.45),
    "loyal":          dict(share=0.20, gap=(10, 20),  spend=1.0,  sens=(0.20, 0.45), coupon=0.15,
                           signup=(300, 900), browse=3,  cart_p=0.55),
    "lapsing":        dict(share=0.12, gap=(8, 20),   spend=1.0,  sens=(0.30, 0.60), coupon=0.20,
                           signup=(300, 800), browse=3,  cart_p=0.55),
    "deal_hunter":    dict(share=0.15, gap=(15, 35),  spend=0.7,  sens=(0.70, 0.95), coupon=0.75,
                           signup=(120, 700), browse=5,  cart_p=0.65),
    "cart_abandoner": dict(share=0.10, gap=(30, 60),  spend=0.85, sens=(0.50, 0.80), coupon=0.30,
                           signup=(120, 700), browse=6,  cart_p=0.90),
    "window_shopper": dict(share=0.13, gap=(60, 150), spend=0.8,  sens=(0.40, 0.70), coupon=0.20,
                           signup=(90, 700),  browse=10, cart_p=0.35),
    "new":            dict(share=0.10, gap=(15, 40),  spend=0.9,  sens=(0.30, 0.70), coupon=0.30,
                           signup=(0, 29),    browse=6,  cart_p=0.55),
    "hibernating":    dict(share=0.15, gap=(20, 45),  spend=0.8,  sens=(0.30, 0.70), coupon=0.20,
                           signup=(450, 900), browse=2,  cart_p=0.45),
}


class DataGenerator:
    """Builds all tables in memory, then writes them to SQLite in one go."""

    def __init__(self, n_customers: int, seed: int, today: date, calendar: Calendar | None = None):
        self.n = n_customers
        self.rng = np.random.default_rng(seed)
        self.fake = Faker("en_IN")
        self.fake.seed_instance(seed)
        self.today = today
        self.start = today - timedelta(days=HISTORY_DAYS)
        self.calendar = calendar or load_calendar()
        self._boost_cache: dict[tuple[date, str], tuple[float, list[str]]] = {}
        self.rows = {k: [] for k in ("customers", "customer_truth", "orders", "sessions",
                                     "cart_events", "reward_points")}
        self._order_seq = 0
        self._session_seq = 0

    # ---------- helpers ----------
    def _season(self, day: date, region: str) -> tuple[float, list[str]]:
        """(demand multiplier, boosted categories) for this day and region, cached."""
        key = (day, region)
        if key not in self._boost_cache:
            self._boost_cache[key] = (self.calendar.demand_multiplier(day, region),
                                      sorted(self.calendar.boosted_categories(day, region)))
        return self._boost_cache[key]

    def _choice(self, options, p=None):
        return options[self.rng.choice(len(options), p=p)]

    def _hour_near(self, active_hour: int) -> int:
        return int(np.clip(round(self.rng.normal(active_hour, 2)), 0, 23))

    def _ts(self, day: date, hour: int) -> datetime:
        return datetime.combine(day, time(hour, int(self.rng.integers(0, 60))))

    # ---------- customers ----------
    def _persona_list(self) -> list[str]:
        """Exact persona counts by share, shuffled."""
        names = list(PERSONAS)
        counts = [int(round(PERSONAS[p]["share"] * self.n)) for p in names]
        counts[names.index("loyal")] += self.n - sum(counts)  # rounding remainder
        personas = [p for p, c in zip(names, counts) for _ in range(c)]
        self.rng.shuffle(personas)
        return personas

    def _make_profile(self, cid: str, persona: str) -> dict:
        cfg = PERSONAS[persona]
        rng = self.rng
        city, tier, _, region = CITIES[rng.choice(len(CITIES), p=CITY_WEIGHTS)]
        gender = "male" if rng.random() < 0.52 else "female"

        if region == "punjab" and rng.random() < 0.6:
            first = self._choice(PUNJABI_MALE if gender == "male" else PUNJABI_FEMALE)
            last = "Kaur" if gender == "female" and rng.random() < 0.4 else self._choice(PUNJABI_SURNAMES)
        else:
            first = self.fake.first_name_male() if gender == "male" else self.fake.first_name_female()
            last = self.fake.last_name()

        if persona == "vip":
            age = rng.triangular(26, 34, 52)
        elif rng.random() < 0.7:
            age = rng.triangular(22, 28, 40)  # core online shoppers
        else:
            age = rng.uniform(18, 65)
        age = int(np.clip(age, 18, 65))
        birth_date = self.today - timedelta(days=int(age * 365.25 + rng.integers(0, 365)))
        signup = self.today - timedelta(days=int(rng.integers(cfg["signup"][0], cfg["signup"][1] + 1)))

        favs = self._favourites(persona, gender)
        gap = float(rng.uniform(*cfg["gap"]))

        # When does this customer stop ordering?
        order_end = self.today
        if persona == "lapsing":
            # regular until 3-8 weeks ago, and long enough ago that the gap is > 2x usual
            quiet_days = int(rng.integers(max(21, math.ceil(2.2 * gap)), 57))
            order_end = self.today - timedelta(days=quiet_days)
        elif persona == "hibernating":
            order_end = self.today - timedelta(days=int(rng.integers(185, 381)))

        still_browsing = True
        if persona == "lapsing":
            still_browsing = rng.random() < 0.6   # still interested, just not buying
        elif persona == "hibernating":
            still_browsing = rng.random() < 0.1

        channel_p = {"deal_hunter": [0.20, 0.40, 0.30, 0.10],
                     "vip": [0.45, 0.20, 0.10, 0.25], "loyal": [0.45, 0.20, 0.10, 0.25]}
        device_p = [0.45, 0.45, 0.10] if persona == "vip" else [0.72, 0.15, 0.13]
        lang_p = [0.35, 0.40, 0.25] if region == "punjab" else [0.55, 0.43, 0.02]
        member_p = {"vip": [0.05, 0.25, 0.70], "loyal": [0.30, 0.50, 0.20],
                    "lapsing": [0.40, 0.45, 0.15], "new": [0.97, 0.03, 0.0]}
        opt_in_p = {"new": 0.95, "hibernating": 0.85}.get(persona, 0.90)

        return dict(
            customer_id=cid, persona=persona, cfg=cfg, name=f"{first} {last}", gender=gender,
            age=age, birth_date=birth_date, city=city, city_tier=tier, region=region,
            signup_date=signup,
            acquisition_channel=self._choice(["organic", "ads", "social", "referral"],
                                             channel_p.get(persona, [0.35, 0.30, 0.20, 0.15])),
            device=self._choice(["android", "ios", "web"], device_p),
            preferred_language=self._choice(["english", "hinglish", "punjabi"], lang_p),
            favourites=favs,
            membership_tier=self._choice(["none", "silver", "gold"], member_p.get(persona, [0.80, 0.17, 0.03])),
            marketing_opt_in=bool(rng.random() < opt_in_p),
            active_hour=int(rng.choice(24, p=HOUR_WEIGHTS)),
            typical_gap=gap,
            price_sensitivity=float(np.clip(rng.uniform(*cfg["sens"]) + rng.normal(0, 0.03), 0, 1)),
            spend_factor=float(rng.lognormal(0, 0.55) * cfg["spend"]),
            order_end=order_end,
            still_browsing=still_browsing,
        )

    def _favourites(self, persona: str, gender: str) -> list[str]:
        w = dict(zip(CAT_NAMES, CAT_POPULARITY.copy()))
        if persona == "vip":
            w["electronics"] *= 2.5; w["mobiles"] *= 3; w["fashion"] *= 1.3
        if persona == "deal_hunter":
            w["grocery"] *= 1.3; w["fashion"] *= 1.3
        if gender == "female":
            w["beauty"] *= 2.0; w["fashion"] *= 1.3
        else:
            w["electronics"] *= 1.3; w["footwear"] *= 1.2
        p = np.array(list(w.values()))
        k = int(self.rng.choice([1, 2, 3], p=[0.35, 0.45, 0.20]))
        idx = self.rng.choice(len(CAT_NAMES), size=k, replace=False, p=p / p.sum())
        return [CAT_NAMES[i] for i in idx]

    def _pick_category(self, prof: dict, boosted: list[str]) -> str:
        r = self.rng.random()
        if boosted and r < 0.4:
            return self._choice(boosted)
        if r < 0.75:
            return self._choice(prof["favourites"])
        return self._choice(CAT_NAMES, CAT_POPULARITY / CAT_POPULARITY.sum())

    # ---------- orders ----------
    def _order_dates(self, prof: dict) -> list[date]:
        """Purchase days: Gamma gaps around the typical gap, shortened during festivals."""
        rng = self.rng
        if prof["persona"] == "new":
            if rng.random() < 0.55:  # 0-1 orders since signing up
                days_since = (self.today - prof["signup_date"]).days
                return [prof["signup_date"] + timedelta(days=int(rng.integers(0, days_since + 1)))]
            return []

        first_day = max(self.start, prof["signup_date"] + timedelta(days=1))
        end = prof["order_end"]
        if first_day > end:
            return []
        gap = prof["typical_gap"]
        span = (end - first_day).days
        dates = []
        x = float(rng.uniform(0, gap))  # days after first_day
        while x <= span:
            day = first_day + timedelta(days=int(x))
            dates.append(self._nudge(day, first_day, end))
            boost, _ = self._season(day, prof["region"])
            x += max(1.0, rng.gamma(GAMMA_SHAPE, gap / GAMMA_SHAPE) / boost)
        if not dates and prof["persona"] in ("hibernating", "lapsing"):
            dates.append(first_day + timedelta(days=int(rng.integers(0, span + 1))))
        return sorted(dates)

    def _nudge(self, day: date, lo: date, hi: date) -> date:
        """People shift purchases to weekends and to payday (1st-5th of the month)."""
        rng = self.rng
        new = day
        wd = day.weekday()
        if wd in (3, 4) and rng.random() < 0.20:        # Thu/Fri -> Saturday
            new = day + timedelta(days=5 - wd)
        elif wd == 0 and rng.random() < 0.15:           # Monday -> Sunday
            new = day - timedelta(days=1)
        elif day.day >= 26 and rng.random() < 0.35:     # month end -> payday
            first_next = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
            new = first_next + timedelta(days=int(rng.integers(0, 5)))
        return new if lo <= new <= hi else day

    def _make_orders(self, prof: dict) -> list[dict]:
        rng = self.rng
        cfg = prof["cfg"]
        orders = []
        for i, day in enumerate(self._order_dates(prof)):
            _, boosted = self._season(day, prof["region"])
            cat = self._pick_category(prof, boosted)
            avg, _, return_rate, _ = CATEGORIES[cat]
            n_items = 1 + int(rng.poisson(2.5 if cat == "grocery" else 0.4))
            unit = avg * rng.lognormal(0, 0.4) * prof["spend_factor"]
            gross = round(max(99.0, unit * (1 + 0.6 * (n_items - 1))))

            festival = bool(boosted)
            first_new = i == 0 and prof["persona"] == "new"
            p_coupon = min(0.95, cfg["coupon"] * (1.4 if festival else 1.0))
            if first_new:
                p_coupon = max(p_coupon, 0.6)
            discount, code = 0, None
            if rng.random() < p_coupon:
                pct = int(rng.choice([5, 10, 15, 20, 25], p=[0.15, 0.35, 0.25, 0.18, 0.07]))
                discount = min(round(gross * pct / 100), 750 if festival else 500)
                prefix = "WELCOME" if first_new else ("FEST" if festival else "SAVE")
                code = f"{prefix}{pct}"

            mix = PAYMENT_MIX[prof["city_tier"]]
            if prof["persona"] == "vip":
                mix = [0.45, 0.05, 0.38, 0.12]
            ts = self._ts(day, self._hour_near(prof["active_hour"]))
            self._order_seq += 1
            orders.append(dict(
                order_id=f"O{self._order_seq:07d}", customer_id=prof["customer_id"],
                order_ts=ts, category=cat, n_items=n_items, gross_amount=float(gross),
                discount_amount=float(discount), net_amount=float(gross - discount),
                coupon_code=code, payment_method=self._choice(PAYMENT_METHODS, mix),
                returned=bool(rng.random() < return_rate * (1.3 if prof["persona"] == "deal_hunter" else 1.0)),
                delivery_city=prof["city"] if rng.random() < 0.95 else self._choice(CITIES)[0],
                _unit_price=float(round(unit)),
            ))
        return orders

    # ---------- sessions & carts ----------
    def _add_session(self, prof, ts, cats, views, term, cart) -> None:
        self._session_seq += 1
        self.rows["sessions"].append(dict(
            session_id=f"S{self._session_seq:07d}", customer_id=prof["customer_id"], ts=ts,
            device=prof["device"] if self.rng.random() < 0.85 else self._choice(["android", "ios", "web"]),
            categories_viewed=",".join(cats), product_views=views, searched_terms=term,
            added_to_cart=cart,
        ))

    def _add_cart(self, prof, cat, price, ts, status) -> None:
        self.rows["cart_events"].append(dict(
            customer_id=prof["customer_id"], product_name=self._choice(PRODUCTS[cat]),
            category=cat, price=float(price), added_ts=ts, status=status,
        ))

    def _make_sessions(self, prof: dict, orders: list[dict]) -> None:
        rng = self.rng
        cfg = prof["cfg"]

        # 1) Browsing that leads to each order: 1-2 sessions, the last one adds to cart.
        for o in orders:
            n_pre = 1 + int(rng.random() < 0.5)
            for k in range(n_pre):
                last = k == n_pre - 1
                days_before = 0 if last else int(rng.integers(1, 4))
                ts = o["order_ts"] - timedelta(days=days_before, minutes=int(rng.integers(5, 90)))
                cats = [o["category"]] + ([self._choice(prof["favourites"])] if rng.random() < 0.3 else [])
                term = self._choice(SEARCH_TERMS[o["category"]]) if rng.random() < 0.5 else None
                self._add_session(prof, ts, sorted(set(cats)), 2 + int(rng.poisson(4)), term, last)
                if last:
                    self._add_cart(prof, o["category"], o["_unit_price"], ts, "purchased")

        # 2) Browsing without buying. Lapsers who are "gone" stop browsing at their last order.
        b_start = max(self.start, prof["signup_date"])
        if prof["still_browsing"]:
            b_end = self.today
        else:
            b_end = orders[-1]["order_ts"].date() if orders else prof["order_end"]
        span = (b_end - b_start).days
        if span <= 0:
            return
        n = int(rng.poisson(cfg["browse"] * span / 30.4))
        view_mean = 7 if prof["persona"] == "window_shopper" else 3
        for d in sorted(rng.integers(0, span + 1, size=n)):
            day = b_start + timedelta(days=int(d))
            ts = self._ts(day, self._hour_near(prof["active_hour"]))
            k = 1 + int(rng.poisson(0.5))
            cats = sorted({self._pick_category(prof, []) for _ in range(k)})
            term = self._choice(SEARCH_TERMS[cats[0]]) if rng.random() < 0.45 else None
            cart = bool(rng.random() < cfg["cart_p"])
            self._add_session(prof, ts, cats, 1 + int(rng.poisson(view_mean)), term, cart)
            if cart:
                for _ in range(1 + int(rng.random() < 0.5)):
                    cat = self._choice(cats)
                    price = round(CATEGORIES[cat][0] * rng.lognormal(0, 0.4) * prof["spend_factor"])
                    status = "abandoned" if rng.random() < 0.8 else "removed"
                    self._add_cart(prof, cat, price, ts + timedelta(minutes=int(rng.integers(1, 20))), status)

    # ---------- points ----------
    def _make_points(self, prof: dict, orders: list[dict]) -> None:
        """1 point per Rs 10 spent on kept orders; some redeemed; some expiring soon."""
        rng = self.rng
        earned = int(sum(o["net_amount"] for o in orders if not o["returned"]) // 10)
        if prof["membership_tier"] == "gold":
            earned = int(earned * 1.5)
        redeem_share = rng.uniform(0.4, 0.8) if prof["persona"] == "deal_hunter" else rng.uniform(0.1, 0.6)
        balance = int(earned * (1 - redeem_share))
        expiring, expiry = 0, None
        if balance > 0 and rng.random() < 0.35:
            expiring = int(balance * rng.uniform(0.2, 0.6))
            expiry = self.today + timedelta(days=int(rng.integers(3, 46)))
        self.rows["reward_points"].append(dict(
            customer_id=prof["customer_id"], balance=balance, lifetime_earned=earned,
            points_expiring=expiring, expiry_date=expiry,
        ))

    # ---------- truth ----------
    def _base_buy_prob(self, prof: dict, n_orders: int) -> float:
        """Chance of buying in the next 7 days with NO offer (used only by the simulator)."""
        persona = prof["persona"]
        if persona == "lapsing":
            p = 0.08 if prof["still_browsing"] else 0.03
        elif persona == "hibernating":
            p = 0.01
        elif persona == "new" and n_orders == 0:
            p = 0.10
        else:
            p = 1 - math.exp(-7 / prof["typical_gap"])
        return round(float(np.clip(p * self.rng.uniform(0.8, 1.2), 0.002, 0.95)), 3)

    # ---------- main ----------
    def run(self) -> dict[str, pd.DataFrame]:
        for i, persona in enumerate(self._persona_list(), start=1):
            prof = self._make_profile(f"C{i:05d}", persona)
            orders = self._make_orders(prof)
            self._make_sessions(prof, orders)
            self._make_points(prof, orders)

            churned_on = None
            if persona in ("lapsing", "hibernating") and orders:
                churned_on = orders[-1]["order_ts"].date()  # the hard stop

            self.rows["customers"].append(dict(
                customer_id=prof["customer_id"], name=prof["name"], gender=prof["gender"],
                age=prof["age"], birth_date=prof["birth_date"], city=prof["city"],
                city_tier=prof["city_tier"], region=prof["region"], signup_date=prof["signup_date"],
                acquisition_channel=prof["acquisition_channel"], device=prof["device"],
                preferred_language=prof["preferred_language"],
                favourite_categories=",".join(prof["favourites"]),
                membership_tier=prof["membership_tier"], marketing_opt_in=prof["marketing_opt_in"],
                active_hour=prof["active_hour"], telegram_chat_id=None,
            ))
            self.rows["customer_truth"].append(dict(
                customer_id=prof["customer_id"], persona=persona,
                price_sensitivity=round(prof["price_sensitivity"], 3),
                typical_gap_days=round(prof["typical_gap"], 1),
                base_buy_prob=self._base_buy_prob(prof, len(orders)),
                churned_on=churned_on, still_browsing=prof["still_browsing"],
            ))
            for o in orders:
                o.pop("_unit_price")
                self.rows["orders"].append(o)

        frames = {name: pd.DataFrame(rows) for name, rows in self.rows.items()}
        frames["categories"] = pd.DataFrame(
            [dict(category=c, avg_price=v[0], margin_pct=v[1], return_rate=v[2]) for c, v in CATEGORIES.items()]
        )
        return frames


def _to_sql_value(series: pd.Series) -> pd.Series:
    """SQLite stores dates/timestamps as ISO text and booleans as 0/1."""
    if series.dtype == bool:
        return series.astype(int)
    sample = series.dropna()
    if not sample.empty and isinstance(sample.iloc[0], (date, datetime, pd.Timestamp)):
        return series.map(lambda v: v.isoformat(sep=" ") if isinstance(v, datetime)
                          else (v.isoformat() if v is not None and not pd.isna(v) else None))
    return series


def generate(n_customers: int = 5000, seed: int = 42, today: date | None = None,
             db_path: Path | str = db.DB_PATH) -> dict[str, int]:
    """Generate everything and write a fresh database. Returns row counts per table."""
    today = today or date.today()
    frames = DataGenerator(n_customers, seed, today).run()
    conn = db.reset(db_path)
    try:
        order = ["categories", "customers", "customer_truth", "orders", "sessions",
                 "cart_events", "reward_points"]
        for name in order:
            df = frames[name].apply(_to_sql_value)
            df.to_sql(name, conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()
    return {name: len(frames[name]) for name in frames}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic e-commerce data.")
    parser.add_argument("--customers", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="pretend today is this date (YYYY-MM-DD); default: real today")
    parser.add_argument("--db", default=str(db.DB_PATH))
    args = parser.parse_args()

    counts = generate(args.customers, args.seed, args.today, args.db)
    print(f"Wrote {args.db}")
    for name, n in counts.items():
        print(f"  {name:<15} {n:>8,} rows")


if __name__ == "__main__":
    main()
