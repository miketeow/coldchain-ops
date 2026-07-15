# Phase 4 — SQL analytics views (the semantic layer)

> **View** = a *saved query* that behaves like a table. You give a `SELECT` a name; from
> then on anyone can `SELECT * FROM that_name` and get the query's result, freshly
> computed, without knowing the joins and formulas underneath. It stores **no data of its
> own** — it's a definition, not a copy.

**Goal:** end this phase with three views — `v_sales_margin`, `v_delivery_performance`,
`v_storage_cost` — that turn your raw star schema into three clean, business-ready
tables, each with the money/logistics formulas already computed and the dimensions
already joined on. Nothing new is *generated* here; you're packaging what Phase 3 loaded
into shapes that are easy to query. This is also the layer Phase 6 (LLM/vector
intelligence) will read from — a natural-language query feature is only as trustworthy as
the SQL definitions underneath it, same reason a dashboard would have needed this.

Phase 3 was about *getting the data right*. Phase 4 is about *making it easy to ask
questions of* — the layer between "correct rows in Postgres" and "a number anyone,
human or LLM, can trust without re-deriving it."

Two table types from Phase 1, one more term for this phase: a **dimension** describes a
thing (customer, product, date); a **fact** records an event (an order line, a delivery,
a storage charge); a **view** is neither — it's a lens that joins facts to dimensions and
computes the numbers people actually ask for (revenue, margin, delay, cost).

## The moves
1. **A view is a saved query, not a copy.** Define each business number *once*, in SQL,
   so every dashboard and query reads the same definition instead of re-inventing it.
2. **One grain per view.** Each view sits at the finest grain of *one* fact. Never blend
   two facts of different grains into a single view — it silently multiplies rows and
   double-counts (the "fan-out" trap).
3. **Keep the finest useful grain; let the consumer aggregate.** Don't pre-sum to
   "margin by month" in the view — that locks every downstream query into that one slice.
   Ship the line-level rows and let whoever reads the view (a query, a script, Phase 6's
   LLM layer) roll them up any way it likes.
4. **Business rules live in the view, not wherever the data gets consumed next.** The
   margin formula and the "on-time" definition belong in SQL where they're written once
   and audited, not copied into every downstream query or script.

---

## Step 0 — Pre-flight

Phase 4 reads what Phase 3 loaded. Confirm the facts are there and note the numbers this
phase must reproduce — if a view's total doesn't match these, the view's joins or formula
are wrong.

```fish
cd coldchain-ops
docker compose ps          # expect coldchain-db running
make psql
```
```sql
SELECT (SELECT count(*) FROM order_lines)   AS lines,       -- expect 12117
       (SELECT count(*) FROM deliveries)    AS deliveries,  -- expect 4964
       (SELECT count(*) FROM storage_costs) AS storage;     -- expect 4065

-- the three totals your views must reproduce exactly:
SELECT round(sum((unit_price-unit_cost)*qty_cartons),2) AS total_margin  -- 6031937.59
FROM order_lines;
\q
```

This phase adds one migration:
- a goose migration that creates the three views (SQL only — no data moves).

No new dependencies. Everything is plain `psql` and `goose`.

---

## Step 1 — What a view is, and why you want one

### Chapter 0: no view — repeat the query everywhere

Suppose you skip views. Every time someone wants margin by channel, they write the join
by hand — in a BI tool's custom-SQL box, in a probe script, in a colleague's notebook:

```sql
SELECT c.channel,
       sum((ol.unit_price - ol.unit_cost) * ol.qty_cartons) AS margin
FROM order_lines ol
JOIN orders    o ON o.order_id    = ol.order_id
JOIN customers c ON c.customer_id = o.customer_id
GROUP BY c.channel;
```

It works. It also breaks in slow, expensive ways as soon as there's more than one copy:

- **The formula drifts.** Margin is `(unit_price - unit_cost) * qty_cartons`. The day
  someone needs to subtract a delivery surcharge, they fix *their* copy. The other four
  copies now disagree, and two dashboards quietly show different "margin" for the same
  month. Nobody can say which is right.
- **The rules are invisible.** A new analyst doesn't know that "on-time" means "within 2
  hours of ETA" (Step 3), or which `status` counts as a real sale. That knowledge lives
  in people's heads and in scattered SQL, not anywhere you can point to.
- **The joins get re-derived (and mis-derived).** Someone forgets the `dates` join, or
  joins `deliveries` into a margin query and double-counts. Each hand-written copy is a
  fresh chance to get the grain wrong.

