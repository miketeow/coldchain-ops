# Phase 3 — ETL: clean the messy export into Postgres, then generate the logistics facts

> **ETL** = *Extract, Transform, Load* — pull data out of a source (extract), fix and
> reshape it (transform), write it into your database (load). That's the arc of this
> phase.

**Goal:** end this phase with all four fact tables filled in and correctly linked —
`orders`/`order_lines` reconstructed from the messy `accounting_export.xlsx`,
`deliveries`/`storage_costs` generated fresh against the **real** IDs those orders end
up with. Re-runnable with one command (`make etl`), accounts for every row it throws
away, shaped to run overnight with nobody watching.

Quick reminder of the two table types from Phase 1: a **dimension** describes a thing
(a customer, a product, a date); a **fact** records an event that happened to those
things (an order, a delivery, a storage charge). Facts point back at dimensions.

This is the phase the job actually pays for. Today, someone at CTG does this by hand
every month: opens the export, fixes the dates, strips the `RM` off prices, reconciles
the product spellings, retypes it somewhere queryable. You're writing the script that
does that with no human in the loop. Steps 1–4 are pandas; Step 5 reuses the same
Python-writes-to-Postgres path from Phase 2.

Each cleaning step is the exact reverse of a flaw ("defect") you deliberately planted
in Phase 2. *I broke it on purpose, here is the exact line that un-breaks it.*

## The six moves
1. **Read defensively — the source lies about its shape.** Inspect before you trust.
2. **One transform per defect.** Each cleaning step maps to exactly one flaw, so when a
   number looks wrong later you know which step to suspect.
3. **Normalize-then-join.** Reduce both sides of a fuzzy match to one agreed-on standard
   form (a **canonical form**), then do an ordinary exact match. This is the honest,
   rule-based first version of a "fuzzy join" (matching things that are *almost* equal)
   — and it's exactly where Phase 6's embedding-based approach will earn its place.
4. **Quarantine, don't fabricate.** Bad rows go to a rejects file with a reason. You
   never invent a missing value to save a row — in a money pipeline that's fabricating
   revenue.
5. **Header/detail split + key survival.** The export is one row per product line; the
   model wants a header row plus separate detail lines. Splitting is easy — keeping each
   line tied to the right order as the ID numbers change underneath you is the real
   lesson.
6. **Idempotent + unattended.** *Idempotent* = "safe to run more than once — running it
   twice leaves the same result as running it once." A script you can only run once by
   hand is a demo; an idempotent, scheduled one is automation.

---

## Step 0 — Pre-flight

```fish
cd coldchain-ops
docker compose ps                 # expect coldchain-db running
make psql
```
```sql
SELECT 'suppliers' t, count(*) FROM suppliers
UNION ALL SELECT 'products',  count(*) FROM products
UNION ALL SELECT 'customers', count(*) FROM customers
UNION ALL SELECT 'dates',     count(*) FROM dates
UNION ALL SELECT 'orders',    count(*) FROM orders;
-- expect 6 / 8 / 7 / 731 / 0
\q
```
```fish
ls -lh data/accounting_export.xlsx     # ~474K
```

This phase adds two files to `src/`:
- `src/etl_orders.py` — read the xlsx, clean it, split it, load `orders` + `order_lines`.
- `src/generate_logistics.py` — generate `deliveries` + `storage_costs` against the real IDs.

No new dependencies — `pandas`, `openpyxl`, `sqlalchemy`, `psycopg`, `numpy` are already
installed.

