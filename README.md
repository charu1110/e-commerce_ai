# Personalised Offer Engine

Decides which offer (if any) each customer of a synthetic e-commerce store should get, and when,
and delivers it through one Telegram bot. Rules set the limits, the AI personalises within them,
and deterministic code checks every offer before it is sent. See `CLAUDE.md` for the full design.

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # then fill in your own keys
```

## Synthetic data

```
python -m src.generate_data --customers 5000 --seed 42
python scripts/validate_data.py
```

Generates 18 months of history for 5,000 customers across 8 hidden personas (~100k orders,
~430k sessions, ~300k cart events) into `data/offers.db`. `validate_data.py` prints the realism
checks (Pareto spend, Gamma purchase gaps, Diwali spike, weekend and payday bumps, cart abandonment,
return rates, COD by city tier, lapsing behaviour).

Festival dates and demand boosts are in `config/occasions.yaml`; edit them without touching code.

## Features and segments

```
python -m src.segments
```

Scores every customer from behaviour only (days since last order vs their usual gap, churn risk,
"would buy anyway" chance, price sensitivity, RFM) and assigns one of 7 segments: new, hibernating,
lapsing, vip, deal_hunter, loyal, occasional. Each segment comes with a plain-English reason.
Thresholds are in `config/segments.yaml`. The command also prints how well segments match the
hidden personas (80-100%).

## Rules and offers (marketing can edit these)

| File | What it controls |
|---|---|
| `config/rules.yaml` | Every limit: max discount, per-category and per-segment caps, daily budget, cooldown, holdout %, quiet hours, expiry range, banned phrases |
| `config/offers.yaml` | Pre-approved fallback offers and their message templates, per segment |
| `config/segments.yaml` | How customers are grouped (lapsed, VIP, loyal, ...) |
| `config/occasions.yaml` | Festival calendar |

Every offer, whether written by the AI or taken from `offers.yaml`, is checked by `src/guardrails.py`
(plain Python, no AI) against 14 rules before it is sent: offer type allowed for the segment, discount
caps, flat cap and minimum order, daily budget, customer eligibility (opt-out, holdout, cooldown, just
bought, kill switch, discount-addiction and return-abuse guards), expiry and stacking, profit after
discount, and message checks (length, banned phrases, numbers match the offer, no other customer's
data, no made-up claims, style). If a value in `rules.yaml` is mistyped or out of range, loading fails
with a clear error instead of silently ignoring it.

## Tests

```
pytest -q
```
