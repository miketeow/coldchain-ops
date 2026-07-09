# Phase 6 — Ask a business question in English, get it answered by SQL

**Goal:** end this phase with a script that takes a plain-English business question —
"what's our total margin?", "which region has the worst temperature breach rate?" — and
answers it by having an LLM write a SQL query against Phase 4's views, running that query
safely, and printing the result. This is the payoff the whole project has been building
toward: Phase 4 built the trustworthy semantic layer, Phase 5 proved semantic matching
works over messy text, and this phase connects an LLM to both so a person never has to
hand-write SQL for a question you didn't anticipate.

**Why `gemini-2.5-flash-lite`:** this is a narrow, low-stakes task — one-shot,
schema-constrained SQL generation — not open-ended reasoning, so it doesn't need a
frontier model. Google's Gemini API free tier gives Flash-Lite free input and output
tokens with no credit card, and the highest free-tier request throughput of any Gemini
model (the exact RPM/RPD numbers shift over time and are only authoritative live in
[AI Studio](https://aistudio.google.com)). That fits a portfolio project well: anyone
cloning this repo can get a key in a minute and actually run it, for free. Every run of
the script is a real request against a real model — there is no offline mode to maintain
and no divergence between what you develop against and what a reader would run.

## The moves
1. **Restrict at the database, not the prompt.** A system prompt telling the model "only
   query these views" is a *request*, not a *wall* — nothing stops it from writing
   `DROP TABLE orders;` if it misunderstands, and Postgres will run whatever a
   full-privilege connection sends it. The real fix is a Postgres role that is only
   ever *granted* `SELECT` on the three views — it cannot touch a raw table or write
   anything, no matter what SQL text arrives, because Postgres refuses it before your
   code even sees the result.
2. **Force a structured answer, don't parse prose.** Asking "write me some SQL" and then
   regexing a code fence out of the reply breaks the moment the model adds an
   explanation sentence or forgets the fence. Force the shape of the reply instead, the
   same structured-output mechanism (a JSON schema the model must conform to) from the
   structured-extraction discussion earlier — you get back exactly one field, `sql`,
   guaranteed.
3. **Validate before executing, as a second, cheap layer.** The database role is the real
   guarantee; a quick check that the returned text is a single `SELECT` statement is a
   fast, free sanity check that fails with a clear error before a query ever reaches
   Postgres.

---

## Step 0 — Pre-flight

```fish
cd coldchain-ops
docker compose ps          # expect coldchain-db running
make psql
```
```sql
-- confirm the three views from Phase 4 are still there and correct
SELECT table_name FROM information_schema.views WHERE table_schema='public' ORDER BY 1;
-- expect: v_delivery_performance / v_sales_margin / v_storage_cost

SELECT round(sum(margin),2) FROM v_sales_margin;             -- expect 6031937.59
SELECT round(100.0*avg(temp_excursion::int),1) AS breach_pct
FROM v_delivery_performance WHERE region='South';             -- expect 12.4
\q
```

New dependency — the Google Gen AI Python SDK:

```fish
uv add google-genai
```

Get an API key before going further — the script needs one on every run:

1. Go to [AI Studio](https://aistudio.google.com/apikey) and sign in with a Google
   account.
2. Click **Create API key**. No credit card, no billing account required for the free
   tier.
3. Copy the key into `.env`. It's a live credential, so treat it like any other secret:
   `.env` is already gitignored, and it must stay that way.

```
GEMINI_API_KEY=<paste your key here>
DATABASE_URL_LLM_PLAIN=postgres://llm_reader:llm_reader_pw@localhost:5433/coldchain?sslmode=disable
```

The second line is a **new, separate connection string** — not your existing
`DATABASE_URL_PLAIN`. It points at a new, deliberately underpowered Postgres role you'll
create in Step 2, which is the actual safety mechanism this whole phase depends on.

This phase adds:
- one goose migration (`add_llm_reader_role`) — creates the restricted role.
- `src/ask_question.py` — the script that ties everything together.

---

## Step 1 — Chapter 0: trusting the prompt, and why that's not a safety mechanism

### The naive version

```python
system_prompt = """You are a SQL analyst. Only ever query these three views:
v_sales_margin, v_delivery_performance, v_storage_cost. Never touch any other table."""

# ... call the LLM, get back some SQL, run it directly against the same
# full-privilege connection your other scripts already use (DATABASE_URL_PLAIN)
```

This looks reasonable and will work almost all the time. But "almost all the time" is
not a safety property. A system prompt is an *instruction to a language model*, not a
*permission system* — the model can misread the question, hallucinate a join against
`order_lines` directly instead of the view, or (if this were ever exposed to outside
input) be steered by an adversarial message into writing something destructive. If the
database connection it runs against has full read/write privileges — which
`DATABASE_URL_PLAIN` does, since that's the same `ops` user every other script in this
project uses — nothing stops a bad query from actually running.

### The fix: make the wrong thing structurally impossible

Instead of asking the model nicely, take away everything it could misuse. Create a
second Postgres role that has been granted `SELECT` on exactly three objects — the
views — and nothing else. Even a perfectly-formed `DELETE FROM orders;` sent through
that connection fails with a permissions error, because the role was never granted
write access to anything, and was never granted *any* access to the raw tables at all.
This is the same principle as Phase 4's grain discipline, just applied to permissions
instead of SQL correctness: don't rely on every caller behaving — make the wrong shape
structurally unreachable.

---

## Step 2 — Create the restricted role

```fish
make db-create name=add_llm_reader_role
```

```sql
-- +goose Up
CREATE ROLE llm_reader LOGIN PASSWORD 'llm_reader_pw';
GRANT USAGE ON SCHEMA public TO llm_reader;
GRANT SELECT ON v_sales_margin, v_delivery_performance, v_storage_cost TO llm_reader;

-- +goose Down
REVOKE SELECT ON v_sales_margin, v_delivery_performance, v_storage_cost FROM llm_reader;
REVOKE USAGE ON SCHEMA public FROM llm_reader;
DROP ROLE llm_reader;
```

**Why this is enough, with nothing else added.** A newly created Postgres role starts
with *zero* privileges on existing objects — it can't read `order_lines`, `customers`,
or any raw table, because nothing ever granted it that right. The two `GRANT`s above are
the *entire* allowlist: permission to reference objects in the `public` schema at all
(`USAGE`), and permission to `SELECT` from the three views specifically. There's no
`GRANT INSERT`, `GRANT UPDATE`, or `GRANT` on any table anywhere — so even if the LLM
returns SQL that tries to write or touch a raw table, Postgres itself rejects it before
your Python code ever runs. This is a stronger guarantee than anything you could write
in application code, because it doesn't depend on your validation logic being correct.

```fish
make db-migrate
make db-status
```

**Verify the restriction actually holds** (a read-only check — this connects as the new
role and *tries* a query it should never be allowed to run, to confirm it's refused):

```fish
psql "postgres://llm_reader:llm_reader_pw@localhost:5433/coldchain?sslmode=disable" \
  -c "SELECT * FROM v_sales_margin LIMIT 1;" \
  -c "SELECT * FROM order_lines LIMIT 1;"
```

Expect the first command to succeed (a row from the view) and the second to fail with
`permission denied for table order_lines` — that failure is the proof the role is doing
its job.

---

## Step 3 — Describe the views to the model

The model needs to know what it's allowed to query and what each column means — but
handing it a raw `information_schema` dump gives it column names with no business
context (it won't know `channel` only ever takes 4 values, or that `on_time` is already
a computed boolean, not something to re-derive). A short, hand-written description, the
same instinct as Phase 4's inline SQL comments, gives the model exactly the context it
needs and nothing it doesn't:

```python
VIEW_SCHEMA = """
You may only query these three views. Never reference any other table.

v_sales_margin(order_line_id, order_id, order_date, year, quarter, month, month_name,
    channel, region, city, category, product_name, brand, qty_cartons, unit_price,
    unit_cost, revenue, cost, margin)
    -- one row per order line. revenue/cost/margin are already computed money columns.
    -- channel is one of: Hypermarket, Supermarket, Wholesale, Ecommerce.

v_delivery_performance(delivery_id, order_id, order_date, region, channel, route,
    dispatched_at, planned_eta, delivered_at, delay_hours, on_time, temp_excursion)
    -- one row per delivery. on_time and temp_excursion are already-computed booleans.
    -- region is one of: North, Central, South.

v_storage_cost(storage_cost_id, cost_date, year, month, month_name, product_id,
    product_name, category, pallets_stored, cost_per_pallet_day, daily_cost)
    -- one row per product per day held in storage. daily_cost is already computed.
"""
```

---

## Step 4 — Chapter 0: asking for SQL in prose, and why that breaks too

### The naive version

```python
response = client.models.generate_content(
    model="gemini-2.5-flash-lite",
    contents=f"{VIEW_SCHEMA}\n\nWrite SQL for: {question}",
)
# response.text might come back as:
#   "Sure! Here's a query that should work:\n\n```sql\nSELECT ...\n```\nLet me know if..."
```

Now your code has to reliably strip a markdown fence and an explanation sentence out of
free text, every time, hoping the model's formatting habits don't shift. This is exactly
the "regex-scraping a reply" trap — brittle for the same reason parsing a WhatsApp
message with plain string matching would be.

### The fix: force the shape of the answer

```python
from google.genai import types
from pydantic import BaseModel

class SQLAnswer(BaseModel):
    sql: str

response = client.models.generate_content(
    model="gemini-2.5-flash-lite",
    contents=question,
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=SQLAnswer,
    ),
)
response.parsed.sql   # a plain string, guaranteed to be exactly that field
```

`response_schema` is the same structured-output mechanism from the earlier discussion —
here it's a single-field shape instead of the multi-field extraction we discussed for
order parsing, but the guarantee is identical: no prose, no fence, no `json.loads()`
failure handling. Passing the Pydantic class directly (not `.model_json_schema()`) is
what makes `response.parsed` come back as an already-validated `SQLAnswer` instance
rather than a raw dict you'd have to validate yourself — you get the SQL string, or the
request fails validation up front.

---

## Step 5 — The script: `src/ask_question.py`

```python
import os
import re
import sys

import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()

MODEL_NAME = "gemini-2.5-flash-lite"

VIEW_SCHEMA = """
You may only query these three views. Never reference any other table.

v_sales_margin(order_line_id, order_id, order_date, year, quarter, month, month_name,
    channel, region, city, category, product_name, brand, qty_cartons, unit_price,
    unit_cost, revenue, cost, margin)
    -- one row per order line. revenue/cost/margin are already computed money columns.
    -- channel is one of: Hypermarket, Supermarket, Wholesale, Ecommerce.

v_delivery_performance(delivery_id, order_id, order_date, region, channel, route,
    dispatched_at, planned_eta, delivered_at, delay_hours, on_time, temp_excursion)
    -- one row per delivery. on_time and temp_excursion are already-computed booleans.
    -- region is one of: North, Central, South.

v_storage_cost(storage_cost_id, cost_date, year, month, month_name, product_id,
    product_name, category, pallets_stored, cost_per_pallet_day, daily_cost)
    -- one row per product per day held in storage. daily_cost is already computed.
"""

SYSTEM_PROMPT = f"""You are a SQL analyst for a cold-chain fruit distributor.
Given a business question, write ONE read-only PostgreSQL SELECT query that answers
it, using only the views below. Never write more than one statement.

{VIEW_SCHEMA}
"""


class SQLAnswer(BaseModel):
    sql: str


def get_sql(question: str) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=question,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=SQLAnswer,
        ),
    )
    parsed = response.parsed
    if not isinstance(parsed, SQLAnswer):
        raise RuntimeError(f"Gemini did not return the expected schema: {parsed!r}")
    return parsed.sql


FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|grant|truncate)\b", re.I)


def validate_sql(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError("only a single statement is allowed")
    if not stripped.lower().startswith("select"):
        raise ValueError("only SELECT statements are allowed")
    if FORBIDDEN.search(sql):
        raise ValueError(f"query contains a disallowed keyword: {sql}")


def main():
    question = sys.argv[1]
    sql = get_sql(question)
    validate_sql(sql)

    conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"])
    with conn, conn.cursor() as cur:
        cur.execute(sql)
        colnames = [desc.name for desc in cur.description]
        rows = cur.fetchall()

    print(f"\nSQL: {sql}\n")
    print(colnames)
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
```

Worth reading closely:

- **Reading `GEMINI_API_KEY` straight from `os.environ[...]` (not `.get`) is
  deliberate.** A missing key fails immediately and loudly with a `KeyError` naming the
  variable, at the top of the call, rather than surfacing later as a confusing
  authentication error from deep inside the SDK.
- **`response.parsed` is typed as `BaseModel | dict | Enum | None`,** since
  `response_schema` accepts several shapes — so the `isinstance` check both narrows the
  type for the checker and catches the real case where the model returns something that
  doesn't fit `SQLAnswer`, with a message that shows you what came back instead.
- **`validate_sql` is the second, cheaper safety layer** — it catches an obviously wrong
  answer (multiple statements, a write keyword) with a clear Python error message before
  a query ever reaches Postgres. It is *not* the reason this is safe; Step 2's database
  role is. Even if this function had a bug and let something bad through, the
  `llm_reader` connection still can't do anything with it beyond `SELECT` on 3 views.
- **The connection here is `DATABASE_URL_LLM_PLAIN`, not `DATABASE_URL_PLAIN`.** This
  script only ever talks to Postgres as the restricted role from Step 2 — never as the
  full-privilege `ops` user your other scripts use.

---

## Step 6 — Sanity-check the API call before wiring in the database

Before running the whole pipeline end to end, isolate the one new piece of risk — does
the Gemini call itself work with your key, and does `response_schema` actually force the
shape you expect — separately from the database side. A quick one-off in a Python REPL
(`uv run python`) is enough:

```python
import os
from google import genai
from google.genai import types
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

class SQLAnswer(BaseModel):
    sql: str

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
response = client.models.generate_content(
    model="gemini-2.5-flash-lite",
    contents="what's our total margin?",
    config=types.GenerateContentConfig(
        system_instruction="You are a SQL analyst. Reply with SELECT sum(margin) FROM v_sales_margin;",
        response_mime_type="application/json",
        response_schema=SQLAnswer,
    ),
)
print(response.parsed)
```

Expect a `SQLAnswer(sql='SELECT ...')` back, not an error. If this fails, the problem is
narrowed to the key or the API itself, before the database and `validate_sql` are even
in the picture — a much smaller surface to debug than running `ask_question.py` cold and
guessing which layer broke. A `401`/`PERMISSION_DENIED` means the key is wrong or not
loaded from `.env`; a `429` means you've hit the free-tier rate limit and should wait a
minute rather than change any code.

---

## Step 7 — Verify it worked

```fish
uv run python src/ask_question.py "what's our total margin?"
```

Expect the printed SQL to run cleanly and return a number matching Phase 4's already-
verified total: **6,031,937.59**. Similarly:

```fish
uv run python src/ask_question.py "which region has the worst temperature breach rate?"
# expect South at the top, 12.4%
```

If a question returns the wrong number, the bug is almost always one of: Gemini
paraphrased the question into a query that's subtly wrong (worth reading the printed SQL
by eye — this is exactly why the script prints it before the result, not just the
numbers), or `VIEW_SCHEMA`'s column comments in Step 3 weren't specific enough about what
a column means, so the model computed something plausible-looking but incorrect.

---

## Step 8 — Commit

```fish
git add migrations/*_add_llm_reader_role.sql src/ask_question.py pyproject.toml uv.lock
git commit -m "feat: answer natural-language questions via LLM-generated SQL over the semantic layer"
```

Tell me when you want the PROGRESS.md box ticked.

---

### What I verified while writing this (read-only, nothing in your DB changed)

- Confirmed (from earlier phases, unchanged since) that all three views exist and their
  previously-verified totals still hold: `v_sales_margin` margin **6,031,937.59**,
  `v_delivery_performance` South breach rate **12.4%**.
- Confirmed against Google's current docs that `gemini-2.5-flash-lite` is on the free
  tier, that `google-genai` is the current SDK, and that passing a Pydantic class as
  `response_schema` yields a validated instance on `response.parsed`.
- I have not created the `llm_reader` role, added the `.env` lines, requested an API key,
  or written `src/ask_question.py` — every one of those is a write, a credential, or an
  external API interaction, and per this project's rule, those are yours to do.

**On you to run:** getting an API key from AI Studio, the migration, Step 6's
sanity-check, and Step 7's verification. If the printed SQL looks wrong before you even
look at the result (e.g. it references a raw table instead of a view), that's worth
catching by eye — a good sign the schema description in Step 3 needs a clearer comment,
not that something in the Python is broken.
