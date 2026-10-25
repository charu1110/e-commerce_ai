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
(plain Python, no AI) against 15 rules before it is sent: offer type allowed for the segment, discount
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
| `/why` | Why you got (or didn't get) your last offer, in plain words |
| `/history` | Your last 5 offers and what you did with them |
| `/timetravel N` | Demo: jump N days ahead to see offers expire and the follow-up ladder kick in |
| `/stop` / `/resume` | Opt out of / back into offers |
| `/whoami` | Shows your chat id (put it in `ADMIN_CHAT_IDS` for admin commands) |
| `/help` | All commands |

**Admin commands** (only for chat ids listed in `ADMIN_CHAT_IDS` in `.env`; get yours with `/whoami`):

| Command | What it does |
|---|---|
| `/admin` | Today's stats: offers sent, claim rate, budget used, how often the AI passed the rules, with Pause / Resume buttons |
| `/audit C00123` | One customer's full decision trail: triggers, offers, codes, costs, blocked AI proposals |
| `/pause` / `/unpause` | Kill switch: stop or restart all offers |

Offer cards have **Claim 🎁**, **Not now** and **Why this offer?** buttons; every tap is logged.
If you see SSL certificate errors, make sure `truststore` from `requirements.txt` is installed.

## Follow-ups (escalation ladder)

If a customer ignores an offer (it expires without a tap), the next one follows the ladder in
`config/rules.yaml`: free delivery after 3 days, then 10% off, then 15% off, then stop. Tapping
"Not now" is respected (no follow-up). Every follow-up still passes all the rules. The bot runs the
scheduler every 10 minutes: it marks expired offers, sends offers that were held back by quiet hours,
and pushes follow-ups. Run one round by hand with `python -m src.scheduler` (add `--write` to act).

Demo in Telegram: `/myoffer`, don't tap anything, `/timetravel 6`, `/myoffer` again.

## Campaign simulator (does it pay?)

```
python -m src.simulator --days 14
```

Runs the real engine day by day for 14 days on a copy of the database, and lets each customer react
according to their hidden persona (which the engine never sees). 10% of customers are held back as a
control group, so we can measure what the offers really change:

- **uplift** = share of customers with offers who bought - share of the holdout who bought
- **ROI** = extra margin from the extra orders - discount money spent

Example run (2,000 customers, 14 days): 46.3% of customers with offers bought vs 23.7% of the holdout
(+22.6 points, 365 extra orders). Rs 87,195 spent on discounts (Rs 23,469 of it on people who would
have bought anyway), Rs 2,98,300 extra margin, ROI +Rs 2,11,105. VIPs showed no uplift: they buy
anyway, which is why the rules give them points and early access, not discounts.

## Dashboard

```
streamlit run dashboard/app.py
```

Pick the data source (simulation or live bot) in the sidebar.

- **Results**: uplift vs holdout, money spent vs gained, uplift by segment, funnel (decisions -> eligible
  -> sent -> claimed), why customers got no offer, which rules blocked the AI, fallback rate, offers by type.
- **Audit log**: every decision with its reason; open one to see all 15 rule checks.
- **Customer explorer**: a customer's segment, scores, monthly spend and decisions.
- **Rules & calendar**: the current limits, segment rules, follow-up ladder and upcoming festivals.

## Red-team tests

`tests/test_redteam.py` attacks the system on purpose and checks every attack is blocked: 90% off,
offers that weren't on the AI's menu, text that lies about the numbers ("fifty percent", "half price",
"₹5k", fullwidth or Devanagari digits, invented dates), hidden zero-width characters, made-up coupon
codes, cashback or free-iPhone claims, other customers' names, links, broken JSON, and customers whose
*name* is a prompt-injection attack. Safe messages in English, Hinglish and Punjabi must still pass.

## Tests

```
pytest -q
```
