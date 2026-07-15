# Phase 1 — Model the business as a star schema

> **Schema** = the shape of your database: the tables, their columns and types, and the
> rules that tie them together. This phase writes that shape down as SQL and applies it to
> the empty Postgres you brought up in Phase 0. No data yet — just the skeleton every later
> phase pours data into.

**Goal:** end this phase with **8 empty tables** in Postgres — 4 *dimensions*, 4 *facts*,
arranged as a classic **star schema** — created by a single, re-runnable **goose
migration**, with every foreign key wired and the load-order rule (dimensions before
facts) proven by watching the database *reject* a fact that references a missing
dimension.

Nothing here is generated or cleaned; this is pure modelling. But it's the most
consequential phase, because every downstream number — every margin, every delay, every
cosine-similarity search in Phase 6 — is only as trustworthy as the grain and the keys you
nail down here. Get the shape wrong and no amount of clever ETL rescues it.

## The moves
1. **One flat table can't hold a business.** Start from the naïve single spreadsheet, watch
   it contradict itself, and let that failure *force* the split into dimensions and facts.
2. **Dimensions describe, facts record.** A dimension is a *noun* (a customer, a product, a
   day); a fact is a *verb in the past tense* (an order happened, a truck was delivered, a
   pallet was charged for). Facts point back at dimensions.
3. **Grain is a promise.** Before writing a fact table you answer one question — *if I point
   at a single row, what one real-world thing is it?* — and never violate it afterward.
4. **The database owns its own keys.** Every table gets a meaningless auto-assigned
   **surrogate key**; nothing in the business (a name, an invoice number) is trusted to be
   unique or stable enough to be a primary key.
5. **Foreign keys make wrong data impossible, not just discouraged.** A fact that points at
   a nonexistent dimension is refused by the database, not quietly stored.
6. **The schema is code.** It lives in a versioned migration with an `Up` and a `Down`, so
   the whole shape is reproducible and reversible with one command.

---

## Step 0 — Pre-flight

Phase 0 already brought up Postgres (the `pgvector/pg17` container `coldchain-db` on host
port **5433**) and installed `goose`. Confirm before modelling:

```fish
cd coldchain-ops
docker compose ps                 # expect coldchain-db running
make db-status                    # goose: expect no migrations applied yet
```

This phase adds exactly one file:
- `migrations/20260629103812_add_schema.sql` — the whole star schema, `Up` and `Down`.

You create it with goose so the timestamp and skeleton are generated for you:

```fish
make db-create name=add_schema    # → migrations/<timestamp>_add_schema.sql
```

---

## Step 1 — Why not one big table?

### Chapter 0: the single spreadsheet

The most obvious model is the one a spreadsheet gives you for free: **one row per order
line, every fact copied onto it.** Something like:

| order_date | customer_name | region | product_name | supplier | qty | unit_price | unit_cost |
|---|---|---|---|---|---|---|---|
| 2024-03-15 | Giant KL | Central | Red Grapes | Capespan | 10 | 63.76 | 40.00 |
| 2024-03-15 | Giant KL | Central | Oranges | Sunkist | 25 | 28.50 | 18.00 |
| 2024-03-16 | Giant KL | Central | Gala Apple | Rockit | 8 | 41.00 | 30.00 |

It works until you try to *maintain* it. Three concrete ways it betrays you — these are the
classic **update, insertion, and deletion anomalies**, and they're the entire reason
normalization exists:

- **Update anomaly.** "Giant KL" is written on hundreds of rows. The day they rebrand, or
  you learn their region was miscoded, you must edit *every* row — and if you miss some, the
  table now says Giant KL is in two regions at once. The data can **contradict itself**.
- **Insertion anomaly.** You sign a new supplier but haven't sold any of their fruit yet.
  There's nowhere to record them — no order line to hang them on. A supplier can't exist
  until it's been *sold*, which is absurd.
- **Deletion anomaly.** You delete the last order for a customer to clean up, and the
  customer's name, region and channel vanish with it. You lost a *fact about who they are*
  by deleting a *fact about what they bought*.

The root cause is that this table is mixing two fundamentally different kinds of
information: **descriptions of things** (who Giant KL is, what a Gala Apple is) and
**records of events** (an order line). Descriptions should live once; events should
reference them.

### Chapter 1: split into dimensions and facts