### The fix: name the query once

A **view** gives that query a name. Define it once:

```sql
CREATE VIEW v_sales_margin AS
SELECT ... ;   -- the join + formula, written one time
```

and from then on everyone just:

```sql
SELECT channel, sum(margin) FROM v_sales_margin GROUP BY channel;
```

The formula, the joins, the business rules all live in *one* place. Change the margin
definition once and every dashboard that reads the view updates together. This is the
first brick of a **semantic layer** — the agreed, single-source definitions of "revenue,"
"margin," "on-time," "storage cost" that the whole business shares. That phrase is worth
knowing for the interview: *"I put the business metrics in database views so the
definitions live in one audited place, not copied across every workbook."*

### One decision up front: plain view vs materialized view

- A **plain view** stores no data. Every time you query it, Postgres re-runs the
  underlying `SELECT`. Always fresh, zero storage, but it pays the join cost on every
  read.
- A **materialized view** stores the result like a real table. Reads are instant, but the
  data is a *snapshot* — stale until you run `REFRESH MATERIALIZED VIEW`.

For this project the underlying joins run over ~12,000 rows and return instantly, so a
**plain view** is right — always live, nothing to refresh, no staleness to reason about.
You reach for *materialized* only when a query is genuinely expensive (millions of rows,
heavy aggregation) and a dashboard feels slow, and you can accept a refresh step. Naming
that trade-off — "I used plain views because the data's small and freshness matters more
than read latency; I'd materialize if the joins got expensive" — is exactly the judgment
the role wants. We use plain views for all three.

---

## Step 2 — `v_sales_margin` (the money view)

### The grain decision (same "what is one row?" question as Phase 3)

`order_lines` is the finest money-carrying fact: one row per product per order. The view
sits at **that same grain** — one row per order line — and just *attaches* the dimensions
and *computes* the money columns. It does **not** pre-aggregate.

Why keep it at line grain instead of summing to, say, channel × month right here? Because
the moment you pre-aggregate, you've decided *for* every downstream consumer what it's
allowed to ask. Ship line-level rows and any query — by channel, by category, by region,
by week, any combination — can roll them up from the one view. This "keep it granular,
let the consumer aggregate" shape is the standard BI pattern (sometimes called a "one big
table"), and it applies just as much to an LLM generating SQL against this view in Phase
6 as it would to a dashboard.

### The view

```sql
CREATE OR REPLACE VIEW v_sales_margin AS
SELECT
    ol.order_line_id,
    o.order_id,
    o.order_date,
    d.year,
    d.quarter,
    d.month,
    d.month_name,
    c.channel,
    c.region,
    c.city,
    p.category,
    p.product_name,
    p.brand,
    ol.qty_cartons,
    ol.unit_price,
    ol.unit_cost,
    ol.unit_price                    * ol.qty_cartons AS revenue,
    ol.unit_cost                     * ol.qty_cartons AS cost,
    (ol.unit_price - ol.unit_cost)   * ol.qty_cartons AS margin
FROM order_lines ol
JOIN orders    o ON o.order_id    = ol.order_id
JOIN customers c ON c.customer_id = o.customer_id
JOIN products  p ON p.product_id  = ol.product_id
JOIN dates     d ON d.date        = o.order_date;
```

**What the joins do, and why they're safe.** Each `JOIN` walks one foreign key back to a
dimension to pull descriptive columns onto the line: through `orders` for the date and
customer link, `customers` for channel/region, `products` for category/name, and `dates`
for the calendar breakdown (year/quarter/month) so any consumer gets ready-made time
buckets instead of computing them from a raw date.
These are plain (inner) joins, and inner joins *drop* a row if the match is missing — but
Phase 3's Step 7 already proved there are **0 orphan lines and 0 missing dates**, so
nothing drops. That earlier verification is exactly what lets you use inner joins here
without losing rows.

**What one row looks like, before → after.**

BEFORE — a raw `order_lines` row (just IDs and numbers, no context):

| order_line_id | order_id | product_id | qty_cartons | unit_price | unit_cost |
|---|---|---|---|---|---|
| 8801 | 4012 | 3 | 10 | 63.76 | 40.00 |

AFTER — the same row in `v_sales_margin` (context attached, money computed):

| order_date | month_name | channel | region | category | product_name | qty | unit_price | unit_cost | revenue | cost | margin |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2024-03-15 | March | Hypermarket | Central | Berries | Red Grapes | 10 | 63.76 | 40.00 | 637.60 | 400.00 | 237.60 |

