# Phase 2 — Populate the schema: seed the dimensions, generate a *deliberately messy* export

> **Seed** = fill an empty database with a starting set of rows. Here you seed the four
> dimensions directly into Postgres, and you *manufacture* a realistically ugly Excel export
> of orders — the file Phase 3 will later have to clean. You are, on purpose, creating the
> mess you'll pay yourself to clean up.

**Goal:** end this phase with the **4 dimensions filled** in Postgres (6 suppliers, 8
products, 7 customers, 731 dates) and a single file `data/accounting_export.xlsx` — ~12,500
order lines carrying **four planted defects** that mimic a real accounting export. The two
logistics facts (`deliveries`, `storage_costs`) are deliberately left empty; they need real
order IDs to point at and so are deferred to Phase 3. Everything is rebuildable from empty
with one `make seed`.

This is the phase that makes the project *honest*. Anyone can clean tidy data; the reason
Phase 3 is worth showing an interviewer is that the input genuinely lies about its shape —
and it lies because **you** taught it to, defect by defect, in this phase. Each flaw you
plant here is un-planted by exactly one transform there.

## The moves
1. **Deterministic randomness.** Seed the random generator with a fixed number, so "random"
   produces the *same* data on every run. Reproducibility beats novelty — your Phase 3
   numbers must not shift underfoot.
2. **Referential integrity by construction.** Never hardcode a foreign key. Insert a
   dimension, read back the IDs Postgres assigned, and build the next table from *those*. A
   key you read from the database cannot point at something missing.
3. **Realistic ≠ uniform.** Real businesses are lopsided — a few customers dominate, weekends
   are quiet. Weight the randomness so believable patterns *emerge*, instead of flat noise
   that shows nothing downstream.
4. **Defects on purpose, one at a time.** Inject exactly four kinds of mess — mixed date
   formats, spelling variants, money-as-text, blank cells — each a clean 1:1 inverse of a
   Phase 3 cleaning step. Plant them in a known order so the fix has a known order.
5. **Separate the two data origins.** Dimensions and the *shape* of orders are authored (you
   choose the 8 real products); the *mess* is generated. Keep the honest core and the
   injected noise conceptually distinct — it's the same "labelled synthetic scaffolding is
   fine, disguised fabrication is not" line that recurs in Phase 3.

This phase adds three scripts to `src/`:
- `src/seed_dimensions.py` — suppliers → products → customers, via the read-back-IDs pattern.
- `src/generate_date_dim.py` — two years of calendar rows with pre-computed attributes.
- `src/generate_messy_orders.py` — build clean orders, then dirty them into the xlsx.

They share `src/db.py`'s `get_engine()` — the SQLAlchemy **engine**, the live connection
pandas dials through — the same one every later phase uses.

---

## Step 0 — Pre-flight

```fish
cd coldchain-ops
docker compose ps            # coldchain-db running
make psql
```
```sql
-- the 4 dimensions exist (Phase 1) and are still EMPTY
SELECT 'suppliers' t, count(*) FROM suppliers
UNION ALL SELECT 'products',  count(*) FROM products
UNION ALL SELECT 'customers', count(*) FROM customers
UNION ALL SELECT 'dates',     count(*) FROM dates;
-- expect 0 / 0 / 0 / 0
\q
```

`pandas`, `numpy`, `sqlalchemy`, `psycopg`, `openpyxl` are already installed (Phase 0). No
new dependencies.

---

## Step 1 — Seed the dimensions, and *feel* the surrogate-key boundary

### Chapter 0: the tempting shortcut that quietly rots

The naïve way to link products to suppliers is to just *decide* the IDs yourself:

```python
# supplier 4 is Rockit Global… I think? let me count the list…
products = pd.DataFrame([{ "product_name": "Gala Apple", "supplier_id": 4, … }])
```

This works exactly until it doesn't. `supplier_id` is `GENERATED ALWAYS AS IDENTITY`
(Phase 1) — **Postgres** chooses those numbers, not you. Guess "4" and you might get Montague
instead of Rockit; reorder the supplier list one day and every hardcoded number silently
points at the wrong grower. You'd have referential integrity (the FK is satisfied) with
*semantic* garbage (it points at the wrong real thing) — the worst kind of bug, because
nothing errors.

