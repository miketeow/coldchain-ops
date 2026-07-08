# Phase 5 — Semantic product search (pgvector + sentence embeddings)

> **Embedding** = a piece of text run through a neural network and turned into a fixed
> list of numbers (a *vector*) such that texts with similar **meaning** end up as
> nearby vectors — regardless of whether they share a single word. "Mango" and "tropical
> fruit" land close together not because of a rule you wrote, but because the model
> learned that association from the way those ideas co-occur in the text it was trained
> on. Postgres/pgvector doesn't do that learning — it just stores the resulting numbers
> and answers "which stored vectors are nearest to this one?" fast.

**Goal:** end this phase with every row in `products` carrying an `embedding` column, a
script that (re)computes those embeddings, and a query path that ranks products by
*meaning* — "citrus fruit" finding Oranges even though the word "citrus" never appears
in your data. This is the semantic-*meaning* layer, sitting next to Phase 4's semantic-
*business-logic* layer (the views). Phase 6 will reuse this exact mechanism to fuzzy-
match a WhatsApp customer's free-text product mention to a real `product_id`.

Recall Phase 3's product-name defect: Phase 2 planted 4 casing/spacing/reorder variants
per product name, and Phase 3 fixed it by **normalizing both sides to one canonical
string, then exact-matching** — a rule you wrote by hand, for variants you already knew
about. Embeddings solve a more general version of that same problem: they tolerate
variation *without* you having enumerated it in advance. That's the thread connecting
Phase 3 → this phase → Phase 6.

## The moves
1. **Lexical match ends where meaning begins.** `ILIKE`/full-text search compares word
   forms (with stemming, at best). It cannot find "Oranges" from the query "citrus
   fruit" — there is no shared word, stemmed or not. An embedding model encodes what the
   words *mean*, not just their letters.