Whatever reads this view next never has to know the joins or the margin formula — it
reads finished columns.

### The teaching trap: additive vs non-additive measures

`revenue`, `cost`, and `margin` are **additive** — summing them across any set of rows is
meaningful (`sum(margin)` over a month, a channel, everything, all correct). That's why
they belong in the view at line grain.

A **margin percent** (`margin / revenue`) is **not** additive, and this is where people
get burned. If you computed `margin_pct` per line and then *averaged* those per-line
percentages, you'd get nonsense — the average of ratios is not the ratio of sums. A tiny
1-carton line at 50% margin would count as much as a 500-carton line at 20%. The correct
margin percent is `sum(margin) / sum(revenue)` computed *after* aggregation.

So the rule: **put additive measures in the view; compute ratios downstream, at the
aggregate level.** Concretely, don't add a `margin_pct` column here — whoever queries the
view computes `SUM(margin)/SUM(revenue)` themselves. (For reference, the correct overall
figure is `6,031,938 / 19,832,820 ≈ 30.4%`.) Being able to explain *why* you left margin
percent out of the view is a stronger signal than adding it would be.

**Verified:** the view body returns **12,117 rows**, `sum(revenue) = 19,832,819.59`,
`sum(margin) = 6,031,937.59` — matching the raw `order_lines` totals exactly, confirming
the joins neither dropped nor duplicated a single line.

---

## Step 3 — `v_delivery_performance` (the logistics view, and the KPI that lies)

### Grain, and a warning about mixing it with Step 2

One delivery per order (Phase 3 built exactly one shipment per surviving order), so this
view is **one row per delivery**. Keep it a *separate* view from `v_sales_margin` — do
**not** be tempted to bolt delivery columns onto the margin view. Their grains differ: an
order has one delivery but *many* order lines. Joining deliveries to order_lines would
repeat each delivery once per line — a 3-line order's single delivery would appear 3
times, and your "average delay" and breach counts would be silently inflated. That's the
**fan-out** trap from move #2. Two grains → two views.

### Chapter 0: the obvious "on-time" definition — and why it reports nonsense

The natural first cut: a delivery is on-time if it arrived by its planned ETA.

```sql
(delivered_at <= planned_eta) AS on_time
```

Run it across all 4,964 deliveries and **0.0%** are on-time. Every single delivery is
"late." That's not a data error — it's a definition error, and a great one to catch.

Recall Phase 2/3: the generator sets `delivered_at = planned_eta + delay_h`, where
`delay_h` is drawn from a gamma distribution that is **always strictly positive**. So
`delivered_at` is *always* at least a sliver after `planned_eta`, and "delivered exactly
at or before the ETA, to the second" is essentially never true. The naive metric calls
everyone late.

But step back — this isn't only a synthetic-data quirk. **Real logistics never means
"on-time" to the second either.** A truck that arrives 90 seconds after a 14-hour ETA is
on-time by any sane standard. Every real operation has a *tolerance* — a service-level
grace window. The naive definition is wrong in production for the same reason it's wrong
here: it treats a hard deadline as if it had zero slack.

### The fix: an SLA grace window

Define on-time as "delivered within a grace window of the ETA." How wide is a **business
decision**, not a coding one — so you pick it deliberately and write it down. Here's what
each choice yields on the real data:

| grace window | on-time % |
|---|---|
| 0 h (naive) | 0.0 |
| 1 h | 36.6 |
| 2 h | 66.1 |
| 4 h | 90.9 |

A 2-hour window on same-day fresh-produce runs is defensible and gives a believable
**66.1%** on-time — a KPI with room to improve, which is what makes a dashboard
interesting. (4 hours would flatter the operation to 90.9%; 0 hours condemns it to 0%.
The number you report is only as honest as the definition behind it — which is precisely
what an interviewer probes when they ask "how did you define on-time?")

```sql
CREATE OR REPLACE VIEW v_delivery_performance AS
SELECT
    dl.delivery_id,
    o.order_id,
    o.order_date,
    c.region,
    c.channel,
    dl.route,
    dl.dispatched_at,
    dl.planned_eta,
    dl.delivered_at,
    round(extract(epoch FROM (dl.delivered_at - dl.planned_eta)) / 3600.0, 2) AS delay_hours,
    (dl.delivered_at <= dl.planned_eta + interval '2 hours')                  AS on_time,
    dl.temp_excursion
FROM deliveries dl
JOIN orders    o ON o.order_id    = dl.order_id
JOIN customers c ON c.customer_id = o.customer_id;
```

