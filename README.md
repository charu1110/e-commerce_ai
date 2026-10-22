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

## Tests

```
pytest -q
```
