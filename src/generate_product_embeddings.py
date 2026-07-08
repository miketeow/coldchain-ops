import logging
import os

import pandas as pd
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from db import get_engine

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/etl.log",
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

engine = get_engine()
MODEL_NAME = "all-MiniLM-L6-v2"


def section(title):
    print(f"\n{'═' * 60}\n  {title}\n{'═' * 60}")


def product_text(row):
    brand = row.brand or "an unbranded"
    return f"{row.product_name}, a {row.category.lower()} product by {brand}"


def main():
    logging.info("generate_product_embeddings: start")

    products = pd.read_sql(
        "SELECT product_id, product_name, category, brand FROM products", engine
    )
    logging.info("read %d products to embed", len(products))

    model = SentenceTransformer(MODEL_NAME)
    texts = [product_text(row) for row in products.itertuples()]
    embeddings = model.encode(texts, normalize_embeddings=True)

    conn = psycopg.connect(os.environ["DATABASE_URL_PLAIN"])
    register_vector(conn)
    with conn, conn.cursor() as cur:
        for product_id, vec in zip(products["product_id"], embeddings):
            cur.execute(
                "UPDATE products SET embedding = %s WHERE product_id = %s",
                (vec, int(product_id)),
            )

    section("Phase 5 · Product embeddings")
    print(f"embedded {len(products)} products with {MODEL_NAME}")
    logging.info("wrote %d product embeddings", len(products))
    logging.info("generate_product_embeddings: done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("generate_product_embeddings: FAILED")
        raise