Two computed columns carry the logistics KPIs:
- **`delay_hours`** — `delivered_at - planned_eta` is an *interval* (a duration). To chart
  it you want a plain number, so `extract(epoch FROM ...)` converts the interval to
  seconds, `/ 3600` turns seconds into hours, and `round(..., 2)` tidies it. This is the
  delay KPI.
- **`on_time`** — the grace-window rule above, as a true/false column. This is the
  service KPI.
- **`temp_excursion`** — carried straight through; it's the cold-chain breach KPI from
  Phase 3 (a *temperature* failure, independent of lateness).

**Before → after.**

BEFORE — a raw `deliveries` row (timestamps, no interpretation):

| delivery_id | order_id | route | planned_eta | delivered_at | temp_excursion |
|---|---|---|---|---|---|
| 512 | 4013 | Johor Bahru Line | 2024-03-17 01:00 | 2024-03-17 04:30 | true |

AFTER — the same row in `v_delivery_performance` (KPIs computed):

| delivery_id | order_id | region | route | delay_hours | on_time | temp_excursion |
|---|---|---|---|---|---|---|
| 512 | 4013 | South | Johor Bahru Line | 3.50 | false | true |

That delivery was 3.5 h late (outside the 2 h grace → `on_time = false`) *and* breached
temperature — two different failures, now both legible to the dashboard.

**Verified:** the view body returns **4,964 rows**, average `delay_hours = 1.81`, and
`on_time = 66.1%` at the 2-hour grace. Breach rate is **5.8%** overall and rises with
route length exactly as designed — North 4.0%, Central 8.2%, South 12.4% — a real,
actionable geographic pattern baked into the data.

---

## Step 4 — `v_storage_cost` (the warehouse view)

### Grain and the one computed column

`storage_costs` is one row per product per day it was in storage (Phase 3, Step 5b). The
view keeps that grain and adds the number nobody wants to compute by hand: the day's
actual cost.

Recall the table stores the *ingredients*, not the cost: `pallets_stored` and
`cost_per_pallet_day`. The daily cost is their product. Putting that multiplication in the
view means every dashboard reads a finished `daily_cost` instead of re-deriving it (and
risking someone forgetting to multiply by pallets).

```sql
CREATE OR REPLACE VIEW v_storage_cost AS
SELECT
    sc.storage_cost_id,
    sc.cost_date,
    d.year,
    d.month,
    d.month_name,
    p.product_id,
    p.product_name,
    p.category,
    sc.pallets_stored,
    sc.cost_per_pallet_day,
    sc.pallets_stored * sc.cost_per_pallet_day AS daily_cost
FROM storage_costs sc
JOIN products p ON p.product_id = sc.product_id
JOIN dates    d ON d.date       = sc.cost_date;
```

The joins attach the product's name/category (so you can chart cost by category) and the
date's year/month (so you can chart cost over time) — the same "join facts to dimensions"
move as the other two views, just with a different fact.

**Before → after.**

BEFORE — a raw `storage_costs` row:

| storage_cost_id | cost_date | product_id | pallets_stored | cost_per_pallet_day |
|---|---|---|---|---|
| 91 | 2024-01-01 | 3 | 12 | 5.80 |

AFTER — the same row in `v_storage_cost`:

| cost_date | month_name | product_name | category | pallets_stored | cost_per_pallet_day | daily_cost |
|---|---|---|---|---|---|---|
| 2024-01-01 | January | Red Grapes | Berries | 12 | 5.80 | 69.60 |

**Verified:** the view body returns **4,065 rows** and `sum(daily_cost) = 298,337.40`
across the full 2024-01-01 → 2025-12-31 span — matching the raw computation, so no row was
dropped or duplicated by the joins.

---

## Step 5 — Package the three views as a migration

The views are schema, and schema changes go through goose (project convention) so the
whole thing is versioned and re-buildable — the same discipline as your Phase 1 tables.
Create a migration file:

```fish
make db-create name=add_analytics_views
```

That drops a timestamped stub in `migrations/`. Fill it in — the three `CREATE`s under
`Up`, and the matching `DROP`s under `Down` (reverse order, so a rollback removes them
cleanly):