2. **One vector per row, same grain as the thing it describes.** `products` is a
   dimension — one row per product. The embedding is a property of that same row, so it
   lives as one more column on `products`, not a new table. No grain decision to make
   here (there's only one thing being described), which is what makes this simpler than
   Phase 4's fact-table grain calls.
3. **Same model in, same model out.** A vector is only comparable to other vectors
   produced by the *same* embedding model — the coordinate space is specific to how that
   model was trained. Whatever model embeds your 8 products at write time must be the
   same model that embeds a search query at read time, or "nearest" is meaningless.
4. **Exact search is fine until it isn't.** At 8 rows, comparing a query vector against
   every stored vector (brute force) is instant. An approximate-nearest-neighbor index
   (`ivfflat`/`hnsw`) only earns its cost at real scale — thousands to millions of rows —
   the same "don't reach for the expensive tool before the data demands it" call as
   Phase 4's plain-vs-materialized-view decision.

---

## Step 0 — Pre-flight

```fish
cd coldchain-ops
docker compose ps          # expect coldchain-db running
make psql
```
```sql
-- confirm the extension binary is present in this image (it is — pgvector/pgvector:pg17)
SELECT * FROM pg_available_extensions WHERE name='vector';
-- expect: default_version 0.8.3, installed_version EMPTY (not enabled yet — that's this phase's job)

\d products
-- expect: product_id, product_name, category, brand, supplier_id, unit, shelf_life_days,
--         default_unit_cost, default_unit_price — no embedding column yet

SELECT count(*) FROM products;   -- expect 8
\q
```

This phase adds, for real this time, **new dependencies** — there's no way around it,
turning text into vectors requires a model:

```fish
uv add sentence-transformers   # the embedding model + inference code (pulls in PyTorch)
uv add pgvector                # Python-side adapter: lets psycopg send/receive `vector` values
                                # as plain Python lists/numpy arrays instead of hand-built strings
```

`sentence-transformers` is a large install (PyTorch underneath) — expect it to take a
minute and pull a few hundred MB. That's a one-time local/dev cost only: this project
has no live deployment, the repo + walkthrough is the deliverable, so install size never
has to survive a production constraint. (If it did, the lighter path would be `fastembed`
— same category of model, run through the smaller ONNX runtime instead of full PyTorch —
but `sentence-transformers` is the name any engineer reading your code will recognize
immediately, which is the thing that actually matters when nobody ever deploys this.)

This phase adds one migration (schema: extension + column) and two scripts:
- `src/generate_product_embeddings.py` — compute and store the embedding for every product.
- `src/search_products.py` — take a free-text query, embed it, print the nearest products.

---

## Step 1 — Chapter 0: the naive search, and why it has a ceiling

### The obvious first try

```sql
SELECT product_name FROM products WHERE product_name ILIKE '%citrus%';
```

Zero rows. Not because there's a bug — because no product is literally named "citrus."
"Oranges" is a citrus fruit, but `ILIKE` only knows about substrings; it has no concept
that "Oranges" and "citrus" are related. Layering full-text search (`tsvector`/`tsquery`)
on top helps with a *different* problem — stemming word forms ("run"/"running"), ranking
by frequency and position — but it's still comparing **word forms**. It would not find
"Oranges" for "citrus" either, unless you'd hand-built a synonym dictionary mapping one
to the other, one pair at a time.

Worse: try it against Phase 2's own planted defects. If a raw import ever stored a
variant like `"red  grapes"` (double space) or `"RED GRAPES"` (different casing) —
exactly the kind of noise Phase 3 had to canonicalize by hand before it could exact-match
— a naive `ILIKE '%Red Grapes%'` search can *still* miss rows depending on collation and
whitespace, unless you normalize both sides yourself, every time, the same way Phase 3's
ETL did.

### The fix: search by meaning, not by spelling

An embedding model doesn't care about exact spelling, casing, or word choice — it
encodes what the text is *about*. "citrus fruit," "Oranges," and even a mis-spaced
`"orang es"` all land in roughly the same neighborhood of the vector space, because nothing
about the *meaning* changed. This is strictly more general than Phase 3's canonical-form
trick: Phase 3 handled variants you'd already seen and enumerated; embeddings handle
variants (and synonyms, and related concepts) you never wrote a rule for. This is exactly
why Phase 6's WhatsApp parser will lean on this same mechanism — a customer typing
"redgrapes plz" or "grapes (red ones)" isn't a case you can canonicalize in advance.

---

## Step 2 — The schema decision: one column, right on `products`

`products` is already the dimension that describes one product per row. The embedding is
one more descriptive fact about that same row — same grain, no new table, no join needed
to attach it. This is the one-line version of Phase 4's grain lesson: *figure out what
one row represents, then don't let anything of a different grain sit in the same place.*
Here there's only one grain in play, so the decision is just "which existing table."

```sql
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE products ADD COLUMN embedding vector(384);
```

**Why 384, specifically.** That number isn't a Postgres default — it's the output size of
the model you're about to choose in Step 3 (`all-MiniLM-L6-v2` produces 384-dimensional
vectors). The `vector(384)` column type is a fixed-width contract: it will refuse to
store a vector of any other length. If you ever swap models for one with a different
output size, this column's width has to change with it — another instance of "the shape
of the data is decided by the tool that produced it," same as Phase 3's `NUMERIC`
precision being decided by what the source system could represent.

---

## Step 3 — Generate the embeddings

### The one-time decision: what text actually gets embedded

A product is four-ish columns (`product_name`, `category`, `brand`, `unit`). The model
embeds *text*, so you have to decide what sentence represents "this product" — and like
Phase 4's margin formula, that decision belongs in **one place**, written once, so the
same construction is used whether you're embedding all 8 products today or re-embedding
after a catalog change next month. A natural-language phrasing embeds better than a raw
comma-joined tuple, because the model was trained on sentences, not on structured data —
something like:

```python
def product_text(row):
    return f"{row.product_name}, a {row.category.lower()} product by {row.brand}"
```

(You decide the exact phrasing — the point is that it's a function, called from both the
initial generation script and, conceptually, anywhere else a product ever needs
re-embedding, not copy-pasted string-building in two places.)

