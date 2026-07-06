import logging
import os

import numpy as np
import pandas as pd

from db import get_engine

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/etl.log",
    level=os.environ.get("LOG_LEVEL", "INFO"),  # LOG_LEVEL=DEBUG make etl → verbose
    format="%(asctime)s %(levelname)s %(message)s",
)

engine = get_engine()
SEED = 42
rng = np.random.default_rng(SEED)

ROUTES = {
    "North": ["Penang Island Loop", "Butterworth–Sungai Petani", "BM–Alor Setar"],
    "Central": ["KL Klang Valley Run", "Shah Alam–Petaling", "Seremban Express"],
    "South": ["Johor Bahru Line", "Melaka–Muar", "Batu Pahat Run"],
}
TRANSIT_H = {
    "North": (4, 8),
    "Central": (10, 16),
    "South": (14, 22),
}  # transit hours grow with distance

# storage rate in RM per pallet per day — chilled categories cost more than ambient.
RATE = {"Citrus": 3.20, "Berries": 5.80, "Pome": 3.00, "Tropical": 3.60}
DEFAULT_RATE = 3.50  # fallback in case a new category is added later


def section(title):
    """Print a visual divider so terminal output reads in clear blocks."""
    print(f"\n{'═' * 60}\n  {title}\n{'═' * 60}")


def main():
    logging.info("generate_logistics: start")

    # ── 5a: deliveries — one shipment per surviving order ──────────────────────
    orders = pd.read_sql(
        """
        SELECT o.order_id, o.order_date, c.region
        FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id
        WHERE o.source = 'accounting_import'
    """,
        engine,
    )
    logging.info("read %d orders to generate deliveries for", len(orders))

    n = len(orders)
    regions = orders["region"].to_numpy()

    dispatched = (
        pd.to_datetime(orders["order_date"])
        + pd.Timedelta(hours=5, minutes=30)
        + pd.to_timedelta(rng.integers(0, 120, n), unit="m")
    )  # ~05:30 + up to 2h jitter
    transit_h = np.array([rng.uniform(*TRANSIT_H[r]) for r in regions])
    planned = dispatched + pd.to_timedelta(transit_h, unit="h")
    delay_h = rng.gamma(shape=1.4, scale=1.3, size=n)  # most on time, a few badly late
    delivered = planned + pd.to_timedelta(delay_h, unit="h")
    breach_p = np.where(
        regions == "South", 0.12, np.where(regions == "Central", 0.07, 0.04)
    )  # breach likelier on longer runs
    temp_excursion = rng.random(n) < breach_p
    route = np.array([ROUTES[r][rng.integers(len(ROUTES[r]))] for r in regions])

    deliveries = pd.DataFrame(
        {
            "order_id": orders["order_id"].to_numpy(),
            "route": route,
            "dispatched_at": dispatched.to_numpy(),
            "planned_eta": planned.to_numpy(),
            "delivered_at": delivered.to_numpy(),
            "temp_excursion": temp_excursion,
        }
    )
    deliveries.to_sql("deliveries", engine, if_exists="append", index=False)

    breaches = int(temp_excursion.sum())
    section("Step 5a · Deliveries")
    print(f"deliveries: {len(deliveries)}  breaches: {breaches}")
    logging.info("loaded %d deliveries (%d temp breaches)", len(deliveries), breaches)
    logging.debug("breach rate: %.1f%%", 100 * breaches / len(deliveries))

    # ── 5b: storage_costs — one row per product per day in storage ─────────────
    dates = pd.read_sql("SELECT date FROM dates ORDER BY date", engine)["date"]
    products = pd.read_sql("SELECT product_id, category FROM products", engine)

    rows = []
    for _, p in products.iterrows():
        rate = RATE.get(p["category"], DEFAULT_RATE)
        held = rng.random(len(dates)) < 0.70  # roughly 70% of days have stock on hand
        pallets = rng.integers(2, 40, len(dates))
        for d, h, pal in zip(dates, held, pallets):
            if h:
                rows.append((d, int(p["product_id"]), int(pal), rate))

    storage = pd.DataFrame(
        rows,
        columns=["cost_date", "product_id", "pallets_stored", "cost_per_pallet_day"],
    )
    storage.to_sql("storage_costs", engine, if_exists="append", index=False)

    section("Step 5b · Storage costs")
    print(f"storage_costs: {len(storage)} rows")
    logging.info("loaded %d storage_cost rows", len(storage))

    logging.info("generate_logistics: done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("generate_logistics: FAILED")  # ERROR level + full traceback
        raise