**The shared engine.** To talk to Postgres from Python you need a connection object.
SQLAlchemy (the library underneath pandas' SQL functions) calls this object an
**engine** — the live line to the database that pandas dials through. Your `db.py`
already hands you one, via a function:

```python
# src/db.py  (already exists)
def get_engine():
    return create_engine(os.environ["DATABASE_URL"])
```

So every script this phase starts the same way:

```python
from db import get_engine
engine = get_engine()
```

That import works because running `uv run python src/etl_orders.py` puts `src/` on
Python's search path — the same way your Phase 2 scripts found each other.

---

## Step 1 — Read the export honestly

### Chapter 0: the naive read, and why it breaks

```python
import pandas as pd
df = pd.read_excel("data/accounting_export.xlsx", sheet_name="Orders")
df.head()
```

(`df` is the conventional name for a pandas **DataFrame** — an in-memory table you can
manipulate in code, like a spreadsheet. `.head()` shows the first few rows.)

This runs and hands you garbage. In Phase 2 you wrote the real column headers starting
at the third row, leaving two junk rows on top: a company title and a
"CONFIDENTIAL"-style subtitle — the kind of decorative banner a real accounting export
always has. Pandas grabs **row 0 (the title) as the column names**. The table comes
back with shape `(12493, 7)`, and your first column is literally named after the title
string, with the rest `Unnamed: 1 … Unnamed: 6`.

### The fix: skip to the real header, then look before you leap

```python
import pandas as pd

RAW = "data/accounting_export.xlsx"
df = pd.read_excel(RAW, sheet_name="Orders", skiprows=2)   # skip the 2 junk rows

print(df.shape)            # (12491, 7)
print(df.columns.tolist()) # ['order_date','customer_id','product','qty_cartons','unit_price','unit_cost','order_id']
print(df.dtypes)
print(df.head(3))
```

Now look hard at `df.dtypes` before writing a single transform. A **dtype** is the data
type pandas assigned to a column — integer, decimal, text, date. Pandas guesses the
dtype from the contents, so a surprising dtype is itself a clue that something's messy.
This habit — reading the dtypes first — is what separates *cleaning* data from *hoping*
it's clean.

| Column | dtype (pandas 3.0.4) | Why |
|---|---|---|
| `order_date` | `str` (text) | three different date formats mixed together — pandas can't pick one date type, so it leaves them as text |
| `customer_id` | `int64` (whole number) | kept clean on purpose |
| `product` | `str` (text) | four messy spelling variants per name |
| `qty_cartons` | `float64` (decimal) | 374 blank cells force the whole column to decimal — pandas' "missing" marker (`NaN`, *Not a Number*) only fits in a decimal column |
| `unit_price` | `str` (text) | a mix of `"RM 63.76"` and bare `"63.76"` strings |
| `unit_cost` | `int64` (whole number) | clean, and the costs are whole ringgit, so it reads as integer |
| `order_id` | `int64` (whole number) | the export's own internal link, a clean run of 1–5000 |

---

## Step 2 — Clean each defect, one transform per *injected* defect

Phase 2 injects flaws in this order: dates → product names → money → blank quantities.
Here's the honest 1:1 table, matching that sequence:

| # | Defect (in the order Phase 2 planted it) | Column | Transform |
|---|---|---|---|
| 0 | Junk banner rows | (sheet) | `skiprows=2` — done in Step 1 |
| 1 | Three mixed date formats | `order_date` | detect format per row → parse each with its exact format |
| 2 | 4 spelling/case/word-order variants | `product` | canonical form → match to `products` table |
| 3 | Prices stored as text with `RM` | `unit_price` (`unit_cost` defensively) | strip non-numeric chars → convert to number |
| 4 | ~3% blank quantities (374 rows) | `qty_cartons` | set aside to a rejects file, keep the rest |

### 2a — Dates: the single most valuable transform in this job

#### Chapter 0: the naive parse

```python
df["order_date"] = pd.to_datetime(df["order_date"])
```

This **silently misreads** the ambiguous dates — the worst kind of bug, because nothing
crashes. `15/03/2024` is unambiguous (no 15th month). But `01/05/2024` could be 1 May or
2 January depending on whether the day or the month comes first, and pandas defaults to
**month-first** (the American convention) — even though your Phase 2 generator wrote it
day-first, meaning 1 May. No error. Every dashboard is quietly wrong, and you find out
in the interview.

#### Chapter 1: the fix that looks right, and still breaks

The textbook answer is to resolve the ambiguity explicitly:

```python
df["order_date"] = pd.to_datetime(
    df["order_date"],
    format="mixed",   # parse each value independently — you planted 3 formats
    dayfirst=True,    # read DD/MM/YYYY the Malaysian/UK way
)
```

This looks correct, and it *mostly* works — but check it against a full run and it
isn't. `format="mixed"` doesn't parse the whole column with one rule; it looks at each
cell and **guesses** its format. Combine that with `dayfirst=True`, and the guesser
treats *any* pair of numbers ≤12 as an ambiguous day-vs-month pair — including inside an
ISO string like `2024-06-07`, where the year always comes first and there's no actual
ambiguity. It wrongly applies the day-first swap there too:

```
'2024-06-07' -> 2024-07-06   (wrong — June 7 became July 6)
'2024-05-01' -> 2024-01-05   (wrong — May 1 became January 5)
'01/05/2024' -> 2024-05-01   (correct — this one really was day-first)
```

`format="mixed"` is a documented soft spot in pandas: it's explicitly best-effort and
can misparse silently. This is a general lesson, not just a pandas quirk — no
"guessing" tool, in any language, can resolve genuine ambiguity without extra
information. `01/05/2024` truly could mean either date; the only way to get it right is
to know, in advance, which convention produced it. There's no clever one-liner that
sidesteps that — the fix is to stop guessing.

#### Chapter 2: detect the format per row, don't guess it

You already know there are exactly 3 formats (you wrote the generator). So detect which
one each row is in with a small pattern check, then parse each group with its own
**exact** format string — no ambiguity possible, because you're telling pandas precisely
which digit is which:

```python
iso_mask   = df["order_date"].str.match(r"^\d{4}-\d{2}-\d{2}$")   # 2024-06-07
slash_mask = df["order_date"].str.match(r"^\d{2}/\d{2}/\d{4}$")   # 15/03/2024
long_mask  = ~(iso_mask | slash_mask)                              # "March 15, 2024"

parsed = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
parsed[iso_mask]   = pd.to_datetime(df.loc[iso_mask,   "order_date"], format="%Y-%m-%d",  errors="coerce")
parsed[slash_mask] = pd.to_datetime(df.loc[slash_mask, "order_date"], format="%d/%m/%Y",  errors="coerce")
parsed[long_mask]  = pd.to_datetime(df.loc[long_mask,  "order_date"], format="%B %d, %Y", errors="coerce")

df["order_date"] = parsed
```

New pieces:
- `.str.match(pattern)` checks each string against a **regex** (regular expression — a
  mini pattern language for text) and returns True/False per row. The two patterns here
  are about as simple as regex gets: `^\d{4}-\d{2}-\d{2}$` just means "4 digits, dash, 2
  digits, dash, 2 digits" — no lookaheads, no backreferences, nothing exotic. This isn't
  reaching for a fancy tool; it's the plainest way to say "does this string have this
  shape."
- A **mask** like `iso_mask` is a True/False column the same length as `df`;
  `df.loc[iso_mask, "order_date"]` means "only the rows where the mask is True, in this
  column."
- `pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")` pre-builds an empty date
  column the same length as `df` (`NaT` = *Not a Time*, the missing-value marker for
  dates), which you then fill in three pieces — one per detected format.
- `errors="coerce"` on each branch means: if some value in that group *still* fails to
  parse (a typo, a stray non-date value someone typed by hand), it becomes `NaT` instead
  of crashing the run. You want that — a data-entry surprise should get quarantined, not
  take down the whole pipeline.

**Verified against the real file:** 4,168 ISO rows, 4,076 slash rows, 4,247 long-form
rows — 12,491 total, all accounted for, **0 unparsed**. The two rows that broke under
`format="mixed"` now parse correctly: `2024-06-07` stays `2024-06-07`, `2024-05-01`
stays `2024-05-01`, and the genuinely ambiguous `01/05/2024` still correctly resolves to
1 May.

> **Why not solve this "outside code," with a business rule instead?** In a real
> company, a lot of this mess *is* preventable upstream — a proper order-entry system
> uses a date-picker widget, not a free-text box, so nobody can type an ambiguous date
> (or a stray note in the date column) in the first place. But you don't control CTG's
> upstream process, and even a well-run one eventually hands you data from a partner or
> a legacy system that doesn't follow your rules. So you build the defensive parser
> regardless — not because the business is careless, but because you can't audit what
> you didn't produce. That's why `errors="coerce"` here isn't paranoia; it's the same
> "quarantine, don't fabricate" move as the rest of this phase, just applied to dates.

One more deliberate choice: `df["order_date"]` is left as a proper pandas datetime
column here — **not** reduced to a plain calendar date yet. That reduction (`.dt.date`)
happens later, in Step 2d, after the rows with unparseable dates have been quarantined
out. Doing it in that order means `.isna()` can still find the `NaT` failures on a real
datetime column (where "missing" is unambiguous) rather than on a column that's already
been converted to plain Python date objects.

### 2b — Product names → `product_id` (the normalize-then-join move)

The export has a `product` *name*, in four messy variants per product, and **no
`product_id`** — but `order_lines.product_id` is the foreign key that points at
`products`. (A **foreign key**, or FK, is a database rule saying "the value in this
column must exist as a key in that other table.") So you have to translate name → id.
Three chapters, each failing in a useful way.

#### Chapter 0: join on the raw name

```python
products = pd.read_sql("SELECT product_id, product_name FROM products", engine)
merged = df.merge(products, left_on="product", right_on="product_name", how="left")
merged["product_id"].isna().mean()   # ~0.75 — three-quarters didn't match
```

(A **join**/**merge** glues two tables together by matching a column in one to a column
in the other. `how="left"` keeps every row of `df` and attaches matching `products`
info where it exists, leaving blanks where it doesn't.)

Only rows using the exact official spelling match. `"red grapes "`, `"RED GRAPES"`,
`"Grapes - Red"` all miss. Exact text equality is the wrong tool.

#### Chapter 1: normalize case and whitespace, then join

```python
def norm(s): return s.str.strip().str.lower()
df["pkey"]       = norm(df["product"])
products["pkey"] = norm(products["product_name"])
```

Now `"red grapes "`, `"RED GRAPES"`, `"Red Grapes"` all collapse to `"red grapes"` and
match. But `"Grapes - Red"` → `"grapes - red"` still misses — the **word order** is
different. Lowercasing and trimming fix case and spacing, not reordering.

#### Chapter 2: a canonical form that ignores word order

Two strings name the same product if they contain the same *set* of words, regardless
of order or punctuation. Reduce each name to its sorted words — that sorted form is the
**canonical form**, the one agreed representative all the messy variants boil down to:

```python
import re

def canon(name: str) -> str:
    """'Grapes - Red' and 'red grapes ' both -> 'grapes red'."""
    tokens = re.split(r"[^a-z0-9]+", str(name).lower().strip())
    tokens = [t for t in tokens if t]        # drop empty strings the split leaves at the edges
    return " ".join(sorted(tokens))
```

(A **token** is one word once you've chopped the string up. `re.split(r"[^a-z0-9]+", ...)`
splits wherever a run of non-letter/non-digit characters appears — spaces, dashes,
punctuation all become break points.)

Build the lookup from the dimension (the source of truth), guard against accidents, then
join:

```python
products = pd.read_sql("SELECT product_id, product_name FROM products", engine)
products["canon"] = products["product_name"].map(canon)

# no two different products may boil down to the same canonical form, or you'd
# silently file lines under the wrong product — catch it now, loudly
assert products["canon"].is_unique, "canonical-form collision between products"

lookup = products.set_index("canon")["product_id"].to_dict()
df["canon"]      = df["product"].map(canon)
df["product_id"] = df["canon"].map(lookup)     # blank where no canonical match exists
```

(`.map(canon)` applies the function to every value in the column. `assert` is a
tripwire — if the condition is false, the script stops immediately with your message
instead of limping on and producing a wrong number three steps later. `.to_dict()` turns
the lookup table into a plain Python dict.)

**Verified on the real data:** the collision guard passes (all 8 canonical forms
distinct), and the match rate is **100%, 0 unmatched**. *This is the exact seam where
Phase 6 enters the story:* when variants stop being reachable by rules
(`"grps red"`, `"red grape (seedless)"`), you switch to **embeddings** — turning each
name into a list of numbers that captures its meaning — and measure closeness with
**cosine similarity**. Interview line: "rule-based canonicalization got 100% on these
four variant types; embeddings are what you reach for when the long tail of spellings
outruns the rules."

> **Editor note — a type-checker false positive you'll likely hit here.** If your editor
> flags `df["canon"].map(lookup)` with a "not assignable" error, that's Pylance/Pyright
> (a static type checker), not Python itself — it runs fine. The cause: pandas'
> `df["col"]` could, in principle, return a `DataFrame` instead of a `Series` if a frame
> ever had two columns with the same name, so the type checker conservatively types every
> `df["col"]` access as `Series | DataFrame`. `.map()` means something different on each
> (the `DataFrame.map()` added in pandas 2.1 only accepts a function, not a dict), so the
> checker can't prove your dict-based call is safe even though it is. If you want the
> warning gone: `from typing import cast; df["product_id"] = cast(pd.Series, df["canon"]).map(lookup)`
> — the same idea as TypeScript's `as Type` assertion, telling the checker what you
> already verified by running it. Otherwise it's safe to ignore.

### 2c — Money columns stored as text

#### Chapter 0: the naive cast

```python
df["unit_price"] = df["unit_price"].astype(float)
# ValueError: could not convert string to float: 'RM 63.76'
```

`.astype(float)` is all-or-nothing: one `"RM 63.76"` and the whole column refuses. Strip
the non-numeric characters first, then convert.

#### The fix: a reusable money cleaner

Don't hand-chain `.str.replace("RM", "")….str.replace(",", "")…` — it only removes the
exact symbols you thought of, and a stray `$` or odd space brings the crash back. One
regex that keeps only digits, the decimal point, and a minus sign is robust to any
stray formatting:

```python
def clean_money(s: pd.Series) -> pd.Series:
    """'RM 1,250.00' | '45.50' | 50 | '' -> 1250.00 | 45.50 | 50.0 | NaN."""
    cleaned = (
        s.astype(str)
         .str.replace(r"[^0-9.\-]", "", regex=True)
         .replace("", pd.NA)
    )
    return pd.to_numeric(cleaned, errors="coerce")

df["unit_price"] = clean_money(df["unit_price"])
df["unit_cost"]  = clean_money(df["unit_cost"])   # already clean, but run it for uniformity
```

`errors="coerce"` again means "unparseable becomes `NaN`, not a crash" — lets the
pipeline survive a surprise and quarantine the row rather than dying on row 9,000 of
12,491.

Note: the "comma" in the regex is defensive insurance, not something this data actually
needs — your prices are all double-digit, so the `f"RM {x:,.2f}"` format never actually
inserts a thousands separator on this file (verified: zero commas present). Keeping the
comma-strip costs nothing and covers you if prices ever cross 1,000.

**Verified:** cleaned `unit_price` comes out decimal (`float64`), 0 missing, range
`23.81 … 80.48`.

### 2d — Blank quantities (and quarantined dates): the decision an interviewer actually probes

You planted **374** blanks (~3%) in `qty_cartons`. The code to handle them is trivial;
the *decision* is the senior-level signal. Three options, two of them wrong here:

- **Impute** (fill with a guessed value, e.g. the product's typical quantity)? No — this
  is transactional financial data. Inventing a quantity invents revenue and margin,
  making your dashboard *confidently wrong*. Imputation is for analytics features, never
  for source-of-truth facts.
- **Silently drop them?** No — dropping is fine, doing it *silently* is not. If 3% of
  orders quietly vanish, monthly totals understate and nobody can reconcile against
  accounting.
- **Quarantine and report** — yes. Move bad rows to a holding file with a reason, load
  the clean remainder, log the counts so the run is auditable.

The same policy applies to a row whose date never parsed (Step 2a's `errors="coerce"`
safety net) — it's just as unusable as a missing quantity, so it belongs in the same
rejects pile, not a special case:

```python
df["qty_cartons"] = pd.to_numeric(df["qty_cartons"], errors="coerce")  # stray text qty -> NaN too

# a row is unusable if it lost its quantity, never matched a product, OR its date never parsed
bad_mask = (
    df["qty_cartons"].isna()
    | df["product_id"].isna()
    | df["order_date"].isna()
)

rejects = df[bad_mask].copy()
rejects["reject_reason"] = (
    df.loc[bad_mask, "qty_cartons"].isna().map({True: "null_qty", False: ""})
    + df.loc[bad_mask, "product_id"].isna().map({True: "|unmatched_product", False: ""})
    + df.loc[bad_mask, "order_date"].isna().map({True: "|bad_date", False: ""})
)
rejects.to_csv("data/rejects_orders.csv", index=False)

clean = df[~bad_mask].copy()
clean["qty_cartons"] = clean["qty_cartons"].astype(int)   # safe now: no NaN left
clean["order_date"]  = clean["order_date"].dt.date        # safe now: no NaT left — reduce to a plain date

print(f"rows in: {len(df)}  quarantined: {len(rejects)}  clean: {len(clean)}")
```

(A **mask** is a True/False column the same length as the table; `df[bad_mask]` keeps
the True rows, `df[~bad_mask]` keeps the False ones — `~` means "not.")

**Verified exact numbers:** `rows in: 12491  quarantined: 374  clean: 12117`. On this
particular file, every reject is `null_qty` — 0 rows fail on `bad_date` or
`unmatched_product`, because the date-format detection and product canonicalization
both hit 100%. The `bad_date` guard exists for the data you *haven't* seen yet, not
because this run needed it — the same reasoning as validating input at a system boundary
even when today's input happens to be well-formed.

The `.dt.date` reduction now happens here, on `clean` only, after the bad rows are
already gone — so there's no leftover `NaT` to worry about converting.

> **What the interviewer is really probing:** not "do you know `dropna`," but "what's
> your *policy* for missing source data, and how does someone find out what you
> dropped?" Your answer — "I never impute facts; I quarantine to a rejects file with a
> reason and log the count so it reconciles against the accounting system" — is a senior
> answer to a junior-sounding question.

At the end of Step 2 you have one tidy `clean` table at **line grain** (one row per
product per order) with proper types: `order_date` (date), `customer_id` (int),
`product_id` (int), `qty_cartons` (int), `unit_price` (decimal), `unit_cost` (decimal),
`order_id` (int — still the export's own number).

---

## Step 3 — Split line grain into header (`orders`) + detail (`order_lines`)

### What "grain" means, with a picture

**Grain** answers one question about a table: *if I point at a single row, what
real-world thing is that one row?* Everything in this step follows from getting that
answer straight.

Open `accounting_export.xlsx` and look at one real order — order **4012**, placed by
customer **7** on **2024-03-15**, for three different products. Here are its three rows:

| order_id | order_date | customer_id | product | qty_cartons | unit_price | unit_cost |
|---|---|---|---|---|---|---|
| 4012 | 2024-03-15 | 7 | Red Grapes | 10 | 63.76 | 40 |
| 4012 | 2024-03-15 | 7 | Oranges | 25 | 28.50 | 18 |
| 4012 | 2024-03-15 | 7 | Green Apples | 8 | 41.00 | 30 |

**Three rows for one order.** Point at any single row and you're pointing at *one
product within one order* ("the 10 cartons of red grapes on order 4012"). That is the
**grain**: one row = one order *line*. Compare it to `customers`, where one row is one
customer, or `dates`, where one row is one day — different grain each time.

Grain matters the instant you `SUM` or `COUNT`. `COUNT(*)` on the table above returns
**3** for order 4012 — three *lines*, not three orders. Miscount the grain and every
downstream total is quietly wrong. That's why the word gets drilled before any code.

### Why the same order repeats: denormalization

Look at just the `order_date` and `customer_id` columns above: `2024-03-15` and `7` are
written **three times**. But order 4012 has *one* date and *one* customer — it was
placed once, by one shop, on one day. Those facts belong to the order as a whole, so a
flat spreadsheet copies them onto every line, because a rectangle has nowhere else to
put them.

That copying is **denormalized** data: the same fact stored in many places. The
opposite, **normalized**, means every fact lives in exactly one row, and anything that
needs it *points at it* instead of copying. Your star schema is normalized — and Step 3
is the act of converting the flat export into it. Two concrete reasons this matters:

- **It can't contradict itself.** If you later learn the order was placed on the 16th,
  the denormalized table needs *three* edits and could end up with two lines saying the
  15th and one saying the 16th — an order disagreeing with itself. Normalized, there's
  one cell to change; it *can't* disagree with itself.
- **Totals stay honest.** "How many orders did customer 7 place?" is a plain row count
  on the normalized `orders` table; on the flat export you'd have to remember to count
  *distinct* order_ids or you'd overcount every multi-product order.

Accounting exports always arrive denormalized because the "Export to Excel" button runs
a *join* internally — it glues each line back to its order header and dumps the result
as one flat rectangle. **Step 3 is that join run in reverse**: you regroup the lines
back under their order. That's literally why the code below uses `groupby("order_id")`.

### The code, and what it does to the data

The split is always three moves, in this order: **(1) prove the header columns are
constant within each order → (2) collapse to one header row per order → (3) select the
detail columns.** Move 1 is what earns you the right to do move 2 safely.

```python
# 1. prove every line of an order agrees on its customer and its date
consistency = clean.groupby("order_id")[["customer_id", "order_date"]].nunique()
assert (consistency <= 1).all().all(), "an order has conflicting customer/date across lines"

# 2. collapse each order's lines to one header row
orders = (
    clean.groupby("order_id", as_index=False)
         .agg(customer_id=("customer_id", "first"),
              order_date =("order_date",  "first"))
)
orders["source"] = "accounting_import"   # provenance — records that THIS pipeline produced the row
orders["status"] = "fulfilled"           # the export only ever contains completed sales

# 3. keep every line, only the per-line columns + the order_id pointer
order_lines = clean[["order_id", "product_id", "qty_cartons", "unit_price", "unit_cost"]]

print(len(orders), "orders /", len(order_lines), "lines")
```

**Move 1 — the consistency `assert`.** `groupby("order_id")` piles rows into buckets,
one per order. `.nunique()` counts *distinct* values per bucket. So for our toy order,
`consistency` is:

| order_id | customer_id | order_date |
|---|---|---|
| 4012 | 1 | 1 |

Every cell `1` means "all of order 4012's lines share one customer and one date."
`(consistency <= 1)` turns that grid into True/False, and `.all().all()` boils it down
to a single yes/no ("is every cell ≤ 1?"). If some order had two different dates across
its lines, that cell would be `2`, the assert would be `False`, and the script **stops**
with your message — because "which date is *the* date?" then has no honest answer.
Better to halt loudly here than silently pick one and ship a wrong number to Tableau.

**Move 2 — collapse with `groupby().agg()`.** Same bucketing; now each bucket becomes
one row. Read `customer_id=("customer_id", "first")` as "the output `customer_id` = the
*first* customer_id seen in each bucket." `"first"` is safe *only because move 1 proved
every value in the bucket is identical* — first, last, min would all give the same
answer. `as_index=False` keeps `order_id` as a normal column (not the row label) so
`to_sql` writes it out later. Then two constant columns get stamped on: `source` (which
pipeline made the row) and `status` (the export is all completed sales).

Before → after for move 2:

BEFORE (`clean`, line grain — 3 rows for order 4012):

| order_id | customer_id | order_date | product_id | … |
|---|---|---|---|---|
| 4012 | 7 | 2024-03-15 | 3 | … |
| 4012 | 7 | 2024-03-15 | 5 | … |
| 4012 | 7 | 2024-03-15 | 8 | … |

AFTER (`orders`, order grain — 1 row, date/customer stored once):

| order_id | customer_id | order_date | source | status |
|---|---|---|---|---|
| 4012 | 7 | 2024-03-15 | accounting_import | fulfilled |

**Move 3 — select the detail.** No grouping — `clean[[...]]` just picks the per-line
columns and keeps *every* row, plus `order_id` as the **pointer back** to the header:

`order_lines` (still 3 rows — per-line facts + pointer):

| order_id | product_id | qty_cartons | unit_price | unit_cost |
|---|---|---|---|---|
| 4012 | 3 | 10 | 63.76 | 40 |
| 4012 | 5 | 25 | 28.50 | 18 |
| 4012 | 8 | 8 | 41.00 | 30 |

The date and customer now live once in `orders`; the lines carry only `order_id = 4012`,
meaning "for my customer and date, look at order 4012." That pointer is the foreign key
from Step 2b, applied to orders.

### Verified counts, and the 36 orders that vanish

Of the **5,000** orders in the export, **36** had *every* line quarantined in Step 2d —
with zero surviving lines there's nothing for `groupby` to collapse, so they simply
don't appear in `orders`. Result: **4,964** orders / **12,117** lines (5000 − 36 = 4964;
12,491 − 374 = 12,117). That `4,964` is the number you'll check in Step 7, and the
number `deliveries` must equal in Step 5. The `assert` isn't ceremony — it catches a
Phase 2 generator bug (say, a date that parsed differently on two lines of the same
order) here, with a clear message, instead of as a baffling wrong total in Tableau three
phases later.

**The reusable recipe** (works for any flat → header/detail split): decide which columns
are header-level (repeat within a group) vs line-level (vary row to row) → assert the
header columns are constant per group → `groupby().agg("first")` for the header → select
`[group_key, *line_cols]` for the detail. The moves are identical every time; only
*which columns go where* changes. If you can answer "what does one row represent, before
and after?", the code writes itself.

---

## Step 4 — The surrogate-key boundary (the real lesson of this phase)

### The danger, first — the whole step exists to prevent one specific break

Coming out of Step 3 you hold two frames in memory. `orders` carries the export's
`order_id` (4012, 4013, …). `order_lines`' `order_id` column is the **only** thing tying
each line to its order — a line "knows" it belongs to 4012 because it literally says
4012.

Meanwhile the database's `orders.order_id` is declared `GENERATED ALWAYS AS IDENTITY`,
which means **Postgres insists on choosing that number itself** and, by default, refuses
a value you supply. Insert an order normally and you leave `order_id` blank; Postgres
stamps in the next number from an internal counter — 1, 2, 3, …

So two parties both want to own `order_id`:

> Your `order_lines` already committed to 4012/4013/… — but the database wants to assign
> its own 1/2/3. If the database wins, the lines still say 4012, now pointing at an order
> the database renamed to 1. **The link snaps.**

Preventing that snap is all of Step 4. Vocabulary:

- A **surrogate key** is an ID the *database* invents to tell rows apart, with no
  business meaning — as opposed to a "natural key," a real-world identifier like an
  invoice number. `orders.order_id` is a surrogate key.
- **IDENTITY** is the Postgres feature that auto-assigns those numbers. "This column is
  IDENTITY" = "the DB fills it, not you."
- The **surrogate-key boundary** is the moment data crosses from a world where IDs mean
  one thing (the export's `order_id`, 1–5000) into a world where the same column means
  something else (the database's freshly minted IDs). Something must reconcile the two
  numbering systems at the crossing.

### Chapter 0: load the lines first → rejected

```python
order_lines.to_sql("order_lines", engine, if_exists="append", index=False)
# IntegrityError: violates foreign key "order_lines_order_id_fkey"
```

The database has a foreign-key rule: "every `order_id` in `order_lines` must already
exist in `orders`." But `orders` is still empty, so a line claiming to belong to order
4012 is rejected — there's no order 4012 yet to belong to. **Lesson: parents before
children** (same load-order law as Phase 1's dimensions-before-facts, now parent facts
before child facts). So load `orders` first.

### Chapter 1: load orders first, let the DB assign IDs → the link snaps

Load `orders` the normal way; Postgres ignores your numbers and stamps in its own 1, 2,
3 in insertion order:

BEFORE (`orders` in memory) → AFTER (what lands in the DB):

| your order_id | → | DB order_id | customer_id | order_date |
|---|---|---|---|---|
| 4012 | → | **1** | 7 | 2024-03-15 |
| 4013 | → | **2** | 2 | 2024-03-16 |
| 4014 | → | **3** | 7 | 2024-03-18 |

A rename is harmless *if everyone hears about it* — but nobody told `order_lines`, which
still say 4012, 4013, 4014. Load them and every row is rejected by the same foreign-key
rule: there is no order 4012 anymore; it's called 1. **That's the snap.** The source's
ID namespace and the warehouse's ID namespace are different, and something must bridge
them. Two ways out:

- **Fix A (the production way):** add a column like `source_order_ref` to carry the
  export's number, let the DB assign `order_id` freely, then read back the "4012 → 1"
  pairing and rewrite the lines' pointers before loading. Always correct — but your
  Phase 1 schema has no such column, and nothing else identifies an order uniquely
  (`(customer_id, order_date)` repeats: one customer can order twice a day).
- **Fix B (the shortcut, correct *here*):** forbid the rename — force the DB to keep
  4012/4013/4014. Then the lines never need rewriting, because 4012 still means 4012.

### The decision: keep the export's `order_id` (why Fix B is safe this once)

Three conditions all hold, and together they make Fix B safe:

1. **One-time load into an empty table** — no existing rows for your IDs to collide with.
2. **The export's IDs are a dense, clean run 1…5000, all distinct** (verified) — they
   make perfectly good surrogate keys as-is.
3. So reusing them can't collide with anything → no reason to let the DB renumber.

In an *incremental* pipeline (new orders nightly, the source controlling its own IDs)
Fix B breaks — tonight's export could reuse an ID you already stored. There you'd need
Fix A. Knowing which situation you're in is the judgment being tested.

> **Be honest about the trade-off in the interview.** "I kept the source's order number
> as the primary key because it was a dense, trustworthy 1…N sequence loaded once into an
> empty table. In an *incremental* pipeline — source-controlled IDs, possible collisions,
> nightly batches — I would not reuse them as my surrogate key; I'd store the source's
> reference in its own column and map it to a generated key." That shows you know why the
> shortcut is safe *and* exactly where it stops being safe.

### Mechanics: staging table + `OVERRIDING SYSTEM VALUE`

Two obstacles to supplying your own IDs:

1. Postgres only accepts a value for a `GENERATED ALWAYS` column if the insert carries
   the clause **`OVERRIDING SYSTEM VALUE`** (read literally: "I'm overriding the system's
   value — stand down, DB, I'm supplying these IDs this time"). A plain insert with your
   own ID is rejected: `ERROR: cannot insert a non-DEFAULT value into column "order_id"
   / HINT: Use OVERRIDING SYSTEM VALUE to override.`
2. `df.to_sql` writes plain inserts — it can't emit that clause.

So do it in two hops: dump into a throwaway **staging table** with pandas (an ordinary
table, no IDENTITY rule — pandas writes it happily), then copy staging → real table with
one hand-written SQL statement that carries the override.

```python
from sqlalchemy import text
# engine = get_engine()  already at the top of the file

# hop 1: stage the headers (an ordinary table — pandas writes it happily)
orders.to_sql("stg_orders", engine, if_exists="replace", index=False)

# hop 2: copy staging -> real orders, forcing our order_ids through
with engine.begin() as conn:
    conn.execute(text("""
        INSERT INTO orders (order_id, customer_id, order_date, source, status)
        OVERRIDING SYSTEM VALUE
        SELECT order_id, customer_id, order_date, source, status
        FROM stg_orders
        ORDER BY order_id;
    """))
    conn.execute(text("DROP TABLE stg_orders;"))
```

(`engine.begin()` opens a transaction — a unit of work that either fully succeeds or
fully rolls back. `text("...")` wraps a raw SQL string so SQLAlchemy runs it as-is.)

AFTER hop 2 the real `orders` table keeps your numbers — no rename — so
`order_lines.order_id` still points correctly:

| order_id | customer_id | order_date |
|---|---|---|
| 4012 | 7 | 2024-03-15 |
| 4013 | 2 | 2024-03-16 |
| 4014 | 7 | 2024-03-18 |

### The gotcha: resync the counter, or a future order collides

Behind the IDENTITY column sits a **sequence** — a counter that remembers "the next
number I'll hand out." It started at 1. `OVERRIDING SYSTEM VALUE` wrote your chosen IDs
into the *rows* but never *asked* the counter for a number, so **the counter is still at
1.** The next order inserted *normally* (a future Phase 6 `whatsapp` order, say) would
get `order_id = 1`, and march upward until it hits an ID you already used → a
`duplicate key` error. Fast-forward the counter past your highest ID right after the
override insert:

```python
with engine.begin() as conn:
    conn.execute(text("""
        SELECT setval(
            pg_get_serial_sequence('orders', 'order_id'),
            (SELECT MAX(order_id) FROM orders)
        );
    """))
```

Reading inside-out: `SELECT MAX(order_id)` = 5000; `pg_get_serial_sequence('orders',
'order_id')` looks up the counter's internal name (so you never hardcode it); `setval`
sets it to 5000. Counter before → after: **1 → 5000**, so the next auto-generated order
is `5001`, safely past everything you used.

(Your highest ID is 5000 even though only 4,964 rows exist — the 36 gaps are fine.
Surrogate keys only need to be *unique*, never *gap-free*.)

### Finally, the lines — the easy way

```python
order_lines.to_sql("order_lines", engine, if_exists="append", index=False)
```

This is the *same* line that crashed in Chapter 0, but now it just works: the parent
orders exist, and because you preserved the export IDs, `order_lines.order_id` already
points at the right rows — no rewriting. You omit the lines' own surrogate key
(`order_line_id`), so the DB auto-generates that the normal way. You only needed the
override dance for `orders`, whose IDs had to match numbers already committed elsewhere.

**The whole step in one breath:** lines already point at the export's IDs → load parents
before children → letting the DB renumber snaps the link → so (one-shot, clean IDs)
preserve the export IDs via staging + `OVERRIDING SYSTEM VALUE`, resync the counter, then
append the lines against the preserved keys. The reusable intuition: *whenever a child
table already references a parent's IDs, decide who owns those IDs at the boundary —
preserve them (override + resync) or let the DB reassign and remap the children. Pick
preserve only for a one-time load of clean, collision-free IDs.*

---

## Step 5 — Generate the logistics facts against the REAL IDs

`deliveries` and `storage_costs` don't come from the export — you deferred them in
Phase 2 because they need real `order_id`s and `product_id`s to point at. Those exist
now, so this is straight synthetic *generation* (like Phase 2's dimensions), not
cleaning. Put it in `src/generate_logistics.py`.

### Wait — why *generate* deliveries instead of collecting them?

A fair question to sit with. In a real cold-chain company you would **not** invent
`deliveries` — it would fill from the physical world: a dispatcher logs when the truck
left, a routing/GPS system logs the planned ETA, telematics logs the actual arrival, and
a temperature probe in the reefer flags any cold-chain failure. You'd *collect* that.
Here there is no truck and no GPS — this is a synthetic project — so you manufacture a
plausible stand-in.

Notice the asymmetry with Steps 1–4: orders were *cleaned* from a real-ish export;
deliveries are *generated* from nothing. That mirrors a real small importer, which runs
an accounting system (so an export exists to clean) but tracks deliveries on paper or in
a WhatsApp group (so nothing digital exists to hand you).

**Isn't this the "fabrication" Step 2d forbade?** No — and the distinction is
interview-worthy. Step 2d forbade inventing a value and passing it off *as a real
recorded transaction* (that fabricates revenue). Here nothing is passed off as real: the
whole table is openly synthetic scaffolding for a demo. The rule is "never let an
invented value masquerade as a real observation," not "never generate data." A labelled
sandbox is honest.

**Why is the math elaborate, then?** Because *random ≠ realistic*. Pure uniform noise
would make the Phase 5 dashboard show nothing. The calculations bake real-world
structure into the fake data so believable patterns *emerge* when you chart it: route
depends on region (you don't send a Penang truck to Johor), transit time grows with
distance, delays follow a right-skewed curve (mostly small, rare big ones), and
temperature breaches are likelier on the long southern routes. You're imitating the
statistical *shape* of reality closely enough that downstream analysis is meaningful.

Two principles carry over from Phase 2:
- **Deterministic randomness** — seed the random generator with a fixed number, so
  "random" produces the same data on every run and your dashboards don't shift
  underfoot.
- **Referential integrity by construction** — build every row from IDs read back out of
  the database, so a foreign key can never point at something missing.

```python
# src/generate_logistics.py
import numpy as np
import pandas as pd
from db import get_engine

engine = get_engine()
SEED = 42
rng = np.random.default_rng(SEED)
```

### 5a — `deliveries` (one shipment per surviving order)

Read the real orders back out, joined to `customers` for region — a sensible route
depends on where the customer is. *Reading back rather than reusing the in-memory frame
is the honest pattern:* every `order_id` is guaranteed to exist because you got it from
the DB, and it mirrors production, where the database owns the keys.

```python
orders = pd.read_sql("""
    SELECT o.order_id, o.order_date, c.region
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.source = 'accounting_import'
""", engine)

ROUTES = {
    "North":   ["Penang Island Loop", "Butterworth–Sungai Petani", "BM–Alor Setar"],
    "Central": ["KL Klang Valley Run", "Shah Alam–Petaling", "Seremban Express"],
    "South":   ["Johor Bahru Line", "Melaka–Muar", "Batu Pahat Run"],
}
TRANSIT_H = {"North": (4, 8), "Central": (10, 16), "South": (14, 22)}  # transit hours grow with distance

n = len(orders)
regions = orders["region"].to_numpy()

dispatched = (pd.to_datetime(orders["order_date"])
              + pd.Timedelta(hours=5, minutes=30)
              + pd.to_timedelta(rng.integers(0, 120, n), unit="m"))      # ~05:30 + up to 2h jitter
transit_h  = np.array([rng.uniform(*TRANSIT_H[r]) for r in regions])
planned    = dispatched + pd.to_timedelta(transit_h, unit="h")
delay_h    = rng.gamma(shape=1.4, scale=1.3, size=n)                     # most on time, a few badly late
delivered  = planned + pd.to_timedelta(delay_h, unit="h")
breach_p   = np.where(regions == "South", 0.12,
             np.where(regions == "Central", 0.07, 0.04))                # breach likelier on longer runs
temp_excursion = rng.random(n) < breach_p
route      = np.array([ROUTES[r][rng.integers(len(ROUTES[r]))] for r in regions])

deliveries = pd.DataFrame({
    "order_id": orders["order_id"].to_numpy(), "route": route,
    "dispatched_at": dispatched.to_numpy(), "planned_eta": planned.to_numpy(),
    "delivered_at": delivered.to_numpy(), "temp_excursion": temp_excursion,
})
deliveries.to_sql("deliveries", engine, if_exists="append", index=False)
print(f"deliveries: {len(deliveries)}  breaches: {int(temp_excursion.sum())}")
```

**How each column is built** — worked on one order (customer 7, Central region, ordered
2024-03-15):

| variable | how it's computed | example value |
|---|---|---|
| `dispatched` | order_date + ~05:30 + up to 2h random jitter | 2024-03-15 06:10 |
| `transit_h` | random hours in the region's range (Central 10–16) | 12.5 h |
| `planned` (the ETA) | dispatched + transit_h | 2024-03-15 18:40 |
| `delay_h` | gamma draw — mostly small, rare large | 0.8 h |
| `delivered` | planned + delay_h | 2024-03-15 19:28 |
| `temp_excursion` | random 0–1 < the region's breach chance (Central 0.07) | False |
| `route` | random pick from the region's route list | KL Klang Valley Run |

**What `temp_excursion` (a "breach") actually is — and it is NOT lateness.** Your
business is a *cold chain*: fresh fruit must stay within a safe temperature range (say
0–4°C) the whole way, in a refrigerated ("reefer") truck. A **temperature excursion /
breach** is the moment the cargo leaves that range (compressor fails, door left open at
a stop) — the fruit's shelf life drops or it spoils, a food-safety and money problem.
It's *independent* of arriving on time: a delivery can be on-time-but-breached or
late-but-cold. So this step produces **two separate KPIs** for Phase 5:

| KPI | built from | company goal |
|---|---|---|
| delivery delay | `delivered_at − planned_eta` | minimize lateness |
| temperature breach rate | `% of deliveries where temp_excursion = True` | minimize spoilage |

Breach probability is set higher on the longer southern routes than the short northern
ones, so the Phase 5 dashboard has a believable, *actionable* pattern to surface ("Johor
runs breach ~3× more than Penang → invest in better reefers there"). `rng.gamma(...)`
draws delay hours from a right-skewed *gamma distribution* — bunched near "on time" with
a long thin tail of rare very-late deliveries, which is how real delays behave.

Because you read the orders back from the database, `len(deliveries)` equals **4,964**
(the surviving orders) automatically — "deliveries == orders" in Step 7 holds by
construction, not by luck.

> **Practical study note.** The elaborate distribution math is the *least* transferable
> part of this phase — in a real job this table is fed by a GPS/telematics feed, not by
> `rng.gamma`. You can copy this section rather than hand-derive it. What you *must* own:
> the cold-chain domain (what a breach is, the two KPIs — an interviewer will probe this)
> and the transferable structure (seed for reproducibility, read IDs back for referential
> integrity, one delivery row per order). You don't need to memorize the gamma parameters.

### 5b — `storage_costs` (one row per product per day in storage)

**Is this collected in real life?** Partly — and the split is cleaner than for
deliveries. Storage cost is *two ingredients multiplied*: (1) **how many pallets** of
each product sat in the chiller each day — genuine inventory data from a warehouse
system (WMS); and (2) **the rate** per pallet per day — a *contracted* number from a rate
card, not "collected" from anything. So the `RATE` dict below is actually *realistic* (a
real system really does hold a small rate lookup by category); only the daily pallet
counts are synthesized. In production those counts come from WMS inventory snapshots —
the cost formula and the schema stay identical.

`cost_date` has a foreign key to `dates(date)`, so build the grid straight from the
seeded `dates` and `products` tables — every key is guaranteed to exist.

```python
dates    = pd.read_sql("SELECT date FROM dates ORDER BY date", engine)["date"]
products = pd.read_sql("SELECT product_id, category FROM products", engine)

# storage rate in RM per pallet per day — chilled categories cost more than ambient.
RATE = {"Citrus": 3.20, "Berries": 5.80, "Pome": 3.00, "Tropical": 3.60}
DEFAULT_RATE = 3.50   # fallback in case a new category is added later

rows = []
for _, p in products.iterrows():
    rate = RATE.get(p["category"], DEFAULT_RATE)
    held = rng.random(len(dates)) < 0.70           # roughly 70% of days have stock on hand
    pallets = rng.integers(2, 40, len(dates))
    for d, h, pal in zip(dates, held, pallets):
        if h:
            rows.append((d, int(p["product_id"]), int(pal), rate))

storage = pd.DataFrame(rows, columns=["cost_date", "product_id",
                                      "pallets_stored", "cost_per_pallet_day"])
storage.to_sql("storage_costs", engine, if_exists="append", index=False)
print(f"storage_costs: {len(storage)} rows")
```

**Grain:** one row = one product on one day it was in storage. The loop-inside-a-loop
visits every (product, day) cell — the outer loop walks products, the inner walks days —
and each cell becomes a row *only if there was stock that day*.

Worked on product 3 (Berries, rate 5.80) over a toy 4 days:

| day | `held` (random < 0.70) | `pallets` | row produced? |
|---|---|---|---|
| 01-01 | True | 12 | `(01-01, 3, 12, 5.80)` |
| 01-02 | False | 30 | skipped — no stock |
| 01-03 | True | 7 | `(01-03, 3, 7, 5.80)` |
| 01-04 | True | 25 | `(01-04, 3, 25, 5.80)` |

So product 3 yields **3 rows, not 4** — the `if h` gate drops the no-stock day. That
gate is what "realistic gaps instead of a perfect grid" means: you don't hold every
product every single day, so a perfectly full 8×731 grid would look fake.

Two idioms worth keeping (both transferable): `rng.random(n) < p` = "n weighted
coin-flips, probability `p` of True"; `rng.integers(lo, hi, n)` = "n random whole numbers
in `[lo, hi)`."

The table stores the *ingredients* (`pallets_stored`, `cost_per_pallet_day`), not the
finished cost — the dashboard multiplies them (e.g. 12 × 5.80 = RM 69.60 for that row)
so it can re-slice by product, category, or month later.

8 products × 731 days × ~70% of days ≈ **~4,100 rows** — plenty for the Phase 4 storage
view.

---

## Step 6 — Make it idempotent and unattended

Right now both scripts *append*. Run them twice and you double the data.

### Reset-then-load

```sql
TRUNCATE order_lines, orders, deliveries, storage_costs RESTART IDENTITY CASCADE;
```

`CASCADE` lets the truncate follow foreign-key links and clear related rows in one
shot. `RESTART IDENTITY` resets the auto-generated counters so a fresh run starts
clean (Step 4's `setval` then puts the orders counter back to 5001 afterward).

> **The incremental alternative, so you can speak to it.** A *nightly* import of *new*
> orders wouldn't truncate — you'd **upsert** ("update if it already exists, otherwise
> insert": `INSERT … ON CONFLICT (source_order_ref) DO UPDATE`), so re-importing an
> overlapping export updates rather than duplicates. Truncate-reload is right for a
> synthetic one-shot; upsert is right for production incremental. Knowing which
> situation calls for which is the actual point.

### The Makefile

```make
# --- Phase 3 targets ---
reset-facts:
	psql "$(DATABASE_URL_PLAIN)" -c "TRUNCATE order_lines, orders, deliveries, storage_costs RESTART IDENTITY CASCADE;"

etl: reset-facts
	uv run python src/etl_orders.py
	uv run python src/generate_logistics.py
```

`make etl` is now your one command: clear the facts, clean and load the orders,
generate the logistics. It depends on `reset-facts`, so it's safe to run any number of
times. Keep this **separate** from your Phase 2 `seed` target: `seed` truncates *all*
tables (dimensions + facts) and rebuilds the dimensions; `reset-facts` truncates *only
the four facts*, so re-running the ETL never disturbs the dimensions it reads from. Run
`make seed` once (or when you want to regenerate the source), then `make etl` as often
as you like.

### Logging + the unattended story

`print` scrolls past and vanishes; a scheduled 1 AM run has nobody watching. **Logging**
writes a durable, timestamped record to a file you read the next morning — the only
witness an unattended job leaves. Configure it once at the top of each script, reading
the level from an env var so you can turn on verbosity without editing code:

```python
import logging, os
os.makedirs("logs", exist_ok=True)          # logging won't create the folder for you
logging.basicConfig(
    filename="logs/etl.log",
    level=os.environ.get("LOG_LEVEL", "INFO"),   # LOG_LEVEL=DEBUG make etl → verbose
    format="%(asctime)s %(levelname)s %(message)s",
)
```

**Use the levels that carry distinct meaning** — don't add a level just to complete the
set (that's noise, not craft):

- **INFO** — the normal story: `start`, the counts that reconcile against the source
  (`read 12491 rows`, `rows in/quarantined/clean`, `reject reasons {...}`, `loaded N`),
  and `done`. Logging the in/out/rejected counts is the "report" half of "quarantine
  *and report*" — it's what lets someone reconcile your load against accounting and spot
  an anomalous night at a glance.
- **WARNING** — didn't crash, but a human should look. Guard the "should be 0" checks so
  they stay silent on a clean run and only speak up when they don't:
  ```python
  n_unparsed = int(df["order_date"].isna().sum())
  if n_unparsed:
      logging.warning("dates unparsed: %d (will be quarantined)", n_unparsed)
  ```
- **DEBUG** — verbose detail (dtypes, breach rate) you want *only* when investigating;
  silent at the default INFO level, on with `LOG_LEVEL=DEBUG`.
- **ERROR** — if the job crashes, capture the traceback instead of losing it. Wrap the
  entry point:
  ```python
  if __name__ == "__main__":
      try:
          main()
      except Exception:
          logging.exception("etl_orders: FAILED")   # ERROR level + full traceback
          raise                                       # re-raise so make/cron sees the failure
  ```

Then the cron line that makes it *automation*. Note it lives in the OS's crontab
(`crontab -e`), **not** in your repo. Fields are `minute hour day-of-month month
day-of-week`:

```cron
# crontab -e  → run nightly at 01:00, appending stdout+stderr to a log
0 1 * * * cd /Users/you/coldchain-ops && /usr/bin/env make etl >> logs/cron.log 2>&1
```

The `cd` is essential — cron starts in your home directory, so relative paths (`src/…`,
`logs/…`, `.env`) break without it. `>> logs/cron.log 2>&1` sends both normal output
(`>>`) and errors (`2>&1`) to one file. On macOS, cron's minimal `PATH` won't find
Homebrew's `uv`/`psql` and Docker must be running — so for a portfolio you don't actually
install this; **writing** the cron line is itself the demonstration that "automation =
unattended + auditable." Add the throwaway artifacts to `.gitignore`: `logs/` and
`data/rejects_orders.csv`.

---

## Step 7 — Verify it worked

`make psql`, then run these. Each checks a specific claim.

```sql
-- 1. orders landed, all tagged with the right provenance
SELECT source, count(*) FROM orders GROUP BY source;
-- expect: accounting_import | 4964    (NOT 5000 — 36 orders lost all their lines to quarantine)

-- 2. lines landed, and none is orphaned (an order line pointing at a missing order)
SELECT count(*) AS lines,
       count(*) FILTER (WHERE o.order_id IS NULL) AS orphan_lines
FROM order_lines ol LEFT JOIN orders o USING (order_id);
-- expect: 12117 | 0

-- 3. no order is empty (every header has at least one line)
SELECT count(*) AS empty_orders
FROM orders o LEFT JOIN order_lines ol USING (order_id)
WHERE ol.order_id IS NULL;
-- expect: 0

-- 4. every order_date actually exists in the dates dimension (the FK we relied on)
SELECT count(*) AS dates_missing
FROM orders o LEFT JOIN dates d ON d.date = o.order_date
WHERE d.date IS NULL;
-- expect: 0

-- 5. the money survived cleaning — margins sane, nothing accidentally null
SELECT round(min((unit_price-unit_cost)*qty_cartons), 2) AS min_margin,
       round(avg((unit_price-unit_cost)*qty_cartons), 2) AS avg_margin,
       count(*) FILTER (WHERE unit_price IS NULL OR unit_cost IS NULL) AS null_money
FROM order_lines;
-- expect: null_money = 0, avg_margin a believable positive number

-- 6. logistics facts reference real orders/products, one delivery per order
SELECT (SELECT count(*) FROM deliveries)    AS deliveries,   -- expect 4964 (== orders)
       (SELECT count(*) FROM orders)        AS orders,       -- expect 4964
       (SELECT count(*) FROM storage_costs) AS storage_rows; -- expect ~4100
```

Then the end-to-end margin join from Phase 1, now over real volume — the query your
whole Tableau layer will sit on:

```sql
SELECT c.channel, p.category,
       sum((ol.unit_price - ol.unit_cost) * ol.qty_cartons) AS margin
FROM order_lines ol
JOIN orders    o ON o.order_id    = ol.order_id
JOIN products  p ON p.product_id  = ol.product_id
JOIN customers c ON c.customer_id = o.customer_id
GROUP BY c.channel, p.category
ORDER BY margin DESC
LIMIT 10;
```

If that returns believable margins by channel and category, Phase 3 is done: the ugly
spreadsheet is clean, queryable, internally consistent, and rebuildable with one
command.

---

## Step 8 — Commit

```fish
git add src/etl_orders.py src/generate_logistics.py Makefile .gitignore
git commit -m "feat(etl): clean accounting export into orders/order_lines, generate logistics facts"
```

(`feat(etl)` for the pipeline itself; split Makefile/`.gitignore` housekeeping into a
separate `chore:` commit if you want to keep them apart.) Tell me when you want the
PROGRESS.md box ticked, and we'll move to Phase 4.

---

### What's been verified

Verified against your real `accounting_export.xlsx`, the Postgres schema, **and a full
`make etl` run**: the naive-read shape mismatch and every Step 1 dtype; the
`format="mixed"`+`dayfirst=True` swap bug on real rows, and that the per-format fix
parses all 12,491 rows with **0 unparsed**; product canonicalization at **100% match**,
no collisions; the money cleaner's output range; the exact counts (**374** quarantined /
**12,117** clean / **4,964** orders); the Step 4 staging + `OVERRIDING SYSTEM VALUE` load
with the sequence resynced to 5001; the Step 5 generation (**4,964** deliveries ==
orders, **4,065** storage rows); and all seven Step 7 checks passing (0 orphan lines, 0
empty orders, 0 missing dates, 0 null money, min line margin **+30.10** — no below-cost
lines — and a believable margin-by-channel×category ranking). Rebuildable end to end with
one idempotent `make etl`.