### The script: `src/generate_product_embeddings.py`

This one **writes** to the database (unlike the read-only checks I ran above), so it's
squarely your script to type and run — not something I hand you finished. Here's the
concept made concrete, in the same shape as `generate_logistics.py`:

```python
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
```

Worth reading closely rather than just typing out:

- **`pd.read_sql(..., engine)`** — the same `db.get_engine()` / SQLAlchemy path
  `generate_logistics.py` already uses for reads. Reading is fine through SQLAlchemy;
  it's the *write* below that changes approach.
- **`SentenceTransformer(MODEL_NAME)` loaded once, outside any loop** — it downloads the
  model on first run (cached locally after) and loading it is the slow step; do it once,
  not per row.
- **`model.encode(texts, normalize_embeddings=True)`** — one call, all 8 products at
  once, returning a numpy array of 384-float rows. `normalize_embeddings=True` scales
  every vector to unit length, the standard prep step for the cosine-distance search
  Step 4 runs.
- **Why this switches to raw `psycopg` + `DATABASE_URL_PLAIN` instead of the
  SQLAlchemy `engine`.** You're updating 8 existing rows, not appending new ones, so
  `.to_sql(if_exists="append")` (the pattern `generate_logistics.py` used) doesn't apply
  here. A handful of per-row parameterized `UPDATE`s is simplest done directly — the same
  way `make psql` talks to Postgres directly rather than through SQLAlchemy.
- **`register_vector(conn)`** is what makes `cur.execute(..., (vec, ...))` work at all —
  without it, `psycopg` has no idea how to turn a numpy array into a Postgres `vector`
  literal, and you'd be hand-formatting `'[0.123,0.456,...]'` strings yourself. This is
  the entire reason the `pgvector` Python package is a dependency, separate from the
  Postgres `vector` extension itself.

**Before → after, one product.**

BEFORE — a `products` row with no embedding:

| product_id | product_name | category | brand | embedding |
|---|---|---|---|---|
| 3 | Red Grapes | Berries | BrandX | NULL |

AFTER — the same row, embedding populated:

| product_id | product_name | category | brand | embedding |
|---|---|---|---|---|
| 3 | Red Grapes | Berries | BrandX | `[0.0123, -0.0871, ..., 0.0456]` (384 numbers) |

Nothing else about the row changed — this is additive, exactly like Phase 4's computed
columns were additive on top of raw facts.

---

## Step 4 — Search by meaning: `src/search_products.py`

The query side has to embed its input text with the **same model** used in Step 3 (move
#3) — there's no stored SQL for this part, because the "query" is arbitrary text supplied
at run time, not a fixed definition like Phase 4's views.

```python
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
```

**Why `load_dotenv()` is explicit here, unlike Step 3's script.**
`generate_product_embeddings.py` imports `from db import get_engine`, and `db.py` calls
`load_dotenv()` as a module-level side effect — so `.env` gets loaded into `os.environ`
the moment that import runs, even though the script never calls `load_dotenv()` itself.
`search_products.py` has no reason to import `db` (no SQLAlchemy engine needed, just raw
`psycopg`), so nothing loads `.env` unless this script does it explicitly. Skipping this
line is exactly the kind of implicit, borrowed behavior that bites later — worth calling
out rather than silently relying on another script's import order.

**The operator doing the work:** `<=>` is pgvector's **cosine distance** operator — `0`
means identical direction (as similar as possible), `2` means opposite. `ORDER BY
distance` ascending puts the closest *meaning* first. This is the vector equivalent of
Phase 4's `delay_hours` computation: a raw comparison Postgres can't do natively (there's
no built-in "how similar are these ideas" operator) becomes possible once pgvector adds
the type and the operator.

**What you should see, run for real:** query `"citrus fruit"` ranks "Oranges" at the
smallest distance, despite the word "citrus" appearing nowhere in your data — the thing
`ILIKE` structurally cannot do. I haven't (and can't, without running the model myself)
pre-verify the exact distance numbers the way I pre-verified Phase 4's totals with
read-only SQL — generating real embeddings requires actually running the model, which is
this phase's implementation, not a read-only check. Confirming "Oranges" comes back for
"citrus fruit" is your Step 6 verification, not mine.

