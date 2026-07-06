import logging
import os
import re
from typing import cast

import pandas as pd
from sqlalchemy import text

from db import get_engine

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/etl.log",
    level=os.environ.get("LOG_LEVEL", "INFO"),  # LOG_LEVEL=DEBUG make etl → verbose
    format="%(asctime)s %(levelname)s %(message)s",
)

engine = get_engine()

RAW = "data/accounting_export.xlsx"


# helper functions
def peek(df, *cols):
    print(df[list(cols)].head(), "\n")


def section(title):
    """Print a visual divider so terminal output reads in clear blocks."""
    print(f"\n{'═' * 60}\n  {title}\n{'═' * 60}")


def main():
    logging.info("etl_orders: start (source=%s)", RAW)

    # ── Step 1: read the export honestly ──────────────────────────────────────
    df = pd.read_excel(RAW, sheet_name="Orders", skiprows=2)
    logging.info("read %d rows from %s", len(df), RAW)
    logging.debug("raw dtypes: %s", df.dtypes.to_dict())

    section("Step 1 · Read export")
    print("shape  :", df.shape)
    print("columns:", df.columns.tolist())
    print("\ndtypes:")
    print(df.dtypes)  # read dtypes before writing a transform
    print("\nfirst rows:")
    print(df.head(3))

    # ── Step 2a: dates — detect the format per row, parse each one exactly ─────
    #
    iso_mask = df["order_date"].str.match(r"^\d{4}-\d{2}-\d{2}$")  # 2024-06-07
    slash_mask = df["order_date"].str.match(r"^\d{2}/\d{2}/\d{4}$")  # 15/03/2024
    long_mask = ~(iso_mask | slash_mask)  # "March 15, 2024"

    parsed = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    parsed[iso_mask] = pd.to_datetime(
        df.loc[iso_mask, "order_date"], format="%Y-%m-%d", errors="coerce"
    )
    parsed[slash_mask] = pd.to_datetime(
        df.loc[slash_mask, "order_date"], format="%d/%m/%Y", errors="coerce"
    )
    parsed[long_mask] = pd.to_datetime(
        df.loc[long_mask, "order_date"], format="%B %d, %Y", errors="coerce"
    )

    # Keep it as datetime — do NOT reduce to .dt.date yet. That reduction happens in
    # Step 2d, after unparseable rows are quarantined, so .isna() can still find NaT here.
    df["order_date"] = parsed

    # DIAGNOSTIC (not an assert): errors="coerce" is meant to quarantine bad dates
    # downstream, so an unparsed row is a number to watch, not a reason to crash.
    section("Step 2a · Parse dates")
    print(
        "date rows iso/slash/long:", iso_mask.sum(), slash_mask.sum(), long_mask.sum()
    )
    print("dates unparsed:", df["order_date"].isna().sum())  # expect 0 on this file

    logging.info(
        "date formats iso/slash/long: %d/%d/%d",
        iso_mask.sum(),
        slash_mask.sum(),
        long_mask.sum(),
    )
    n_unparsed = int(df["order_date"].isna().sum())
    if n_unparsed:
        logging.warning("dates unparsed: %d (will be quarantined)", n_unparsed)

    # ── Step 2b: product name -> product_id (normalize-then-join) ──────────────
    def canon(name: str) -> str:
        """'Grapes - Red' and 'red grapes ' both -> 'grapes red'."""
        tokens = re.split(r"[^a-z0-9]+", str(name).lower().strip())
        tokens = [
            t for t in tokens if t
        ]  # drop the empty strings the split leaves at the edges
        return " ".join(sorted(tokens))

    products = pd.read_sql("SELECT product_id, product_name FROM products", engine)
    products["canon"] = products["product_name"].map(canon)

    assert products["canon"].is_unique, "canonical-form collision between products"

    lookup = products.set_index("canon")["product_id"].to_dict()
    df["canon"] = df["product"].map(canon)
    df["product_id"] = cast(pd.Series, df["canon"]).map(lookup)

    section("Step 2b · Match products")
    print(
        "products unmatched:", df["product_id"].isna().mean()
    )  # expect 0.0 on this file
    peek(df, "product", "canon", "product_id")

    n_unmatched = int(df["product_id"].isna().sum())
    if n_unmatched:
        logging.warning("products unmatched: %d (will be quarantined)", n_unmatched)

    def clean_money(s: pd.Series) -> pd.Series:
        """'RM 1,250.00' | '45.50' | 50 | '' -> 1250.00 | 45.50 | 50.0 | NaN."""
        cleaned = (
            s.astype(str)  # str and int both -> str
            .str.replace(r"[^0-9.\-]", "", regex=True)  # strip RM, spaces, anything
            .replace("", pd.NA)  # blank -> real missing value
        )
        return pd.to_numeric(
            cleaned, errors="coerce"
        )  # final cast; bad -> NaN, not crash

    df["unit_price"] = clean_money(df["unit_price"])
    df["unit_cost"] = clean_money(df["unit_cost"])

    df["qty_cartons"] = pd.to_numeric(
        df["qty_cartons"], errors="coerce"
    )  # stray text qty -> NaN too

    # a row is unusable if it lost its quantity, never matched a product, OR its date never parsed
    bad_mask = (
        df["qty_cartons"].isna() | df["product_id"].isna() | df["order_date"].isna()
    )

    rejects = df[bad_mask].copy()
    rejects["reject_reason"] = (
        df.loc[bad_mask, "qty_cartons"].isna().map({True: "null_qty", False: ""})
        + df.loc[bad_mask, "product_id"]
        .isna()
        .map({True: "|unmatched_product", False: ""})
        + df.loc[bad_mask, "order_date"].isna().map({True: "|bad_date", False: ""})
    )
    rejects.to_csv("data/rejects_orders.csv", index=False)

    clean = df[~bad_mask].copy()
    clean["qty_cartons"] = clean["qty_cartons"].astype(int)  # safe now: no NaN left
    clean["order_date"] = clean[
        "order_date"
    ].dt.date  # safe now: no NaT left — reduce to a plain date

    section("Step 2d · Quarantine")
    print(f"rows in: {len(df)}  quarantined: {len(rejects)}  clean: {len(clean)}")

    logging.info(
        "rows in %d, quarantined %d, clean %d", len(df), len(rejects), len(clean)
    )
    logging.info(
        "reject reasons: %s", rejects["reject_reason"].value_counts().to_dict()
    )

    consistency = clean.groupby("order_id")[["customer_id", "order_date"]].nunique()
    assert (consistency <= 1).all().all(), (
        "an order has conflicting customer/date across lines"
    )

    orders = clean.groupby("order_id", as_index=False).agg(
        customer_id=("customer_id", "first"), order_date=("order_date", "first")
    )
    orders["source"] = (
        "accounting_import"  # provenance — records that THIS pipeline produced the row
    )
    orders["status"] = "fulfilled"  # the export only ever contains completed sales

    order_lines = clean[
        ["order_id", "product_id", "qty_cartons", "unit_price", "unit_cost"]
    ]

    section("Step 3 · Header / detail split")
    print(len(orders), "orders /", len(order_lines), "lines")

    logging.info("split into %d orders / %d lines", len(orders), len(order_lines))

    # engine = get_engine()  already at the top of the file

    section("Step 4 · Load to Postgres")

    # 1. stage the headers (an ordinary table — pandas writes it happily)
    orders.to_sql("stg_orders", engine, if_exists="replace", index=False)

    # 2. promote them into the real table, keeping order_id intact
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO orders (order_id, customer_id, order_date, source, status)
            OVERRIDING SYSTEM VALUE
            SELECT order_id, customer_id, order_date, source, status
            FROM stg_orders
            ORDER BY order_id;
        """)
        )
        conn.execute(text("DROP TABLE stg_orders;"))

    with engine.begin() as conn:
        conn.execute(
            text("""
            SELECT setval(
                pg_get_serial_sequence('orders', 'order_id'),
                (SELECT MAX(order_id) FROM orders)
            );
        """)
        )

    order_lines.to_sql("order_lines", engine, if_exists="append", index=False)
    logging.info(
        "loaded %d orders + %d lines into postgres", len(orders), len(order_lines)
    )
    print(f"loaded {len(orders)} orders + {len(order_lines)} lines into postgres")

    logging.info("etl_orders: done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("etl_orders: FAILED")  # ERROR level + full traceback
        raise
