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

## The offer pipeline

```
python -m src.engine.pipeline --customer C00001          # one customer, every step explained
python -m src.engine.pipeline --sample 500               # many customers, summary
python -m src.engine.pipeline --sample 300 --hour 23     # see quiet-hours queuing
```

Every decision goes through 10 steps: load customer, detect trigger (lapsed buyer, abandoned cart,
points expiring, birthday, festival, new customer, ...), segment and score, eligibility rules
(opt-out, cooldown, just bought, would buy anyway, budget, holdout), AI offer (currently a rule-based
stand-in), guardrail validator, fallback offer if needed, Telegram card with a single-use code, and
tracking. Each decision is written to `decision_log`. Runs are dry runs unless you add `--write`.
When the daily budget is limited, customers are served in priority order: most likely to churn,
most valuable, and least likely to buy anyway come first.

## The AI offer agent

Claude picks one offer from the options the rules allow and writes the message. Put your key in
`.env` as `ANTHROPIC_API_KEY` (model in `ANTHROPIC_MODEL`). The prompt is plain text in
`src/prompts/offer_agent.md`, so tone and guidance can be changed without code.

Claude's answer is never trusted on its own: every proposal goes through the guardrail validator,
and if Claude errors, times out (10 s, 1 retry), declines, or breaks a rule, a pre-approved offer
from `config/offers.yaml` is sent instead. Without a key, a rule-based agent writes the offers.

```
python -m src.engine.pipeline --customer C00001 --agent claude
```

## Telegram bot

1. Create a bot with @BotFather and put its token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Run it:

```
python -m src.bot
```

3. In Telegram, open the bot and send `/start`. Pick a shopper type (lapsing regular, VIP, deal
   hunter, ...) and you are linked to a synthetic customer of that type. Then:

| Command | What it does |
|---|---|
| `/myoffer` | Runs the full pipeline for you now and shows the offer card (or why there is none) |
| `/persona` | Become a different customer |
| `/stop` / `/resume` | Opt out of / back into offers |
| `/whoami` | Shows your chat id (put it in `ADMIN_CHAT_IDS` for admin commands) |
| `/help` | All commands |

Offer cards have **Claim 🎁**, **Not now** and **Why this offer?** buttons; every tap is logged.
If you see SSL certificate errors, make sure `truststore` from `requirements.txt` is installed.

## Tests

```
pytest -q
```
