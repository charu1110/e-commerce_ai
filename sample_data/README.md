# Testing dataset

All data is **synthetic** (no real people). This folder is a readable slice of 300 customers.
The app itself uses the full 5,000-customer store, which you can rebuild in about 45 seconds:

```
python -m src.generate_data --customers 5000 --seed 42
```

The hosted dashboard uses `demo/demo.db` (all 5,000 customers, their orders and the 14-day simulated
campaign), so evaluators don't need to generate anything.

| File | Rows | What it is |
|---|---|---|
| `customers.csv` | 300 | Profile: name, age, city, tier, region, language, favourite categories, membership, opt-in, usual shopping hour |
| `orders.csv` | 6,865 | 18 months of orders: category, amounts, discount, coupon, payment method (UPI/COD/card/wallet), returned |
| `sessions.csv` | 26,135 | Browsing: categories viewed, searches, added to cart |
| `cart_events.csv` | 20,032 | Cart items: purchased / abandoned / removed |
| `reward_points.csv` | 300 | Points balance and points expiring soon |
| `categories.csv` | 7 | Average price, margin and return rate per category |
| `customer_truth.csv` | 300 | **Hidden** persona and true price sensitivity. Only the simulator reads this; the engine never does. Use it to check that the engine's segments match reality. |

Regenerate this slice with `python scripts/export_sample_data.py`.

## Quick ways to test

- One customer through the 10-step engine, every step explained:
  `python -m src.engine.pipeline --customer C00001`
- Realism report (24 checks): `python scripts/validate_data.py`
- Segments vs hidden personas: `python -m src.segments`
- Attack tests (the AI tries to break the rules): `pytest tests/test_redteam.py -q`