### The fix: insert, read the IDs back, then map by *name*

The honest pattern — the one every loader in this project repeats — is: insert the parent,
ask the database what IDs it assigned, and join the child on a **name** you control rather
than an **id** the database controls.

```python
# 1. insert suppliers — let Postgres mint the supplier_ids
suppliers = pd.DataFrame([
    {"supplier_name": "Zespri International", "country": "New Zealand"},
    {"supplier_name": "Sunkist Growers",     "country": "USA"},
    # …6 total
])
suppliers.to_sql("suppliers", engine, if_exists="append", index=False)

# 2. read back the ids the DB actually assigned, keyed by name
sup = pd.read_sql("SELECT supplier_id, supplier_name FROM suppliers", engine)
sid = dict(zip(sup.supplier_name, sup.supplier_id))     # {"Rockit Global": 4, …}

# 3. author products by SUPPLIER NAME, then translate name → real FK
products = pd.DataFrame([
    {"product_name": "Gala Apple", "category": "Pome", "brand": "Rockit",
     "supplier": "Rockit Global", "shelf_life_days": 40,
     "default_unit_cost": 38.00, "default_unit_price": 55.00},
    # …8 total
])
products["supplier_id"] = products["supplier"].replace(sid)   # name → the id Postgres chose
products = products.drop(columns=["supplier"])                # drop the helper column
products.to_sql("products", engine, if_exists="append", index=False)
```

The key line is `products["supplier_id"] = products["supplier"].replace(sid)`. You wrote
`"Rockit Global"` — a stable, human-readable fact you *know* — and let the lookup resolve it
to whatever number the database happens to have assigned. Reorder the suppliers, reseed on a
fresh machine, get different `supplier_id`s — and the products still attach to the right
supplier, because the join was on the name, never on a guessed number. **That's referential
integrity by construction:** the FK is right not because you were careful, but because it
was *impossible* to be wrong.

`customers` has no FK, so it's a plain insert of the 7 rows. Note the deliberate touches
that pay off later: `brand` is `None` for loose Red Grapes (a real null, honestly modelled),
`city` is `None` for "TikTok Shop Orders" (an ecommerce customer with no single city), and
the 7 customers span four `channel`s and three `region`s so Phase 4's "by channel / by
region" cuts have something to show.

```python
print(f"seeded {len(suppliers)} suppliers, {len(products)} products, {len(customers)} customers")
# seeded 6 suppliers, 8 products, 7 customers
```

> **Why author the 8 products by hand instead of generating them?** Because the *dimensions*
> are the honest core of the model — real fruit, real suppliers, real Malaysian retail
> channels. Only the *transactions* get synthesized (and messed up). A portfolio piece is
> more convincing when the catalogue is plausible and specific ("SunGold Kiwi from Zespri")
> than when it's `Product_0001`. Generation is for volume; authorship is for the handful of
> things a human would actually curate.

---

## Step 2 — The date dimension: computed once, in pandas

`dates` is filled from code, not typed by hand — 731 rows is too many, and every attribute
is *derivable* from the date itself. pandas is the right tool: `date_range` produces the
calendar, and the `.dt` accessor computes each attribute in one vectorized shot.

```python
dates = pd.date_range("2024-01-01", "2025-12-31", freq="D")   # every day, 2 full years
dim = pd.DataFrame({"date": dates})

dim["year"]        = dim["date"].dt.year
dim["quarter"]     = dim["date"].dt.quarter
dim["month"]       = dim["date"].dt.month
dim["month_name"]  = dim["date"].dt.month_name()          # "January"
dim["week"]        = dim["date"].dt.isocalendar().week.astype(int)
dim["day_of_week"] = dim["date"].dt.dayofweek + 1         # pandas 0=Mon → shift to ISO 1=Mon
dim["day_name"]    = dim["date"].dt.day_name()            # "Monday"
dim["is_weekend"]  = dim["date"].dt.dayofweek >= 5        # Sat=5, Sun=6

dim["date"] = dim["date"].dt.date    # strip the time component → pure DATE to match the PK
dim.to_sql("dates", engine, if_exists="append", index=False)
print(f"seeded {len(dim)} dates: {dim.date.min()} → {dim.date.max()}")
# seeded 731 dates: 2024-01-01 → 2025-12-31
```

