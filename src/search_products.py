import os
import sys

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

load_dotenv()
MODEL_NAME = "all-MiniLM-L6-v2"


def main():
    query = sys.argv[1]

    model = SentenceTransformer(MODEL_NAME)
    query_vec = model.encode(query, normalize_embeddings=True)

    conn = psycopg.connect(os.environ["DATABASE_URL_PLAIN"])
    register_vector(conn)

    rows = conn.execute(
        """
        SELECT product_name, category, brand, embedding <=> %s AS distance
        FROM products
        ORDER BY distance
        LIMIT 5
        """,
        (query_vec,),
    ).fetchall()

    for product_name, category, brand, distance in rows:
        print(f"{distance:.4f}  {product_name} ({category}, {brand})")


if __name__ == "__main__":
    main()
