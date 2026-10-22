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

## Tests

```
pytest -q
```