```sql
-- +goose Up
CREATE OR REPLACE VIEW v_sales_margin AS
SELECT ... ;              -- from Step 2

CREATE OR REPLACE VIEW v_delivery_performance AS
SELECT ... ;             -- from Step 3

CREATE OR REPLACE VIEW v_storage_cost AS
SELECT ... ;             -- from Step 4

-- +goose Down
DROP VIEW IF EXISTS v_storage_cost;
DROP VIEW IF EXISTS v_delivery_performance;
DROP VIEW IF EXISTS v_sales_margin;
```

Two deliberate choices:
- **`CREATE OR REPLACE VIEW`** (not bare `CREATE VIEW`) so that while you're iterating on
  a view's columns you can re-apply without a "already exists" error. (Caveat: `OR
  REPLACE` lets you *add* columns at the end but not rename/reorder/retype existing ones —
  for that you drop and recreate. Not a concern on first creation.)
- **`DROP ... IF EXISTS` in reverse order** in `Down` — the mirror image of `Up`, so
  `make db-rollback` leaves the database exactly as it was before.

A view stores no rows, so this migration is **instant** — it registers three query
definitions, nothing is copied or scanned. Apply it:

```fish
make db-migrate
make db-status     # the new migration shows as applied
```

---

## Step 6 — Verify it worked

`make psql`, then confirm each view exists, sits at the right grain, and reproduces the
Phase-0 totals. If a total is off, the view's join or formula is wrong — not the data.

```sql
-- the three views are registered
SELECT table_name FROM information_schema.views WHERE table_schema='public' ORDER BY 1;
-- expect: v_delivery_performance / v_sales_margin / v_storage_cost

-- 1. sales margin: grain + additive totals match the raw facts
SELECT count(*) AS rows, round(sum(revenue),2) AS revenue, round(sum(margin),2) AS margin
FROM v_sales_margin;
-- expect: 12117 | 19832819.59 | 6031937.59

-- 2. margin by channel (a slice you'd want any consumer of this view to get right)
SELECT channel, round(sum(margin),2) AS margin
FROM v_sales_margin GROUP BY channel ORDER BY margin DESC;
-- expect Hypermarket highest, Ecommerce lowest; 4 channels

-- 3. delivery performance: grain + the KPIs
SELECT count(*) AS rows,
       round(avg(delay_hours),2)          AS avg_delay_h,   -- expect ~1.81
       round(100.0*avg(on_time::int),1)   AS on_time_pct,   -- expect 66.1
       round(100.0*avg(temp_excursion::int),1) AS breach_pct -- expect 5.8
FROM v_delivery_performance;
-- expect rows = 4964

-- 4. breach rate rises with route length (a real, actionable pattern in the data)
SELECT region, round(100.0*avg(temp_excursion::int),1) AS breach_pct
FROM v_delivery_performance GROUP BY region ORDER BY breach_pct;
-- expect North < Central < South (4.0 / 8.2 / 12.4)

-- 5. storage cost: grain + total
SELECT count(*) AS rows, round(sum(daily_cost),2) AS total_cost
FROM v_storage_cost;
-- expect: 4065 | 298337.40
```

If the counts and totals match, Phase 4 is done: the raw star schema now has a clean
semantic layer on top for anything downstream to read — including Phase 6's LLM layer.

---

## Step 7 — Commit

```fish
git add migrations/*_add_analytics_views.sql
git commit -m "feat(db): add sales/delivery/storage analytics views"
```

Tell me when you want the PROGRESS.md box ticked, and we'll move to Phase 6 (pgvector /
LLM intelligence layer).

---

### What I verified while writing this (so you know the SQL is sound before you type it)

Run read-only against your live database, by executing each view's `SELECT` body as an
inline subquery (no view was created, nothing in your DB changed):
- `v_sales_margin` body → **12,117 rows**, revenue **19,832,819.59**, margin
  **6,031,937.59** — exactly the raw `order_lines` totals, so the four joins neither drop
  nor duplicate a line.
- `v_delivery_performance` body → **4,964 rows**, avg delay **1.81 h**, on-time **66.1%**
  at a 2-hour grace, breach **5.8%** (North 4.0 / Central 8.2 / South 12.4).
- `v_storage_cost` body → **4,065 rows**, total `daily_cost` **298,337.40**.

**On you to run** (your step, not mine): creating the actual views via the goose
migration and applying it. The column names and types they depend on are confirmed
against the live schema (`order_line_id`, `delivery_id`, `storage_cost_id` surrogate keys
present; `unit_price`/`unit_cost` `NUMERIC`; `delivered_at`/`planned_eta` `timestamptz`;
`dates.date` the PK the views join on), so they should apply cleanly — Step 6's queries
are what prove it once you've typed and run them yourself.