**Normalize** the table: pull each kind of *thing* into its own table where it lives
exactly once, and leave behind a table of *events* that points at those things. That split
is the **star schema** — so named because when you draw it, the fact table sits in the
middle with the dimension tables radiating out around it like points of a star.

- **Dimensions** (the points of the star) — the nouns, each described once:
  `suppliers`, `products`, `customers`, `dates`.
- **Facts** (the centre) — the events, each pointing back at dimensions:
  `orders`, `order_lines`, `deliveries`, `storage_costs`.

Now "Giant KL is in Central" is stored in **one** cell of `customers`. An order *points* at
that customer instead of copying its name. Rebranding is one edit; a new supplier is one
insert with no order needed; deleting an order can't erase a customer. Every anomaly above
dissolves because every fact now lives in exactly one place.

> **Why a star and not "fully normalized" (a snowflake)?** You could normalize further —
> split `products.category` into its own `categories` table, `customers.region` into a
> `regions` table, and so on (that's a *snowflake schema*). Analytics deliberately stops at
> a *star*: dimensions are kept flat and slightly denormalized so a query joins the fact to
> **one** table per dimension, not a chain of five. The star is the sweet spot between "one
> giant table that contradicts itself" and "fifty tiny tables you can't query without a map."

---

## Step 2 — The four dimensions

Each dimension answers "describe one \_\_\_." Here's the shape and the *why* behind the
non-obvious columns.

### `suppliers` — the growers CTG imports from
```sql
CREATE TABLE suppliers (
    supplier_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    country       TEXT NOT NULL
);
```
The simplest dimension: an id, a name, a country. It exists mostly so `products` has
something real to point at (Zespri, Sunkist, Capespan…).

### `products` — the fruit SKUs
```sql
CREATE TABLE products (
    product_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_name       TEXT NOT NULL,
    category           TEXT NOT NULL,                  -- Citrus, Berries, Pome, Tropical…
    brand              TEXT,                           -- nullable: some fruit is generic
    supplier_id        BIGINT NOT NULL REFERENCES suppliers(supplier_id),
    unit               TEXT NOT NULL DEFAULT 'carton',
    shelf_life_days    INTEGER NOT NULL,               -- cold-chain relevance
    default_unit_cost  NUMERIC(12,2) NOT NULL,         -- LIST/reference cost
    default_unit_price NUMERIC(12,2) NOT NULL          -- LIST/reference price
);
```
Three decisions worth naming:
- **`brand` is nullable, `category` is not.** Every fruit has a category; not every fruit
  has a brand (loose red grapes don't). `NULL` here means "genuinely has no brand," which is
  different from "we don't know it" — and the schema lets you say so honestly.
- **`supplier_id` carries `NOT NULL REFERENCES suppliers`.** A product with no supplier is
  meaningless in an *importer's* business, so the database forbids it. This is the first FK —
  the "many products belong to one supplier" relationship, enforced.
- **`default_unit_cost`/`default_unit_price` are labelled "default" for a reason.** They're a
  *reference* price list. The price a customer *actually* paid varies per transaction and
  lives on `order_lines`, not here. Storing "the list price" and "what was actually charged"
  in the same column would conflate a catalogue with a ledger.

`NUMERIC(12,2)` (exact decimal, 2 places) — never `FLOAT` — for anything that's money.
Floating point can't represent `0.10` exactly; on money that eventually produces a total
that's off by a cent and an accountant who doesn't trust your database.

### `customers` — who CTG sells to
```sql
CREATE TABLE customers (
    customer_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_name TEXT NOT NULL,
    channel       TEXT NOT NULL,          -- Hypermarket, Supermarket, Wholesale, Ecommerce
    region        TEXT NOT NULL,          -- North, Central, South
    city          TEXT                    -- nullable: an ecommerce "customer" has no one city
);
```
`channel` and `region` are the columns Phase 4's dashboards slice by ("margin by channel,"
"delays by region"), which is exactly why they're first-class columns on the dimension and
not buried in a name string. `city` is nullable because an aggregate customer like "TikTok
Shop Orders" isn't in a single city.

### `dates` — one row per calendar day
```sql
CREATE TABLE dates (
    date        DATE PRIMARY KEY,         -- the natural key, for once (see below)
    year        INTEGER NOT NULL,
    quarter     INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    month_name  TEXT    NOT NULL,         -- "January"
    week        INTEGER NOT NULL,         -- ISO week
    day_of_week INTEGER NOT NULL,         -- 1=Mon … 7=Sun
    day_name    TEXT    NOT NULL,         -- "Monday"
    is_weekend  BOOLEAN NOT NULL
);
```
This is the one dimension that surprises people, so it's worth the space.

**Why have a date table at all — doesn't Postgres already know what a date is?** Postgres
knows `2024-03-15` is a date; it does *not* know, for free and fast, that it was a Friday in
Q1, ISO week 11, not a weekend. A **date dimension** pre-computes all those attributes
*once*, at seed time, so every "sales by quarter" or "weekday vs weekend" query is a plain
join-and-group instead of a pile of `EXTRACT(...)` calls recomputed on every row at query
time. It's the single most reused dimension in any analytics schema.

**Why is `date` itself the primary key — the one place we *don't* invent a surrogate?**
Because a calendar date already *is* a perfect natural key: `2024-03-15` is globally unique,
never changes, and is meaningful to a human reading a foreign key. Inventing a `date_id`
surrogate would buy nothing and force every fact to carry an opaque number instead of a
readable date. This is the deliberate exception that proves the surrogate-key rule — you use
a natural key exactly when one is genuinely stable, unique, and meaningful, which for dates
it is and for almost nothing else in this schema is.

---

## Step 3 — The four facts, and the idea of *grain*

A fact table records events. Before writing one you fix its **grain**: *what one row is.*
Everything else follows from that single answer.

### `orders` — grain: one row per order (the header)
```sql
CREATE TABLE orders (
    order_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id),
    order_date  DATE   NOT NULL REFERENCES dates(date),
    source      TEXT   NOT NULL DEFAULT 'system',   -- 'system'|'accounting_import'|'whatsapp'
    status      TEXT   NOT NULL DEFAULT 'fulfilled'
);
```
Note what's **not** here: no product, no quantity, no price. Those vary *within* an order
(one order can have three different products), so they can't live at order grain — they'd
force the row to repeat and you'd be back to the flat spreadsheet. They live one level down,
in `order_lines`. The order header holds only what's true of the order *as a whole*: who,
when, where it came from, and whether it went through.

- **`order_date REFERENCES dates(date)`** is the FK that makes the date dimension pay off —
  and it dictates load order: `dates` must be seeded *before* any order can reference a day.
- **`source`** is *provenance* — which pipeline created the row (`'system'` seed,
  `'accounting_import'` from Phase 3's ETL, `'whatsapp'` from Phase 6). Carrying provenance
  in the data itself lets you later ask "show me only the orders the ETL landed" without
  guessing. It's a small column that saves a lot of forensic pain.

### `order_lines` — grain: one row per product per order (the detail)
```sql
CREATE TABLE order_lines (
    order_line_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      BIGINT NOT NULL REFERENCES orders(order_id),
    product_id    BIGINT NOT NULL REFERENCES products(product_id),
    qty_cartons   INTEGER       NOT NULL,
    unit_price    NUMERIC(12,2) NOT NULL,   -- what the customer actually paid, per carton
    unit_cost     NUMERIC(12,2) NOT NULL    -- what it actually cost CTG, per carton
);
```
This is the **lowest grain in the whole model** and the table margin is computed from:
`(unit_price - unit_cost) * qty_cartons`. Two design notes that carry weight later:

- **The order's total is *not* stored anywhere.** It's *derived* by summing the lines. A
  stored total is a number that can drift out of sync with the lines it's supposed to equal;
  a derived one can't lie. This is the "totals stay honest" principle that Phase 3's
  header/detail split (Step 3 there) leans on directly.
- **`unit_price`/`unit_cost` are on the line, not the product.** The product dimension holds
  the *list* price; the line holds the *transacted* price. Same word, two different facts,
  correctly separated — which is why the messy export in Phase 2 can wobble prices ±15% and
  still be modelled honestly.

### `deliveries` — grain: one row per order (one shipment)
```sql
CREATE TABLE deliveries (
    delivery_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id       BIGINT NOT NULL REFERENCES orders(order_id),
    route          TEXT   NOT NULL,
    dispatched_at  TIMESTAMPTZ,
    planned_eta    TIMESTAMPTZ,
    delivered_at   TIMESTAMPTZ,                        -- nullable until delivered
    temp_excursion BOOLEAN NOT NULL DEFAULT FALSE
);
```
`delivered_at` is nullable on purpose: between dispatch and arrival there's a real window
where the truck is on the road and the actual arrival genuinely *isn't known yet*. `NULL`
models "not delivered yet" truthfully; a fake timestamp would not. The delay
(`delivered_at − planned_eta`) and the breach rate are computed later (Phase 4 view), not
stored — same "derive, don't store" discipline as the order total.

### `storage_costs` — grain: one row per product per day in storage
```sql
CREATE TABLE storage_costs (
    storage_cost_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cost_date           DATE   NOT NULL REFERENCES dates(date),
    product_id          BIGINT NOT NULL REFERENCES products(product_id),
    pallets_stored      NUMERIC(10,2) NOT NULL,
    cost_per_pallet_day NUMERIC(12,2) NOT NULL
);
```
Again the table stores the **ingredients** (`pallets_stored`, `cost_per_pallet_day`), and
the daily cost is their product, computed at query time — so you can re-slice by product,
category or month without a stored total constraining you.

**The pattern across all four facts:** store the raw measurements at the finest honest
grain, reference dimensions by FK, and *derive* every rolled-up number (order total, delay,
daily cost) rather than storing it. Facts are a ledger of what was measured; the arithmetic
is a view laid over them.

---

## Step 4 — Two rules baked into every table

### Surrogate keys: the database invents the IDs
Every table except `dates` has `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`. That's a
**surrogate key** — an id with *no business meaning*, minted by Postgres, whose only job is
to tell rows apart.

Why not use a "natural key" — the supplier's name, the customer's registration number? Because
natural keys break as identifiers: names change, get re-spelled, turn out not to be unique
(two "Giant" branches), or aren't known at insert time. A surrogate key is immune to all of
that because it means nothing — it can't become wrong, because it never claimed anything.
`GENERATED ALWAYS` goes one step further: it tells Postgres *you* assign this, and by default
refuses a value the client tries to supply. (That refusal becomes the whole drama of Phase
3, Step 4 — where the ETL genuinely needs to override it once.)

The practical consequence, which shapes every loader from Phase 2 on: **you insert a
dimension, then read back the IDs Postgres assigned, then use those IDs in the facts.** You
never know a row's id until after it's inserted.

### Foreign keys: wrong references are refused, not stored
Every `… REFERENCES …` line is a standing rule the database enforces on every insert: the
value in this column *must already exist* as a key in the referenced table. An
`order_lines` row pointing at `product_id = 999` when there's no such product is **rejected
at insert time**, not quietly stored to surface as a broken join three phases later. This is
the difference between "we hope the data is consistent" and "the data *cannot* be
inconsistent." It's also what forces the **load order**: a table can only be filled after
every table it points at is already filled — dimensions before facts, parent facts
(`orders`) before child facts (`order_lines`).

### Indexes on the FK columns
```sql
CREATE INDEX idx_orders_customer     ON orders(customer_id);
CREATE INDEX idx_order_lines_order   ON order_lines(order_id);
-- …one per FK column
```
Postgres auto-indexes every **primary** key but **not** the foreign-key columns on the
"many" side. Since every analytics query joins facts to dimensions *on those columns*, each
gets an explicit index. Skipping them still gives correct answers — just slow ones once the
tables are large. Cheap insurance, added now.

---

## Step 5 — The schema is a migration, not a one-off script

You *could* paste this SQL into `psql` once and be done. You don't, because then the shape
of your database lives only in your shell history. Instead it lives in a **goose
migration**: a timestamped file with an `Up` (apply the change) and a `Down` (undo it).

```sql
-- +goose Up
CREATE TABLE suppliers ( … );
-- … all 8 tables + indexes …

-- +goose Down
-- Drop in REVERSE dependency order: facts before the dimensions they reference.
DROP TABLE IF EXISTS storage_costs;
DROP TABLE IF EXISTS deliveries;
DROP TABLE IF EXISTS order_lines;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS dates;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS suppliers;
```

Two things to notice:
- **The `Down` drops in the exact reverse of the `Up`'s create order.** You can't drop
  `suppliers` while `products` still references it — the same FK rule, running backwards. So
  facts drop first, dimensions last. Writing the `Down` correctly is a second, free proof
  that you understand the dependency graph.
- **Migrations compose.** Every later phase (analytics views, the `vector` extension,
  embeddings, the audit log) is another timestamped migration applied on top. `make
  db-status` shows the stack; a fresh machine reaches the identical schema by replaying them
  in order. The database's shape is now *code* — reviewable, reversible, reproducible.

Apply it:
```fish
make db-migrate                   # goose … up
make db-status                    # the add_schema migration now shows applied
```

---

## Step 6 — Verify the shape (and *watch a foreign key work*)

```fish
make psql
```
```sql
-- 1. all 8 tables exist and are empty
\dt
SELECT 'suppliers' t, count(*) FROM suppliers
UNION ALL SELECT 'products',  count(*) FROM products
UNION ALL SELECT 'customers', count(*) FROM customers
UNION ALL SELECT 'dates',     count(*) FROM dates
UNION ALL SELECT 'orders',    count(*) FROM orders;
-- expect all zeros — the skeleton is empty by design
```

Then the check that actually *teaches* something — make the database reject a bad fact, so
you've seen the FK fire with your own eyes rather than trusting it exists:

```sql
-- 2. LOAD-ORDER REJECTION: a fact pointing at a missing dimension is refused
INSERT INTO orders (customer_id, order_date) VALUES (1, '2024-03-15');
-- ERROR: insert or update on table "orders" violates foreign key constraint
--        "orders_customer_id_fkey"
-- DETAIL: Key (customer_id)=(1) is not present in table "customers".
```
There is no customer 1 yet (dimensions are empty), so the order is **refused** — exactly the
protection you designed in. This is the load-order law demonstrated, not asserted: you
literally cannot put the cart before the horse.

Finally, a tiny hand-built end-to-end probe — insert one of each, confirm a margin comes
back, then roll it all away so the skeleton stays empty for Phase 2:

```sql
BEGIN;
INSERT INTO suppliers (supplier_name, country) VALUES ('Test Supplier', 'NZ');
INSERT INTO products (product_name, category, supplier_id, shelf_life_days,
                      default_unit_cost, default_unit_price)
  VALUES ('Test Apple', 'Pome', 1, 30, 30.00, 40.00);
INSERT INTO customers (customer_name, channel, region) VALUES ('Test Mart', 'Supermarket', 'Central');
INSERT INTO dates (date, year, quarter, month, month_name, week, day_of_week, day_name, is_weekend)
  VALUES ('2024-03-15', 2024, 1, 3, 'March', 11, 5, 'Friday', false);
INSERT INTO orders (customer_id, order_date) VALUES (1, '2024-03-15');
INSERT INTO order_lines (order_id, product_id, qty_cartons, unit_price, unit_cost)
  VALUES (1, 1, 34, 40.00, 30.00);

-- the margin formula the whole project is built to compute:
SELECT (unit_price - unit_cost) * qty_cartons AS margin FROM order_lines;
-- expect: 340.00   ( (40 - 30) * 34 )

ROLLBACK;   -- undo everything — Phase 2 starts from a clean, empty schema
```

If the FK rejection fires and the margin probe returns **RM 340.00**, Phase 1 is done: the
business is modelled, the keys and grains are fixed, the shape is reproducible with one
`make db-migrate`, and every guarantee later phases lean on is enforced by the database
rather than hoped for.

---

## Step 7 — Commit

```fish
git add migrations/20260629103812_add_schema.sql
git commit -m "feat(db): star schema — 4 dimensions, 4 facts, FKs and indexes"
```

(`feat(db)` for the schema itself. If you also touched the Makefile's goose targets in this
phase, that's a separate `chore:` commit.) Tell me when to tick the PROGRESS.md box, and
we'll move to Phase 2 — filling these empty tables.

---

### What's been verified

Confirmed against your running Postgres and the applied migration: all **8 tables** exist
(`suppliers`, `products`, `customers`, `dates`, `orders`, `order_lines`, `deliveries`,
`storage_costs`); all **8 foreign-key constraints** are wired (products→suppliers;
orders→customers, orders→dates; order_lines→orders, order_lines→products;
deliveries→orders; storage_costs→dates, storage_costs→products); the load-order rejection
fires (an `orders` insert with no matching customer is refused with a foreign-key
violation); and the end-to-end margin probe returns **RM 340.00** before being rolled back,
leaving the schema empty and ready for Phase 2. The `Down` migration drops in correct
reverse-dependency order.