---

## Step 5 — The index question (a Chapter 0 aside, not a build step)

At 8 rows, the query above does a full sequential scan comparing the query vector
against all 8 stored ones — sub-millisecond, no index needed. Worth knowing *why* you'd
eventually want one without building it now: pgvector offers `ivfflat` and `hnsw`
approximate-nearest-neighbor indexes, which trade a small amount of recall accuracy for
speed once brute-force comparison against every row becomes the bottleneck — realistically
somewhere in the thousands-to-millions-of-rows range. Building either index here would be
solving a scale problem your data doesn't have, mirroring exactly why Phase 4 stayed with
plain views instead of materializing them: match the tool to the data you actually have,
not the data you might have someday.

---

## Step 6 — Package the schema change as a migration

```fish
make db-create name=add_product_embeddings
```

```sql
-- +goose Up
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE products ADD COLUMN embedding vector(384);

-- +goose Down
ALTER TABLE products DROP COLUMN IF EXISTS embedding;
DROP EXTENSION IF EXISTS vector;
```

Reverse order in `Down`, same discipline as Phase 4 — drop the column before the
extension it depends on, so a rollback leaves the database exactly as it was.

```fish
make db-migrate
make db-status     # new migration shows as applied
```

---

## Step 7 — Verify it worked

```sql
-- extension now enabled
SELECT * FROM pg_available_extensions WHERE name='vector';
-- expect installed_version = 0.8.3

-- column present, still 8 rows, embeddings populated after you run the generation script
\d products
SELECT count(*) AS total, count(embedding) AS with_embedding FROM products;
-- expect: 8 | 8  (once generate_product_embeddings.py has run)

-- the semantic test: run your search script
uv run python src/search_products.py "citrus fruit"
-- expect: Oranges ranked first (or near-first), despite "citrus" never appearing in your data

-- a second test that calls back to Phase 3's canonicalization problem
uv run python src/search_products.py "red grapes"
-- expect: Red Grapes ranked first, robust to the exact casing/spacing you typed the query in
```

If "Oranges" doesn't come back first for "citrus fruit," the likely culprits are the same
category of mistake as any other phase: wrong model loaded at query time vs. generation
time (move #3), or `normalize_embeddings` set inconsistently between the two scripts.

---

## Step 8 — Commit

```fish
git add migrations/*_add_product_embeddings.sql src/generate_product_embeddings.py src/search_products.py pyproject.toml uv.lock
git commit -m "feat(db): add pgvector product embeddings and semantic search"
```

Tell me when you want the PROGRESS.md box ticked, and we'll move to Phase 6 (WhatsApp →
structured order), which reuses this exact embed-and-compare mechanism to fuzzy-match a
customer's free-text product mention to a real `product_id`.

---

### What I verified while writing this (read-only, nothing in your DB changed)

- `pg_available_extensions` confirms `vector` is present in this image at version
  **0.8.3**, `installed_version` empty — the extension is available but not yet enabled,
  which is this phase's first step, not something already done.
- `\d products` confirms the current column set (`product_id`, `product_name`,
  `category`, `brand`, `supplier_id`, `unit`, `shelf_life_days`, `default_unit_cost`,
  `default_unit_price`) with no `embedding` column yet, and `brand` is nullable — worth
  knowing if your `product_text()` function needs to handle a missing brand.
- `products` currently holds **8 rows** — the number every count-check above assumes.

**On you to run** (this phase, more than any before it, is genuinely yours): installing
`sentence-transformers`/`pgvector`, writing and running both scripts, and confirming the
semantic search actually ranks "Oranges" for "citrus fruit." Unlike Phase 4, I can't
pre-verify those numbers with a read-only query — producing them *is* the implementation,
so Step 7 is where you find out if it works, not me.