Three details that matter:
- **731, not 730.** 2024 is a leap year (366 days) + 2025 (365) = 731. If you ever see 730,
  a leap day went missing.
- **`+ 1` on `day_of_week`.** pandas numbers weekdays 0–6 (Monday=0); your Phase 1 schema
  documented ISO 1–7 (Monday=1). The shift reconciles the two conventions. Small, but the
  kind of off-by-one that silently mislabels every Monday if you skip it.
- **`.dt.date` at the end.** `date_range` produces full timestamps (`2024-01-01 00:00:00`);
  the PK column is `DATE`. Stripping the time makes the pandas value match the column type
  exactly, so the join from `orders.order_date` lands cleanly. (This is the same "reduce to a
  plain date only once it's safe" idea Phase 3 applies to the parsed order dates.)

The whole point of pre-computing these: Phase 4's "sales by quarter" becomes a plain join +
`GROUP BY quarter`, with no per-row date math at query time. You paid the computation once,
here.

---

## Step 3 — Generate the orders (clean first), then plant the mess

This is the heart of the phase. The strategy is deliberately two-staged: **build a perfectly
clean set of orders, then deface it.** Keeping "generate" and "corrupt" as separate passes
means each planted defect is one small, isolated, reviewable function — and each maps to
exactly one Phase 3 cleaner.

### 3a — Deterministic seed + read the dimensions back

```python
rng = np.random.default_rng(42)     # move #1: fixed seed → identical data every run
engine = get_engine()

cust = pd.read_sql("SELECT customer_id, channel FROM customers", engine)
prod = pd.read_sql("SELECT product_id, product_name, default_unit_cost, "
                   "default_unit_price FROM products", engine)
days = pd.read_sql("SELECT date FROM dates", engine)["date"]
```

Everything downstream draws from `rng`, so the entire 12,500-row file is a pure function of
the seed `42`. Change nothing, rerun, get byte-identical output — which is what lets Phase
3's verified counts (374 quarantined, 4,964 orders) stay stable. And, move #2 again: the
orders are built from `customer_id`/`product_id` *read back from the DB*, so every reference
is real by construction.

### 3b — Realistic ≠ uniform: weight the randomness

Pure uniform random draws would make a *statistically dead* dataset — every customer equal,
every day equal — and Phase 4's dashboards would show flat, patternless bars. Real
distribution businesses are lopsided, so bake that lopsidedness in:

```python
N_ORDERS = 5000

# a couple of customers dominate (Pareto-ish), the rest trail off — not uniform
cust_weights = np.array([0.28, 0.22, 0.15, 0.12, 0.10, 0.08, 0.05])[:len(cust)]
cust_weights = cust_weights / cust_weights.sum()
order_customer = rng.choice(cust.customer_id, size=N_ORDERS, p=cust_weights)

# weekends are quieter than weekdays
is_weekend  = pd.to_datetime(days).dt.dayofweek.to_numpy() >= 5
day_weights = np.where(is_weekend, 0.4, 1.0)
day_weights = day_weights / day_weights.sum()
order_dates = rng.choice(np.array(days), size=N_ORDERS, p=day_weights)
```

`rng.choice(..., p=weights)` draws *with* those probabilities, so the top customer really
does place ~28% of orders and Saturdays really are ~60% lighter. This is the "realistic
structure so believable patterns emerge" principle — the same reason Phase 3's *generated*
logistics facts use gamma-distributed delays instead of flat noise.

### 3c — Build the clean lines: 1–4 products per order, price wobble

```python
orders = pd.DataFrame({
    "order_id":    np.arange(1, N_ORDERS + 1),   # in-memory link ONLY — NOT the DB's id (see below)
    "customer_id": order_customer,
    "order_date":  order_dates,
})

lines = []
for oid in orders.order_id:
    n = rng.integers(1, 5)                        # 1..4 distinct products this order
    picks = rng.choice(prod.index, size=n, replace=False)
    for pi in picks:
        p = prod.loc[pi]
        qty   = int(rng.integers(5, 60))
        # price wobbles ±15% around list → transacted price ≠ list price, realistically
        price = round(float(p.default_unit_price) * rng.uniform(0.85, 1.15), 2)
        lines.append({"order_id": oid, "product": p.product_name,   # NAME, not id — mess lives here
                      "qty_cartons": qty, "unit_price": price,
                      "unit_cost": float(p.default_unit_cost)})
lines = pd.DataFrame(lines)
messy = lines.merge(orders, on="order_id").copy()   # flatten to one row per line (denormalized)
```

Two decisions that echo forward:
- **`order_id` here is `np.arange(1, 5001)` — a purely in-memory link, *not* the database's
  IDENTITY id.** It exists only to tie each line back to its order inside this script. The
  entire drama of Phase 3, Step 4 (the "surrogate-key boundary") is about reconciling *this*
  number with the one Postgres eventually assigns. It's fine to reuse 1–5000 as the key later
  precisely because you generated it here as a dense, clean, one-shot run.
- **The export carries the product `product` *name*, never `product_id`.** That's on purpose:
  a real accounting export names the product in text, and the messed-up text is what Phase
  3's canonicalization step has to translate back into an id. The mess lives in the name
  column by design.
- **±15% price wobble** makes `unit_price` a *transacted* price that differs from the list
  price — exactly why Phase 1 put `unit_price` on the line, not the product.

At this point `messy` is a clean, denormalized, line-grain table. Now you break it — four
times, in order.

---

## Step 4 — Plant the four defects (each the inverse of a Phase 3 cleaner)

The order matters: Phase 3's cleaning table walks these in the same sequence, so plant them
in the sequence you'll un-plant them.

### Defect 1 — three mixed date formats
```python
def messy_date(d):
    d = pd.Timestamp(d)
    style = rng.integers(0, 3)
    if style == 0: return d.strftime("%Y-%m-%d")     # 2024-03-15   (ISO)
    if style == 1: return d.strftime("%d/%m/%Y")     # 15/03/2024   (day-first!)
    return d.strftime("%B %d, %Y")                   # March 15, 2024
messy["order_date"] = messy["order_date"].map(messy_date)
```
Three formats, chosen at random per row, turning a clean date column into **text**. The
day-first slash format is the landmine: `01/05/2024` means 1 May, but a naïve parser assumes
month-first and reads it as 5 January. Phase 3, Step 2a is the exact antidote —
detect-format-per-row, parse each with its own explicit format string. You plant the trap
here so that fix has something to defuse.

### Defect 2 — four spelling / casing / word-order variants
```python
def messy_name(name):
    style = rng.integers(0, 4)
    if style == 0: return name                                        # "Gala Apple"  (clean)
    if style == 1: return name.lower() + " "                          # "gala apple " (case + trailing space)
    if style == 2: return name.upper()                                # "GALA APPLE"
    return f"{name.split()[-1]} - {' '.join(name.split()[:-1])}"      # "Apple - Gala" (reordered)
messy["product"] = messy["product"].map(messy_name)
```
Four ways to write the same product. Case and whitespace are easy to normalize; the
**word-order** variant (`"Apple - Gala"`) is the one that defeats a naïve lowercase-and-trim
and forces Phase 3's *canonical form* (sort the tokens) approach. It's also the exact seam
where Phase 5/6's embeddings earn their place — when the variants outrun what a token-sort
rule can reach.

### Defect 3 — money stored as text with an `RM` prefix
```python
def messy_money(x):
    return f"RM {x:,.2f}" if rng.random() < 0.7 else str(x)   # ~70% "RM 63.76", ~30% bare "63.76"
messy["unit_price"] = messy["unit_price"].map(messy_money)
```
About 70% of prices become strings like `"RM 63.76"` (note the `,` thousands separator the
format *could* insert), the rest bare number-strings. Either way the column is now **text**,
so `.astype(float)` on it crashes on the first `RM`. Phase 3's `clean_money` — strip
everything but digits/dot/minus, then convert — is the inverse. (`unit_cost` is left clean, so
Phase 3 can show that its money cleaner is safe to run even on an already-clean column.)

### Defect 4 — ~3% blank quantities
```python
blank_idx = rng.choice(messy.index, size=int(len(messy) * 0.03), replace=False)
messy.loc[blank_idx, "qty_cartons"] = None
```
3% of `qty_cartons` cells blanked at random — **374** rows on this seed. This is the one
that isn't a *formatting* problem but a *missing-data* problem, and it's deliberately the
defect Phase 3 spends the most words on: the decision of what to do with a row that's missing
a number you can't invent (quarantine it — never impute a financial fact). You plant the
blanks; Phase 3 makes the senior-level call about them.

### Then: reorder columns and wrap in a junk banner
```python
messy = messy[["order_date", "customer_id", "product", "qty_cartons",
               "unit_price", "unit_cost", "order_id"]]   # a human's export order, not your schema's

os.makedirs("data", exist_ok=True)
with pd.ExcelWriter("data/accounting_export.xlsx", engine="openpyxl") as w:
    messy.to_excel(w, sheet_name="Orders", startrow=2, index=False)   # leave rows 0–1 for junk
    ws = w.sheets["Orders"]
    ws["A1"] = "Chop Tong Guan Sdn Bhd — Sales Export"
    ws["A2"] = "Generated by AutoCount · CONFIDENTIAL"
print(f"wrote {len(messy)} messy order lines to data/accounting_export.xlsx")
# wrote 12491 messy order lines to data/accounting_export.xlsx
```
`startrow=2` pushes the real header down to row index 2 and leaves two rows for a decorative
title/subtitle banner — the "Export to Excel" cruft every real accounting system emits. This
is the `skiprows=2` that Phase 3, Step 1 opens with. The column reorder mimics how a human
would lay out an export (dates first, the internal `order_id` shoved to the end), *not* your
schema's order — one more small realism that the ETL has to not care about.

---

## Step 5 — Why the two logistics facts stay empty

`deliveries` and `storage_costs` are **not** generated in this phase, and that's a modelling
decision worth being able to defend. Both reference **real order/product IDs** —
`deliveries.order_id`, `storage_costs.product_id` — and those real IDs don't exist until the
orders have actually been loaded into Postgres (which happens in Phase 3, through the
surrogate-key dance). Generate deliveries now and you'd be inventing foreign keys to orders
that don't exist yet — the exact "referential integrity by construction" rule, violated. So
they wait until Phase 3, Step 5, when there are real IDs to point at.

This also mirrors the real business (see Phase 3's note): CTG runs an accounting system, so
an *orders* export exists to clean — but tracks deliveries on paper / in a WhatsApp group, so
there's nothing digital to hand you, and the logistics facts must be synthesized against real
IDs rather than cleaned from a source.

---

## Step 6 — Make it one idempotent command

Every `to_sql(..., if_exists="append")` above *adds* rows. Run the scripts twice and you
double the dimensions. So the `seed` target wipes first, then rebuilds — the same
reset-then-load discipline Phase 3's `etl` target uses:

```make
seed:
	psql "$(DATABASE_URL_PLAIN)" -c "TRUNCATE order_lines, orders, deliveries, storage_costs, dates, customers, products, suppliers RESTART IDENTITY CASCADE;"
	uv run python src/seed_dimensions.py
	uv run python src/generate_date_dim.py
	uv run python src/generate_messy_orders.py
```

- **`TRUNCATE … RESTART IDENTITY CASCADE`** empties *all* tables, resets every IDENTITY
  counter to 1 (so a reseed gives identical IDs), and `CASCADE` follows FKs so the order of
  truncation doesn't fight the constraints.
- **The script order is the load order from Phase 1:** dimensions before facts, and among
  dimensions, `suppliers` before `products` (the FK). `dates` before anything that references
  a day. Run them out of order and the FKs reject you — the load-order law, enforced.

`make seed` is now the one command that rebuilds the entire starting state deterministically
from empty. Keep it **separate** from Phase 3's `make etl`: `seed` rebuilds dimensions +
regenerates the source export; `etl` (later) only touches the four fact tables. You run `make
seed` once (or when you want fresh source), then `make etl` as often as you like without
disturbing the dimensions it reads from.

Add the generated artifact to `.gitignore` — `data/*.xlsx`. The **generator is the source of
truth**, not its output; anyone can reproduce the exact file from the seeded code, so the
464 KB binary doesn't belong in git.

---

## Step 7 — Verify

```fish
make psql
```
```sql
-- 1. dimensions filled to the expected counts
SELECT 'suppliers' t, count(*) FROM suppliers
UNION ALL SELECT 'products',  count(*) FROM products
UNION ALL SELECT 'customers', count(*) FROM customers
UNION ALL SELECT 'dates',     count(*) FROM dates;
-- expect 6 / 8 / 7 / 731

-- 2. the products→suppliers FK resolved to the RIGHT supplier (not a guessed number)
SELECT p.product_name, s.supplier_name
FROM products p JOIN suppliers s USING (supplier_id)
ORDER BY p.product_name;
-- expect e.g. Gala Apple → Rockit Global, Navel Orange → Sunkist Growers

-- 3. the date dimension spans exactly two years, no gaps
SELECT min(date), max(date), count(*) FROM dates;
-- expect 2024-01-01 | 2025-12-31 | 731

-- 4. facts are still empty by design
SELECT (SELECT count(*) FROM orders)     AS orders,
       (SELECT count(*) FROM deliveries) AS deliveries;
-- expect 0 | 0
\q
```

Then confirm the export exists and carries its planted defects:

```fish
ls -lh data/accounting_export.xlsx      # ~464K
```
```python
import pandas as pd
df = pd.read_excel("data/accounting_export.xlsx", sheet_name="Orders", skiprows=2)
print(df.shape)                          # (12491, 7)
print(df["unit_price"].head())           # a mix of "RM 63.76" and bare "63.76" — text
print(df["order_date"].head())           # three formats mixed
print(int(df["qty_cartons"].isna().sum()))  # 374 blanks
print(sorted(df["product"].str.strip().str.lower().unique())[:6])  # casing/reorder variants visible
```

If the dimensions show **6 / 8 / 7 / 731**, the products join to the right suppliers, the
facts are empty, and the xlsx reads back as **(12491, 7)** with the four defects present,
Phase 2 is done: the honest core is seeded, the mess is manufactured and reproducible, and
Phase 3 has a genuinely dirty file to clean.

---

## Step 8 — Commit

```fish
git add src/seed_dimensions.py src/generate_date_dim.py src/generate_messy_orders.py Makefile .gitignore
git commit -m "feat(data): seed dimensions + generate deliberately messy accounting export"
```

(`feat(data)` for the generators; a separate `chore:` for the Makefile `seed` target and
`.gitignore` if you prefer to keep housekeeping apart.) Tell me when to tick the PROGRESS.md
box, and we'll move to Phase 3 — cleaning this export into the fact tables.

---

### What's been verified

Confirmed against your running Postgres and the generated file: the dimensions seed to
**6 suppliers, 8 products, 7 customers, 731 dates** (2024-01-01 → 2025-12-31, no gaps); the
products→suppliers FK resolves by the read-back-IDs pattern rather than hardcoded numbers, so
each product attaches to the correct supplier regardless of assigned id; the two logistics
facts remain empty by design; and `data/accounting_export.xlsx` (~464 KB) contains **12,491
line rows across 5,000 in-memory order_ids**, with the real header at row index 2
(`skiprows=2`) and all four planted defects present — three mixed date formats, `RM`-prefixed
vs bare money strings, four casing/spacing/reorder product-name variants, and **374** blank
`qty_cartons` (~3%). `make seed` rebuilds the whole starting state deterministically from
empty via `TRUNCATE … RESTART IDENTITY CASCADE`.
