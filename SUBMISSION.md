# Hackathon submission: Personalised Offer Engine

| # | Item | Link / location |
|---|---|---|
| 1 | **Deployed link** | Dashboard: `[Streamlit Cloud URL]` · Telegram bot: https://t.me/Offer_genrate_bot (answers while the bot runs) |
| 2 | **GitHub repository (public)** | https://github.com/charu1110/e-commerce_ai |
| 3 | **Testing dataset** | `sample_data/` (300 customers as CSV, see its README) and `demo/demo.db` (all 5,000 customers + the 14-day simulated campaign). Full data: `python -m src.generate_data --customers 5000 --seed 42` |
| 4 | **Presentation** | `[Deck link]` (also downloadable as PowerPoint / PDF) |
| 5 | **Additional resources** | `README.md` (quick start, features, screenshots) · `docs/DEMO.md` (walkthrough) · `docs/screenshots/` · `pytest -q` (275 tests, incl. `tests/test_redteam.py`) |

## For evaluators: test in 3 minutes

1. **Dashboard** (no setup): open the deployed link → *Results* (uplift +22.6 pts, ROI +₹2.11 lakh) →
   *Audit log* (open a decision: 15 rule checks) → *Rule editor* (change Max discount, click *Preview impact*).
2. **Telegram**: open https://t.me/Offer_genrate_bot → `/start` → pick a shopper type → `/myoffer` →
   tap *Why this offer?* → `/myoffer` again (cooldown) → `/timetravel 6` → `/myoffer` (follow-up).
3. **Code**: `pip install -r requirements.txt` → `python -m src.generate_data --customers 5000 --seed 42`
   → `python -m src.engine.pipeline --customer C00001` → `pytest -q`.

All customer data is synthetic. No API key is needed to run the code (a rule-based agent writes offers
without one).
