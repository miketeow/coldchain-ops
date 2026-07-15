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

Commit here, with the pipeline working end to end, before improving anything. Step 9
changes both the prompt and the output shape, and you want a known-good revision to diff
against when the answers change.

---

## Step 9 — Making the answer read like an answer

Run what you just built and look hard at the output:

```
SQL: SELECT SUM(margin) FROM v_sales_margin;

['sum']
(Decimal('6031937.59'),)
```

```
SQL: SELECT region, AVG(CASE WHEN temp_excursion THEN 1.0 ELSE 0.0 END) AS
     temperature_breach_rate FROM v_delivery_performance
     GROUP BY region ORDER BY temperature_breach_rate DESC LIMIT 1;

['region', 'temperature_breach_rate']
('South', Decimal('0.12396694214876033058'))
```

Both are *correct*. Neither is an *answer*. A column called `sum`, a twenty-digit
`Decimal`, a bare tuple — no company would put this in front of a person. The instinct is
to reach for prose: pipe the rows to an LLM, ask it to write a sentence. Hold that
thought, because most of what's wrong here isn't fixable with better words.

### Chapter 0: the format string

The obvious first move:

```python
print(f"Our total margin is ${rows[0][0]:,.2f}.")
```

This works for exactly one question. The breach-rate question returns a different number
of columns, a different number of rows, and wants a *comparison* sentence rather than a
statement. You'd end up writing one template per question — which means hand-anticipating
every question a user might ask, the precise thing this whole phase exists to avoid. The
template approach doesn't scale down from "any question" to "the questions I wrote code
for."

### Chapter 1: let the model write the template

A cleverer version, worth pausing on because it's genuinely appealing. Have the *first*
call return two fields instead of one:

```python
class SQLAnswer(BaseModel):
    sql: str
    answer_template: str   # e.g. "Our total margin is {total_margin_myr}."
```

Then Python fills the placeholders from the result row. The model **never sees a number**,
so it structurally *cannot* state a wrong one — the same "remove the opportunity to
misbehave" instinct as the `llm_reader` role in Step 2.

It breaks on two things. Templates can't loop, so any multi-row result has nowhere to go.
And a model writing the template *blind* can't say "notably higher than the others,"
because at template-writing time it doesn't know whether it is. The rigidity that makes
this safe is exactly what keeps it cold.

### The actual diagnosis: it's missing context, not missing words

Look again at the breach-rate query. `LIMIT 1`. It returned South and threw the rest away.
Here is what the database will happily tell you:

| region  | breach_pct |
|---------|-----------:|
| South   |       12.4 |
| Central |        8.2 |
| North   |        4.0 |

South isn't just "the worst." South is **three times North**. That is the finding — and no
amount of prose polish on `('South', Decimal('0.1239...'))` can recover it, because the
information is no longer in the tuple. **What makes an answer feel natural is mostly
context, not phrasing.** Prettier words around one number are still one number.

So before adding any narration, fix what the SQL *retrieves* and what it *returns*.

### Fix 1 — make the SQL produce presentation-ready values

Three sentences appended to `SYSTEM_PROMPT`, no new code:

```python
SYSTEM_PROMPT = f"""You are a SQL analyst for a cold-chain fruit distributor.
Given a business question, write ONE read-only PostgreSQL SELECT query that answers
it, using only the views below. Never write more than one statement.

Alias every output column with a descriptive snake_case name; never emit a bare
sum/avg/count as a column name. Round money to 2 decimal places and percentages to 1,
using round(), so the result is directly readable. When a question asks for a
superlative ("worst", "best", "top"), return the full ranked set rather than only the
top row — the comparison is the answer.

{VIEW_SCHEMA}
"""
```

And give the model the units it is currently guessing at, in `VIEW_SCHEMA`:

```python
    -- one row per order line. revenue/cost/margin are already computed money columns, in MYR.
```

That alone turns `['sum'] / (Decimal('6031937.59'),)` into `['total_margin_myr']`, and
turns the breach question into the full three-row ranking. The output is dramatically
more informative and you have not added a single line of Python.

It also does something subtler that the next fix depends on. Rounding now happens **in
Postgres**. When a narrator model later needs to say "12.4%", that string already exists
in the result — it never has to divide `0.12396694214876033058` by anything. Arithmetic
stays in the deterministic layer; the model's only job is words.

### Fix 2 — a narrator call that can only quote

Now, and only now, add the second `generate_content`. Same structured-output mechanism as
Step 4:

```python
class Narration(BaseModel):
    answer: str


NARRATOR_PROMPT = """You explain database query results to a business user at a
cold-chain fruit distributor. You are given a question and the exact rows the database
returned.

Rules:
- Every figure you state must appear verbatim in the rows. Never compute, derive,
  estimate, or round a number that is not already there.
- Never mention trends, comparisons to other time periods, or causes — you can only
  see these rows, nothing else.
- If the rows do not answer the question, say so plainly.
- Two sentences at most. Plain language, no markdown.
"""


def narrate(question: str, colnames: list[str], rows: list[tuple]) -> str:
    table = " | ".join(colnames) + "\n"
    table += "\n".join(" | ".join(str(v) for v in row) for row in rows)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=f"Question: {question}\n\nQuery result:\n{table}",
        config=types.GenerateContentConfig(
            system_instruction=NARRATOR_PROMPT,
            response_mime_type="application/json",
            response_schema=Narration,
        ),
    )
    parsed = response.parsed
    if not isinstance(parsed, Narration):
        raise RuntimeError(f"Gemini did not return the expected schema: {parsed!r}")
    return parsed.answer
```

Note `str(v)` when building the table — not `repr()`, and not the raw tuple. You want the
model to see `12.4`, not `Decimal('12.4')`; feeding it Python's repr invites it to echo
Python's repr back at you.

Wire it into `main()`, and **keep printing the rows**:

```python
    print(f"\nSQL: {sql}\n")
    print(narrate(question, colnames, rows))
    print()
    print(colnames)
    for row in rows:
        print(row)
```

With the ranked data from Fix 1, that yields something like: *"South has the worst
temperature breach rate at 12.4%, well above Central at 8.2% and North at 4.0%."* Every
number in that sentence came out of Postgres.

### Why the raw rows stay on screen

The prose is a *rendering*; the table is the *truth*. Every serious BI tool shows you the
chart next to the data it was drawn from, and for the same reason: a rendering you cannot
audit is a rendering you cannot trust. If the narrator ever drifts, the evidence is
sitting directly beneath it.

### Be honest about what just changed, safety-wise

This is the part worth internalising, because it's easy to assume the whole pipeline is
uniformly hardened once one part of it is.

Step 2's `llm_reader` role is a **structural** guarantee: Postgres refuses bad SQL no
matter what the model intended. `NARRATOR_PROMPT` has no equivalent wall. "Quote
verbatim, never compute" is a *request* — the exact category of thing that Step 1 spent a
page explaining is not a safety mechanism. You have moved back down to prompt-level
assurance, deliberately.

What makes that acceptable *here*, and not in Step 1, is blast radius. The narrator has
no capabilities: it cannot reach the database, and its output is never executed. The
first call's output **is** executed — that's why it needed a wall. The worst a bad
narration can do is put a misleading sentence above a correct table that the reader can
see. Different exposure, different level of defense. The lesson is not "prompts are fine
now"; it's that the defense you build should match what the output is allowed to *do*.

If you want a cheap partial wall anyway — and this is a good thing to reach for, not
just to describe — pull the figures out of `answer` with a regex and assert each one
appears among the row values:

```python
def check_grounded(answer: str, rows: list[tuple]) -> None:
    values = {str(v) for row in rows for v in row}
    for figure in re.findall(r"\d[\d,]*\.?\d*", answer):
        if figure.replace(",", "") not in values:
            raise ValueError(f"narration cites {figure!r}, which is not in the result")
```

It is imperfect — it will trip on a legitimately reformatted `6,031,937.59` — which is
itself the lesson: this check is only *possible* because Fix 1 made every displayed number
a rounded literal that Postgres produced. Design decisions upstream are what make
verification downstream cheap. That's the same story as Phase 4's views making this entire
phase's prompt short enough to fit in a paragraph.

---

## Step 10 — Commit the improvement

Run both demo questions again and read the sentences before you commit — you are checking
that the narration says what the table says, which is a judgement no test can make for
you:

```fish
uv run python src/ask_question.py "what's our total margin?"
uv run python src/ask_question.py "which region has the worst temperature breach rate?"
```

```fish
git add src/ask_question.py
git commit -m "feat: narrate query results in natural language, grounded in the returned rows"
```

---

## Step 11 — Surviving a flaky upstream

Run the script enough times and it dies:

```
google.genai.errors.ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message':
'This model is currently experiencing high demand. Spikes in demand are usually
temporary. Please try again later.', 'status': 'UNAVAILABLE'}}
```

`gemini-2.5-flash-lite` on the free tier is best-effort capacity. A `503` is Google
shedding load — nothing about your request is wrong, and the same call a second later
usually succeeds. Two API calls per invocation (one for SQL, one to narrate) means two
independent chances to hit it, so a ~20% per-call rate surfaces as roughly a 40%
chance that *something* in the run fails.

### Chapter 0: assume the SDK handles it

Read the traceback closely and it looks like retries already happened:

```
File ".../google/genai/_api_client.py", line 1411, in _request
    return self._retry(self._request_once, http_request, stream)
File ".../tenacity/__init__.py", line 470, in __call__
    do = self.iter(retry_state=retry_state)
```

`tenacity` is *right there* in the stack — the industry-standard Python retry library,
inside the vendor's client, wrapping the exact call that failed. The natural conclusion
is that the SDK retried and gave up, and that if you want more you must layer your own
retry on top.

That conclusion is wrong, and the only way to find out is to open the dependency:

```fish
grep -n "def retry_args" -A 14 .venv/lib/python3.14/site-packages/google/genai/_api_client.py
```

```python
def retry_args(options: Optional[HttpRetryOptions]) -> _common.StringDict:
  """...If None, the 'never retry' stop strategy will be used."""
  if options is None:
    return {'stop': tenacity.stop_after_attempt(1), 'reraise': True}
```

**`stop_after_attempt(1)` means one attempt: the original.** When you don't pass
`retry_options`, the SDK still constructs a `tenacity.Retrying` object — which is why it
appears in your traceback — but configures it to never retry. It's a no-op wrapper. The
presence of a retry library in a stack trace tells you nothing about whether retrying
occurred.

Directly above that function sit the defaults, unused until you ask for them:

```python
_RETRY_ATTEMPTS = 5  # including the initial call.
_RETRY_INITIAL_DELAY = 1.0  # seconds
_RETRY_EXP_BASE = 2
_RETRY_JITTER = 1
_RETRY_HTTP_STATUS_CODES = (408, 429, 500, 502, 503, 504)
```

Exponential backoff with jitter, retrying on precisely the transient statuses — including
your `503`. Someone already wrote what you were about to write. It is simply opt-in.

This is worth generalising: **"there's a well-known library in the traceback" is not
evidence that the behaviour you want is switched on.** The cost of checking was one
`grep`. The cost of not checking would have been hand-rolling a second retry loop *around*
the SDK's — 5 × 5 attempts, a backoff curve nobody can reason about, and a bug you'd only
notice as mysteriously long hangs.

### Fix 1 — turn on the retry that already exists

You currently build a `genai.Client(...)` inside both `get_sql` and `narrate`. Hoist it to
module scope so the configuration is stated once, and pass the retry options:

```python
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
)
```

Then delete the per-call `genai.Client(...)` lines and use this module-level `client` in
both functions. You don't need to enumerate status codes — `503` is already in
`_RETRY_HTTP_STATUS_CODES`, and overriding `http_status_codes` would only narrow what you
get for free.

With five attempts and 1→2→4→8-second backoff, a 20% per-call failure rate falls to
roughly 0.03%. Don't take that arithmetic on faith: run the same question ten times and
count. If failures persist at any real rate, retrying is masking a different problem and
you should stop and read the error, not add a sixth attempt.

### Fix 2 — the guard, which matters more than the retry

Retries shrink the failure probability. They cannot drive it to zero — a long enough
outage exhausts any budget. So ask the sharper question: **when `narrate` fails, what have
you actually lost?**

Trace it. By the time `narrate` runs, the SQL was generated, validated, executed against
`llm_reader`, and the rows were fetched. The correct, verified answer is sitting in memory
in `rows`. And the program threw it away, with a stack trace, because the decorative layer
had a bad minute.

That is not a missing retry. That is a program treating an optional component as
load-bearing. It's the same distinction Step 9 drew about safety, arriving from the other
direction: the narrator has no capabilities and its output is never executed. **A
component with no power to do harm should also have no power to fail the program.**

```python
from google.genai import errors


def narrate(question: str, colnames: list[str], rows: list[tuple]) -> str | None:
    table = " | ".join(colnames) + "\n"
    table += "\n".join(" | ".join(str(v) for v in row) for row in rows)

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=f"Question: {question}\n\nQuery result:\n{table}",
            config=types.GenerateContentConfig(
                system_instruction=NARRATOR_PROMPT,
                response_mime_type="application/json",
                response_schema=Narration,
            ),
        )
    except errors.APIError as e:
        print(f"[narration unavailable: {e.code} {e.status}]", file=sys.stderr)
        return None

    parsed = response.parsed
    if not isinstance(parsed, Narration):
        raise RuntimeError(f"Gemini did not return the expected schema: {parsed!r}")
    return parsed.answer
```

In `main()`, compute the narration first but print the table no matter what:

```python
    narration = narrate(question, colnames, rows)

    print(f"\nSQL: {sql}\n")
    if narration:
        print(narration)
        print()
    print(colnames)
    for row in rows:
        print(row)
```

Three details worth the ink:

- **The asymmetry is the point.** `get_sql` still fails hard on an `APIError` — without
  SQL there is no answer to degrade *to*. `narrate` fails open, because the answer already
  exists and prose is a rendering of it. Match the failure mode to what the output is
  *for*: required output fails loudly, decorative output degrades quietly.
- **`except errors.APIError` and nothing wider.** The `RuntimeError` from the schema check
  and the `ValueError` from `validate_sql` still propagate, and should — those mean
  something is *wrong*, not merely *unavailable*. A bare `except Exception` here would
  silently swallow a real bug and print a table beneath a missing sentence, and you'd
  never know why.
- **The notice goes to `stderr`.** Then `uv run python src/ask_question.py "..." >
  answer.txt` captures the answer and leaves the complaint on your terminal, which is what
  the two streams are for.

By the time `narrate` runs, a `401` is impossible — `get_sql` would already have died on
it — so catching `APIError` broadly here can't mask a bad key. That's not luck; it's a
consequence of the required call happening first.

---

## Step 12 — Commit

Force the guard to fire before you trust it. Temporarily point `MODEL_NAME` at a model
that doesn't exist (`"gemini-does-not-exist"`), run a question, and confirm you still get
your table plus a one-line `[narration unavailable: 404 NOT_FOUND]` on stderr — then put
the real name back. A guard you have never seen trigger is a guard you have not tested.

```fish
git add src/ask_question.py
git commit -m "fix: retry transient Gemini errors and degrade gracefully when narration fails"
```

---

## Step 13 — A wrong answer that looks exactly like a right one

Ask the pipeline something that should be easy:

```
➤ uv run python src/ask_question.py "how many different kinds of orange products we have?"

SQL: SELECT count(DISTINCT product_name) AS orange_product_count
     FROM v_sales_margin WHERE category = 'Orange';

There are 0 different kinds of orange products.

['orange_product_count']
(0,)
```

You sell Navel Orange and Valencia Orange. The correct answer is 2. The program said 0, in
a complete, confident, grammatically perfect sentence.

Before fixing anything, it's worth understanding *why* this happened, because this failure
is a different species from the `503` in Step 11, and it's much more dangerous.

### Nothing in the program malfunctioned

Go through every safety mechanism you have built so far and check what each one did with
this query.

**The `llm_reader` role allowed it.** The query is `SELECT count(...) FROM v_sales_margin`.
That is a read from one of the three views the role was granted access to. The role exists
to permit exactly this. It has no opinion about whether the `WHERE` clause makes sense.

**`validate_sql` allowed it.** That function checks three things: the text is a single
statement, it begins with `SELECT`, and it contains no forbidden keyword like `DELETE`.
This query passes all three. `validate_sql` has no way to know that `'Orange'` is not a
real category.

**The narrator followed `NARRATOR_PROMPT` perfectly.** You told it: every number you state
must appear exactly as-is in the rows; never compute anything; never infer anything. It was
handed a single row containing the number `0`. It wrote a sentence containing the number
`0`. It obeyed you completely.

**And the `check_grounded` function from Step 9 would have passed too.** That function
pulls the numbers out of the narrator's sentence and confirms each one appears somewhere in
the rows. The sentence contains `0`. The rows contain `0`. Check passes.

So: four separate safety mechanisms, all working exactly as designed, and the program
printed something false.

### Why this matters more than a crash

There is a general lesson here, and it is the reason this step exists.

Those safety mechanisms all answer the same kind of question: *did this number really come
from the database?* That is a question about where the number came from. None of them
answer a different question: *did we ask the database the right thing?* That is a question
about whether the query matched the user's intent.

Checking where a number came from is not the same as checking whether it answers the
question. You can have perfect assurance that a number came out of Postgres, and still
print a number that is completely irrelevant to what the user asked. That is what happened
here.

This is why a `503` error is, in a real sense, a *good* failure. It is loud. It stops the
program. Nobody ever pastes a stack trace into a slide deck and presents it to management.
A wrong-but-fluent sentence, on the other hand, looks precisely like a correct one. It will
get copied, quoted, and believed. **The failures worth fearing are the quiet ones.**

### A guard that looks right and does nothing

Having seen the problem, the obvious defensive move is to refuse to narrate an empty
result:

```python
if not rows:
    ...   # refuse to narrate
```

Read that carefully against the actual output above. It would never have run.

`rows` here is `[(0,)]` — a list containing one tuple, which contains the number zero. That
list is **not empty**. It has one element in it. So `not rows` is `False` and the guard is
skipped entirely.

The reason is worth spelling out. Functions like `count()`, `sum()`, and `avg()` are
*aggregate* functions: they collapse however many rows matched into a single summary row.
If a thousand rows match, `count()` returns one row saying `1000`. If **zero** rows match,
`count()` still returns one row — saying `0`. An aggregate query never returns zero rows.

So the emptiness guard would catch a query like `SELECT DISTINCT product_name ... WHERE
<bad filter>`, which genuinely returns nothing. It cannot catch `SELECT count(...) WHERE
<bad filter>`, which returns one row containing zero. And the query that actually produced
our false answer is the second kind.

That is not a small detail. The most natural guard you would reach for happens to miss
precisely the case that hurt you.

### The root cause: the model had to guess a value

A **literal** is a fixed value written directly into a query — the `'Orange'` in `WHERE
category = 'Orange'`. To write a correct literal, the model has to know which values
actually exist in that column. So: what did we tell it?

| column | values that really exist in the database | what `VIEW_SCHEMA` tells the model |
|---|---|---|
| `channel` | Hypermarket, Supermarket, Wholesale, Ecommerce | all four, listed out |
| `region` | North, Central, South | all three, listed out |
| `category` | Berries, Citrus, Pome, Tropical | **nothing at all** |

There it is. In this database, oranges are filed under the category `Citrus`. The model was
asked to filter on `category`, was never shown which values that column can hold, and so it
had to invent one. Given a fruit company and a question about oranges, `'Orange'` is a
*sensible* guess. The model was not hallucinating wildly or ignoring instructions. It was
doing the only thing available to it, because we withheld the information it needed.

Now go back and reread Step 3. It opens by arguing that a raw column dump is not enough,
because the model "won't know `channel` only ever takes 4 values." It then lists the values
for `channel`. It lists the values for `region`. It mentions `category` three separate
times without ever saying what goes in it.

The walkthrough described this exact bug, and then shipped it. That is not simple
carelessness, and the next section explains why.

### Fix 1 — read the allowed values out of the database instead of typing them by hand

The quick repair is obvious: add a line to `VIEW_SCHEMA` saying `category is one of:
Berries, Citrus, Pome, Tropical`. Do that, and today's bug goes away.

But stop and ask how the bug got in. `VIEW_SCHEMA` is a block of text that a human types
and keeps up to date by hand. A human forgot one column. Nothing warned anybody, because
there is no mechanism that compares that text against the actual database.

Which means: suppose next quarter somebody adds a fifth category, `Stone Fruit`. The text
in `VIEW_SCHEMA` still lists four. The model now silently cannot filter on the fifth. Same
bug, new value, no error message, another confidently wrong answer. Patching the one
missing line fixes today's symptom and leaves the cause untouched.

**Any description of data that a human maintains by hand will eventually disagree with the
data.** The only durable fix is to stop maintaining it by hand.

The `llm_reader` role can already read these three views. So let the database tell you its
own values, at startup:

```python
from psycopg import sql

# Columns that hold a small, fixed set of values worth listing for the model.
ENUM_COLUMNS = [
    ("v_sales_margin", "category"),
    ("v_sales_margin", "channel"),
    ("v_delivery_performance", "region"),
]


def load_enums(conn: psycopg.Connection) -> str:
    lines = []
    with conn.cursor() as cur:
        for view, col in ENUM_COLUMNS:
            cur.execute(
                sql.SQL("SELECT DISTINCT {} FROM {} ORDER BY 1").format(
                    sql.Identifier(col), sql.Identifier(view)
                )
            )
            values = [r[0] for r in cur.fetchall()]
            if len(values) > 20:
                continue          # too many values to be a fixed set — skip it
            joined = ", ".join(str(v) for v in values)
            lines.append(f"{view}.{col} is one of exactly: {joined}")
    return "\n".join(lines)
```

Two things in there deserve a proper explanation.

**Why `sql.Identifier` and not an f-string.** Here you are pasting *table and column names*
into a query that you are building yourself. Those are called identifiers, and
`psycopg.sql.Identifier` quotes them correctly for you.

You might reasonably object: didn't we already give up on this in `main()`, where the
comment explains that `cast(LiteralString, sql)` is fine? No — and the difference is worth
being precise about. In `main()`, the *entire query text* comes from the language model. We
cannot sanitise our way to safety there, so we do something stronger: we run it as a
database role that is physically incapable of doing damage. The role is the protection, and
the `cast` is just us telling the type-checker we know what we are doing.

Here the situation is the reverse. *We* are constructing the query, out of pieces. There is
a proper tool for constructing queries out of pieces, and it costs nothing to use it. The
earlier `cast` was an escape hatch for a specific, unavoidable situation. It is not a habit
to carry into code where the normal tool works fine.

**Why the `len(values) > 20` check.** Listing every allowed value only makes sense for
columns that hold a small, fixed set — four categories, four channels, three regions. It
would be absurd for `product_name`, `brand`, or `city`, where there are many values and new
ones appear whenever a row is inserted. You would bloat the prompt and *still* be out of
date immediately. The number 20 is a rough dividing line between "small fixed set" and
"open-ended list," not a magic constant.

One thing to be clear about first, because the name is misleading: this is not "the model
loading its own values." It is **your Python** reading the real values out of the database
and pasting them into the prompt text before the request is sent. The model never runs
`load_enums` — it just receives a better-informed prompt. You are the one closing the gap
between what the prompt *claims* the columns contain and what they *actually* contain.

**Where the code goes.** All of it lives in `src/ask_question.py` at module level: the
`from psycopg import sql` import at the top with the others, `ENUM_COLUMNS` next to your
other constants, and `load_enums` as a top-level function alongside `get_sql` and
`validate_sql`. There is no separate script and no separate command to run — `load_enums`
is *called from inside `main()`*, once per invocation, right before `get_sql`.

**The restructure this forces.** Until now, `main()` connected to Postgres *after* calling
`get_sql`. That no longer works: `load_enums` needs a live connection, and its output has
to be appended to `SYSTEM_PROMPT` *before* the model sees it. So the connection has to move
to the top of `main()`, and the same connection is then reused to run the model's SQL:

```python
def main():
    question = sys.argv[1]

    conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"])
    with conn:
        # 1. read the real column values FROM the db and build the prompt from them
        system_prompt = SYSTEM_PROMPT + "\n" + load_enums(conn)

        # 2. now the model sees the true enums, not a hand-typed list that can drift
        query = get_sql(question, system_prompt)
        validate_sql(query)

        # 3. reuse the SAME connection to run the model's SQL
        with conn.cursor() as cur:
            cur.execute(query)
            colnames = [desc.name for desc in cur.description]
            rows = cur.fetchall()

    narration = narrate(question, colnames, rows, query)

    print(f"\nSQL: {query}\n")
    if narration:
        print(narration)
        print()
    print(colnames)
    for row in rows:
        print(row)
```

That means **`get_sql` now takes the prompt as a second argument** — before this step it
read the module-level `SYSTEM_PROMPT` directly; now it must receive the enum-augmented one:

```python
def get_sql(question: str, system_prompt: str) -> str:
    ...
    config=types.GenerateContentConfig(
        system_instruction=system_prompt,   # was SYSTEM_PROMPT
        ...
    )
```

Two smaller things that will otherwise trip you up:

- **The `sql` name looks like a collision, but isn't.** You're importing `from psycopg
  import sql` (the module) while the query variable was called `sql` in earlier steps.
  They live in different function scopes — `load_enums` uses the module, `main` uses the
  local — so it works, but it's a footgun. That's why the query variable is renamed to
  `query` above. If you keep the name `sql` for the query, know that inside `main` you then
  cannot also call `sql.Identifier`, because the local name shadows the import *in that
  function*. Renaming removes the hazard entirely.
- **`load_enums` reuses the `llm_reader` connection, and that's fine** — reading `SELECT
  DISTINCT category FROM v_sales_margin` is exactly the read-only access that role already
  has. No new privilege, no second connection.

Once this is wired, the prompt cannot disagree with the database, because the prompt is
generated *from* the database every time the script runs.

(Forward note, so it doesn't surprise you later: in **Step 15** this same block gets pulled
out of `main()` into a function called `answer_question(conn, question)`, so a test can call
it too. The enum-loading moves there unchanged — you're not undoing this work, just
relocating it.)

### Fix 2 — tell the model not to guess values it hasn't been shown

`load_enums` covers the three small columns. It deliberately skips `product_name`, `brand`,
`city`, and `route`, because those have too many values to list. But the model still needs
to filter on them sometimes, and it still has not seen their contents. Left alone, it will
guess a literal there for exactly the same reason it guessed `'Orange'`.

So tell it what to do instead. Append this to `SYSTEM_PROMPT`:

```
For columns whose allowed values are not listed above (product_name, brand, city,
route), never filter with equality on a value you have not been shown. Use
ILIKE '%substring%' instead, so a near-miss still matches.
```

`ILIKE` is Postgres's case-insensitive pattern match, and `'%orange%'` means "contains the
letters o-r-a-n-g-e anywhere." So instead of `product_name = 'Orange'`, which matches
nothing, the model writes `product_name ILIKE '%orange%'`, which matches both `Navel
Orange` and `Valencia Orange`.

That returns **2** — the same answer you would get from `category = 'Citrus'`, reached by a
different route. Either query is now correct.

### Fix 3 — show the narrator the query, so it can tell you when the filter found nothing

Look at what `narrate` currently receives: the question, the column names, and the rows.
That is all. It is never shown the SQL.

This means that when it gets handed `(0,)`, it has genuinely no way to tell the difference
between these two very different situations:

1. We correctly searched for oranges and the company sells none.
2. We searched for a category that does not exist, so of course nothing matched.

From inside `narrate`, both look identical: one row, containing zero. The narrator did not
*fail* to add a caveat — it lacked the information required to know a caveat was warranted.
We never gave it the filter.

So pass `sql` in as a third argument, and add a rule to `NARRATOR_PROMPT`:

```
- If the result is empty or a zero count, do NOT assert that none exist. Say that the
  query matched no rows, and quote the filter it used, so the reader can judge whether
  the filter was the right one.
```

The failure now reads something like: *"The query filtered on `category = 'Orange'` and
matched no rows."* That sentence is **true**. It is useful. And it points a human straight
at the mistake, because anyone who knows the data will immediately notice that `Orange` is
not a category.

Compare that to *"There are 0 different kinds of orange products."* That sentence is false,
and worse, it sends the reader off in the wrong direction entirely.

One thing to be clear about: showing the narrator the query does not let it start
calculating things. It still may not compute, derive, or estimate any number. The extra
context lets it be honest about what was actually asked. It does not grant it permission to
reason about the data.

### A stronger guard exists — here's why we're not building it

Be clear about what Fixes 1, 2, and 3 are. All three are instructions in a prompt. Step 1
spent a page explaining that an instruction in a prompt is a *request*, not a *wall*. These
are requests. A model that ignores them produces a wrong answer again.

A real wall is possible. You could take the SQL the model produced, pull out every quoted
string in it, and check each one against the true values that `load_enums` just read from
the database. If the model wrote `category = 'Orange'` and `Orange` is not among the four
real categories, you reject the query and never run it — the same way `validate_sql`
rejects a `DELETE` before it reaches Postgres.

Notice that this is only possible *because* Fix 1 loaded the real values into memory. This
is the same pattern as Step 9, where rounding the numbers inside SQL is what made the
`check_grounded` check cheap to write. A good decision early on is often what makes a
verification step possible at all later.

We are still not going to build it, and the reason is honest rather than lazy. To find the
quoted strings in a SQL statement, you have to parse SQL — and the practical way to do that
in twenty lines is a regular expression. This walkthrough has now argued against
regex-scraping structured text twice: in Step 4, where scraping a code fence out of the
model's prose was brittle, and in Step 9, where `check_grounded` trips over a number
reformatted as `6,031,937.59`. A regex that parses SQL badly will reject valid queries and
accept invalid ones, and when it goes wrong it produces exactly the kind of quiet, confident
failure this whole step is about. The extra safety it buys over Fixes 1 and 3 is small.

Know that the wall can be built, and that here we are choosing not to build it. That is a
different thing from not having noticed.

---

## Step 14 — Commit

Check the fix against the question that exposed the bug, and against one it should leave
completely alone:

```fish
uv run python src/ask_question.py "how many different kinds of orange products we have?"
# expect 2 — either via ILIKE on product_name, or via category = 'Citrus'

uv run python src/ask_question.py "which region has the worst temperature breach rate?"
# expect South 12.4%, Central 8.2%, North 4.0% — Step 13 should not have changed this
```

Then confirm the caveat from Fix 3 actually appears. Ask something whose true answer really
is nothing, such as `"how many kiwi products are in the Pome category?"` — kiwis are
Tropical, so the honest answer is zero. You want a sentence that names the filter it used,
not a flat claim that none exist.

```fish
git add src/ask_question.py
git commit -m "fix: read column values from the database so the model never guesses a literal"
```

---

## Step 15 — Checking the answers are right, without reading them by hand every time

Everything up to here has made the pipeline *better*. This step makes it *measurable* —
and that shift is the whole point of the step, so it's worth slowing down on why it
matters before touching any code.

Think about what you actually did in Step 14. You ran two questions, you looked at the
two answers with your own eyes, and because you happened to already know the right totals
(6,031,937.59 and the South/Central/North ranking), you could tell they were correct.
That works. But look closely at what it depends on:

1. **It depends on you already knowing the answer.** You can only eyeball-check a question
   whose true answer you've memorised. That doesn't grow past a handful of questions.
2. **It depends on you re-doing it every time you change anything.** Every time you touch
   `SYSTEM_PROMPT`, or add a column value, or upgrade the model, in principle you should
   re-check *all* the questions again — because a change that fixes one question can
   quietly break another. Nobody actually does this by hand for long. You get tired, you
   check one question instead of ten, and a regression slips through.
3. **"It looked right" is not a number.** You can't put "I looked at it and it seemed
   fine" in front of anyone. You can't compare last week's version to this week's.

So the thing we're missing is a way for the *computer* to check its own answers against
answers we already know are true — automatically, the same way every time, as many
questions as we like. That collection of "question + the answer we know is correct" is
usually called a **gold set** or an **eval set** ("eval" is just short for "evaluation").
It is the single most important idea in this whole step. Everything below is mechanics.

### Chapter 0: the check that lives in your head

Right now your "test suite" is a mental checklist: *"total margin should be about six
million, breach rate worst in South."* It isn't written down anywhere. It isn't run by
anything. It exists only while you're actively thinking about it, and it evaporates the
moment you move on. The first and most important move is simply to **write the checklist
down in a form a program can run** — turn the thing in your head into data.

### Chapter 1: a list of questions with known answers

Here is the checklist as data — a plain Python list, where each entry pairs a question
with the answer you already know is correct:

```python
CASES = [
    {"question": "what's our total margin?", "expected": 6031937.59},
    {"question": "how many different kinds of orange products we have?", "expected": 2},
]
```

Two entries is enough to start. (The second one is deliberate: it's the exact bug from
Step 13. Once it's in the eval set, that specific wrong answer can never quietly come back
without the eval catching it — a bug you've written a test for is a bug that stays dead.)

But before this list is useful, there's a problem to solve: **the test code needs to
actually run the pipeline, and right now it can't.** All of your logic lives inside
`main()`, which is hard-wired to read the question from `sys.argv[1]` and `print` the
result to the screen. A test can't call that — it has no question to pass in and no answer
handed back, only text printed to a terminal. So the first real change is to **separate
"do the work" from "read input and print output."**

Pull the core of `main()` out into its own function that takes a question and *returns*
the result instead of printing it:

```python
def answer_question(
    conn: psycopg.Connection, question: str
) -> tuple[str, list[str], list[tuple]]:
    system_prompt = SYSTEM_PROMPT + "\n" + load_enums(conn)
    sql = get_sql(question, system_prompt)
    validate_sql(sql)
    with conn.cursor() as cur:
        cur.execute(sql)
        colnames = [desc.name for desc in cur.description]
        rows = cur.fetchall()
    return sql, colnames, rows
```

(If your `get_sql` currently reads the module-level `SYSTEM_PROMPT` directly instead of
taking it as an argument, give it a parameter now — the eval and `main` both need to hand
it the enum-augmented prompt from Step 13.)

Now `main()` becomes thin — it only deals with input and output, and hands the real work
to `answer_question`:

```python
def main():
    question = sys.argv[1]
    conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"])
    with conn:
        sql, colnames, rows = answer_question(conn, question)
    narration = narrate(question, colnames, rows, sql)

    print(f"\nSQL: {sql}\n")
    if narration:
        print(narration)
        print()
    print(colnames)
    for row in rows:
        print(row)
```

Nothing about the behaviour changed — you can still run the script exactly as before. But
now `answer_question` is a reusable piece that *anything* can call, including a test.

### Why we compare the answer, not the SQL

Here's a tempting idea that's worth rejecting out loud. You might think: "I'll store the
*correct SQL* for each question, and check that the model produces that exact query."

Don't. There are many different SQL queries that all produce the right answer —
`SELECT sum(margin)` and `SELECT sum(revenue - cost)` and a version with a redundant
`WHERE true` are all correct. If you check the query *text*, you'll fail perfectly good
answers just because the model phrased the SQL differently than you did. You'd be testing
*how* it got the answer instead of *whether the answer is right.*

So the rule is: **check the output value, not the query that produced it.** You don't care
how the model got to 6,031,937.59; you care that it got there. This is the same instinct
as testing a function by its return value rather than its internal variable names.

To keep that comparison simple, we'll deliberately fill the eval set with questions whose
answer is a **single number** — one row, one column. That's why both starter cases are
counts/sums. It means "the answer" is just `rows[0][0]`, one value we can compare directly,
instead of having to line up whole tables. (You *can* eval multi-row answers later; it's
just more fiddly, and single-number questions already exercise the whole pipeline.)

### The rounding wrinkle

One catch: Postgres hands numbers back as `Decimal` objects, and money often has trailing
digits. `Decimal('6031937.59')` is not `==` to the float `6031937.59` in a naive
comparison, and you don't want a test that fails over the last decimal place. So compare
with a small tolerance instead of exact equality:

```python
def matches(got, expected) -> bool:
    return abs(float(got) - float(expected)) <= 0.01
```

Convert both sides to `float`, subtract, and accept anything within a cent. That's enough
for counts (which are exact) and money (where sub-cent differences don't matter).

### The wrinkle that actually matters: the model isn't deterministic

Here is the part that makes evaluating an LLM pipeline genuinely different from testing
ordinary code, and it connects straight back to Step 11.

Ordinary code is deterministic: same input, same output, every single time. If a normal
unit test passes once, it passes always (until the code changes). **The model is not like
this.** Ask it "what's our total margin?" ten times and it might write a slightly
different query each time — usually all correct, but occasionally one is subtly wrong. This
means a single run tells you almost nothing. A pass could be luck; a fail could be a fluke.

So an LLM eval is not a yes/no. It is a **rate.** You run each question several times and
measure *how often* it gets the right answer. "9 out of 10" is a real, honest description
of the pipeline; "it passed" is not. This is the same shift in thinking as Step 11's retry
math, where a 20% per-call failure rate was something you reasoned about as a probability
rather than pretended was zero. Here you're *measuring* that probability instead of
assuming it.

### The eval script

Put this in a new file, `src/eval_pipeline.py` (not `eval.py` — `eval` is a built-in
Python function and shadowing it invites confusion):

```python
import os
import time

import psycopg
from dotenv import load_dotenv

from ask_question import answer_question

load_dotenv()

CASES = [
    {"question": "what's our total margin?", "expected": 6031937.59},
    {"question": "how many different kinds of orange products we have?", "expected": 2},
]

RUNS_PER_CASE = 3      # each question is asked this many times, because the model varies
TOLERANCE = 0.01
REQUEST_PAUSE = 4      # seconds to wait between requests — see "Mind the rate limit" below


def matches(got, expected) -> bool:
    return abs(float(got) - float(expected)) <= TOLERANCE


def main():
    # autocommit=True is deliberate — see the note below.
    conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"], autocommit=True)

    total_runs = 0
    total_passed = 0
    with conn:
        for case in CASES:
            question = case["question"]
            expected = case["expected"]
            case_passed = 0

            for _ in range(RUNS_PER_CASE):
                total_runs += 1
                try:
                    sql, colnames, rows = answer_question(conn, question)
                    got = rows[0][0]
                    if matches(got, expected):
                        case_passed += 1
                        total_passed += 1
                    else:
                        print(f"  WRONG: got {got!r}, expected {expected!r}")
                        print(f"         sql: {sql}")
                except Exception as e:
                    print(f"  ERROR: {e}")

                time.sleep(REQUEST_PAUSE)   # don't burst — the free tier caps requests per minute

            print(f"{case_passed}/{RUNS_PER_CASE}  {question}")

    pct = 100 * total_passed / total_runs if total_runs else 0
    print(f"\nOverall: {total_passed}/{total_runs} correct = {pct:.0f}%")


if __name__ == "__main__":
    main()
```

Three things worth reading closely:

- **`autocommit=True` on the connection is not decoration — it's a correctness fix.**
  Normally psycopg runs your statements inside a transaction. The problem: if one run in
  the loop sends SQL that errors (say the model writes something invalid), Postgres marks
  the *whole transaction* as aborted, and then every following query in the loop fails too
  — one bad run would poison all the runs after it, and your eval numbers would be
  garbage. Turning on autocommit makes each statement stand alone, so a single failure
  stays contained to that one run. These are all read-only `SELECT`s, so there's nothing
  to commit anyway — autocommit costs you nothing here and saves you from a confusing
  cascade of fake failures.
- **A failed run prints the wrong value *and* the SQL.** Same reasoning as Step 7: when a
  case scores 2/3, you want to see the query that went wrong to understand *how* the model
  drifted — was it a bad literal? a wrong column? — not just that it did.
- **`time.sleep(REQUEST_PAUSE)` after every run is not politeness — it's what keeps the
  eval from rate-limiting itself.** The next subsection explains why; the short version is
  that firing every request back-to-back trips the free tier's per-minute cap.

### Mind the rate limit — eval is a deliberate batch, not a continuous check

There's a practical reality the free tier forces on you, and it's better to build it into
how you think about eval than to fight it. Every run here is a real API request, and the
free tier caps how many you can make — both per minute and per day (the exact numbers
drift, so check them live at the dashboard the `429` error links to). Fire a burst of
requests back-to-back and you'll trip the per-minute wall and get a
`429 RESOURCE_EXHAUSTED`, with the server telling you to retry in ~30 seconds.

That's why two constants in the script are set the way they are:

- **`RUNS_PER_CASE = 3`, not 20.** Three runs is enough to see a *rate* — 0/3, 1/3, 2/3,
  3/3. You don't need dozens; you need just enough to catch a question that's only
  *sometimes* right. More runs mostly buys precision you don't need and requests you can't
  spare.
- **`REQUEST_PAUSE = 4` seconds between calls.** Eval isn't interactive — nobody is waiting
  on it — so slowness is free. Spacing the calls a few seconds apart keeps you comfortably
  under the per-minute cap instead of bursting past it. A 6-request eval then takes about
  half a minute, which is nothing for something you run occasionally.

But the deeper point is *cadence*. Eval is **not** something you run on every save. It's a
deliberate ritual you run at specific moments — before a commit that changed the prompt,
the model, or `load_enums` — to confirm you didn't regress. Run continuously it is both
wasteful and (on a free tier) impossible; run deliberately, a few times a day, it is
exactly right. Even with generous *paid* quota you'd design it this way, because burning
API calls on every keystroke is silly no matter who's paying. The rate limit is just
enforcing a discipline you'd want regardless.

And be honest about the tradeoff you're standing in, because it's a good one to be able to
articulate. This phase chose a free-tier model *on purpose*, so anyone who clones your repo
can run it without a credit card. The price of "free and runnable by anyone" is "not much
throughput." In a real deployment you'd have paid quota and run this eval in CI on every
pull request — the *skill* is identical, only the cadence changes. Being able to say
*"eval is request-hungry, so I run it as a pre-commit batch and would move it to CI with
paid quota"* is exactly the kind of judgement the job is asking about.

One caveat to know about the accuracy number when a limit is hit: a `429` is caught by the
same `except Exception` as a genuinely wrong answer, so it counts as a **miss**. That means
a rate-limit error slightly *understates* your true accuracy — a run that never got to ask
the model isn't the model being wrong. For a rough measure that's fine, and it's why the
eval keeps going instead of crashing when it hits the cap. If it ever bothers you, catch
the API error separately from a wrong-value miss and report the two counts differently.

### Running it and reading the result

```fish
uv run python src/eval_pipeline.py
```

You'll see something like:

```
3/3  what's our total margin?
3/3  how many different kinds of orange products we have?

Overall: 6/6 correct = 100%
```

Two runs from now that might read `5/6 = 83%` instead, and **that is fine and expected**
— it's the honest face of a non-deterministic system, not a bug you must chase to zero.
What you've gained is enormous and easy to undersell: you can now say a real sentence about
your pipeline — *"it answers this set of questions correctly ~95% of the time"* — and you
can re-run this after **any** change to see whether you helped or hurt. That is the exact
thing a person pasting files into a chatbot cannot tell you about their own setup, and it's
squarely the kind of "does it actually work, and how do you know" judgement the job is
really asking for.

### Growing the set (do this by hand, carefully)

The set is only as trustworthy as the `expected` values in it. Never guess them. To add a
case, first run the query yourself in `make psql` to find the true answer, *then* write it
down:

```fish
make psql
```
```sql
-- find a real answer to record as 'expected'
SELECT count(*) FROM v_delivery_performance WHERE temp_excursion;
```

Take whatever number that returns and add `{"question": "...", "expected": <that number>}`
to `CASES`. A dozen well-chosen questions — a couple of sums, a couple of counts, one that
should return zero, one with a filter on each enum column — is a genuinely useful eval and
plenty for a portfolio project.

### Commit

```fish
git add src/ask_question.py src/eval_pipeline.py
git commit -m "feat: measure pipeline accuracy with a repeatable eval set"
```

---

## Step 16 — Leaving a record of what was asked

There's one thing a real deployment needs that the pipeline still doesn't do at all: it
keeps **no record**. You ask a question, an answer scrolls up your terminal, and then it's
gone. Now imagine this were actually running at a company and someone asks, next month:
*"Which numbers did people pull from this thing, and what exact query did it run to get
them?"* Right now you cannot answer. There's no history.

This is the last governance piece. Step 2 controlled *what the LLM is allowed to touch*.
This step records *what it actually did* — an **audit log.** In a real setting these two
things together are the difference between "an AI has access to our data" (scary) and "an
AI has read-only access to these three views and every question it's ever asked is on
record" (defensible).

### Chapter 0: print it to a file

The simplest possible version: every time the script runs, append a line to a text file.

```python
with open("audit.log", "a") as f:
    f.write(f"{question} | {sql}\n")
```

This is better than nothing, and for about a day it's fine. Then the cracks show. It's
unstructured text, so answering "show me every question from last Tuesday" means parsing
lines by hand. Two runs at the same moment can interleave and corrupt a line. And the
record lives in a loose file next to your data instead of *in* the database with
everything else. You already have a real database sitting right there — the log belongs in
it, as rows.

### Chapter 1: write it to a table — and discover the wall works

So make a table and insert a row per question. But the moment you try, you hit something
instructive:

**The `llm_reader` role cannot do this.** Back in Step 2 you granted it `SELECT` on three
views and *nothing else* — no `INSERT`, on anything. So an `INSERT` sent over the
`DATABASE_URL_LLM_PLAIN` connection will be refused by Postgres. That refusal is not a
problem to work around — **it's Step 2's wall doing exactly its job.** You deliberately
built a connection that cannot write, and now it can't write. Good. Don't weaken it.

The tempting shortcut is: "just use the `ops` superuser connection
(`DATABASE_URL_PLAIN`) to write the audit row." Reject that too, and it's worth being
clear about why. The whole spirit of Step 2 is *least privilege* — every connection should
be able to do the least it needs and no more. The part of the program that writes the
audit log needs to do exactly one thing: **append a row to one table.** It never needs to
read the views, never needs to touch a raw table, never needs to update or delete
anything. Handing it the full `ops` connection gives it the power to do all of those — a
much bigger blast radius than the job requires, for no reason.

### Chapter 2: a third role that can only append

So we do the same thing Step 2 did, again, for this new job: create a **dedicated role
whose entire power is "insert into the audit table."** Not read it, not change it, not
touch anything else. A new migration:

```fish
make db-create name=add_query_audit
```

```sql
-- +goose Up
CREATE TABLE query_audit (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asked_at     timestamptz NOT NULL DEFAULT now(),
    question     text        NOT NULL,
    generated_sql text,
    row_count    integer,
    error        text
);

CREATE ROLE auditor LOGIN PASSWORD 'auditor_pw';
GRANT USAGE ON SCHEMA public TO auditor;
GRANT INSERT ON query_audit TO auditor;

-- +goose Down
REVOKE INSERT ON query_audit FROM auditor;
REVOKE USAGE ON SCHEMA public FROM auditor;
DROP ROLE auditor;
DROP TABLE query_audit;
```

Look at what `auditor` can and cannot do, because the *cannots* are the whole point:

- It has `INSERT` on `query_audit` — it can add a record. That's all it's for.
- It has **no `SELECT`**, even on `query_audit` itself — so this role cannot read back the
  history. Reading the audit log is an administrator's job (done as `ops`), not something
  the question-answering script should be able to do. The writer of a log shouldn't be able
  to browse everyone else's entries.
- It has **no `UPDATE` and no `DELETE`** on anything — so it cannot alter or erase a record
  once written. The log is **append-only**: nobody using this role can go back and quietly
  change history to cover a mistake. That tamper-resistance is most of what makes an audit
  log worth having.
- It has no access to the views or raw tables at all — a completely separate concern from
  `llm_reader`, kept completely separate.

**One detail about `GENERATED ALWAYS AS IDENTITY` and permissions** (this is a nice reason
the project uses identity columns everywhere). With an old-style `serial` column you'd
also have to `GRANT USAGE` on a separate sequence, or the `INSERT` would fail with a
sequence-permission error. Identity columns are managed by the table itself, so `INSERT`
on the table is all `auditor` needs — no separate sequence grant. If, when you verify
below, an insert somehow *does* complain about a sequence, that's the thing to grant; but
with identity it shouldn't.

Run it:

```fish
make db-migrate
make db-status
```

Add the new connection string to `.env` — a third one, separate from the other two:

```
DATABASE_URL_AUDIT_PLAIN=postgres://auditor:auditor_pw@localhost:5433/coldchain?sslmode=disable
```

**Verify the role does exactly what it should and nothing more** (read-only proof — it
tries one thing it's allowed and one it isn't):

```fish
psql "postgres://auditor:auditor_pw@localhost:5433/coldchain?sslmode=disable" \
  -c "INSERT INTO query_audit (question, generated_sql, row_count) VALUES ('probe', 'SELECT 1', 1);" \
  -c "SELECT * FROM query_audit;"
```

Expect the `INSERT` to succeed (`INSERT 0 1`) and the `SELECT` to fail with
`permission denied for table query_audit`. That failure is the proof: the role can write
the log but cannot read it — precisely the shape you wanted.

### Wiring it into the script

Add a small function that writes one audit row, over the new `auditor` connection:

```python
def write_audit(
    question: str, generated_sql: str | None, row_count: int | None, error: str | None
) -> None:
    with psycopg.connect(os.environ["DATABASE_URL_AUDIT_PLAIN"]) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO query_audit (question, generated_sql, row_count, error) "
            "VALUES (%s, %s, %s, %s)",
            (question, generated_sql, row_count, error),
        )
```

The single most important thing to notice here, and the reason this is safe even though
it uses a writing connection: **the model's SQL is passed as a `%s` parameter — as
data, not as a command.** The only statement that ever *executes* on this connection is
the fixed `INSERT` string you wrote by hand. The model's `generated_sql` rides along as a
plain text value to be *stored*, exactly like `question`. It is never run here. So there's
no way for a weird query the model produced to "do" anything on the audit connection — it's
just a string getting filed away. (This is the same `%s`-parameter discipline that keeps
`load_enums` safe in Step 13, applied to the write side.)

Now record every question in `main()` — and crucially, record failures too, because a
question that *failed* is often the most interesting thing in an audit log:

```python
def main():
    question = sys.argv[1]
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"])
        with conn:
            sql, colnames, rows = answer_question(conn, question)
    except Exception as e:
        write_audit(question, None, None, str(e))
        raise

    write_audit(question, sql, len(rows), None)

    narration = narrate(question, colnames, rows, sql)

    print(f"\nSQL: {sql}\n")
    if narration:
        print(narration)
        print()
    print(colnames)
    for row in rows:
        print(row)
```

Two things about the placement:

- **The audit write happens right after the query runs, before narration.** Narration is
  the optional, fail-open decoration from Step 11 — whether the pretty sentence succeeded
  or not has nothing to do with what the pipeline actually *did*, which is what the log
  records. So the log is written based on the real work (the SQL and its rows), not on
  whether the cosmetic layer worked.
- **A failure is logged and then re-raised.** If the query errors, you still write a row —
  with the error text and no SQL — so the log captures the attempt, and *then* let the
  exception propagate as before. The log is a record of what happened; it doesn't swallow
  problems.

### Reading the log (as an admin, i.e. `ops`)

The `auditor` role can't read the log — by design — so you read it as yourself:

```fish
make psql
```
```sql
SELECT asked_at, question, row_count, error
FROM query_audit
ORDER BY asked_at DESC
LIMIT 10;
```

There's the history: every question, when it was asked, how many rows came back, and the
error text for any that failed. That's the artifact that turns "we let an AI query our
data" into something a cautious manager can actually sign off on.

### Commit

Before committing, run a normal question and confirm a row lands in the log, then force a
failure and confirm *that* lands too — a log you've only ever seen record successes is a
log you haven't really tested:

```fish
uv run python src/ask_question.py "what's our total margin?"
# then, in make psql, confirm a new row with row_count = 1 and error = NULL

uv run python src/ask_question.py "nonsense that should produce no valid query"
# this may error; confirm a row appears with the error text filled in
```

```fish
git add migrations/*_add_query_audit.sql src/ask_question.py
git commit -m "feat: append-only audit log of every question, via an insert-only role"
```

---

## Step 17 — When the free tier runs dry: a local model for development

By now the rate limit has stopped being a footnote and started being the thing that
interrupts you. It bites hardest exactly where Step 15 warned it would: **during eval.**
A single `uv run python src/eval_pipeline.py` fires `RUNS_PER_CASE × len(CASES)` requests
in a tight loop — and each question is *two* calls (SQL, then narration) — so even a
modest eval set bursts a dozen-plus requests through the free tier in under a minute.
That trips the per-minute `429 RESOURCE_EXHAUSTED` wall, and a few eval runs in an
afternoon can chew through the per-*day* cap entirely, at which point you're locked out
until tomorrow. The `REQUEST_PAUSE` from Step 15 softens the per-minute burst, but it
can't create daily quota out of nothing — and pausing 4 seconds between every call while
you're actively iterating on `SYSTEM_PROMPT` turns a tight feedback loop into a slog.

### Chapter 0: just wait it out

The zeroth option is the one Step 15 already gave you: run eval deliberately, space the
calls, treat it as a batch. That's the right discipline and you should keep it. But it
solves *politeness*, not *scarcity*. When you're mid-change and want to run eval ten times
in twenty minutes to see whether a prompt tweak helped, "wait 30 seconds" and "you're out
for the day" are not pacing problems you can pace your way out of. Slower is not the same
as unblocked.

### The reframe: this task doesn't need the cloud to *develop* against

Look honestly at what the model is being asked to do here. Write one schema-constrained
`SELECT` over three views whose columns and enums you hand it explicitly, and later a
two-sentence narration that may only quote numbers already in front of it. This is a
narrow, bounded task — the whole reason Step 0 justified `flash-lite` over a frontier model
in the first place. A task that narrow doesn't need a datacenter to *iterate* against. A
small model running on your own machine has **no quota at all** — you can run eval fifty
times in an afternoon and the only cost is a few seconds of your laptop's time.

The one thing it is *not* is what you deploy. "It runs on my MacBook" doesn't ship. So the
goal is precise: **keep Gemini as the deployed default, and add a local backend you flip on
for the development loop.** Same code, same eval, same prompts — a different engine behind
one function.

### Why not just point the Gemini client at localhost

The tempting shortcut — construct `genai.Client(...)` with a `base_url` aimed at your local
server — doesn't work, and it's worth knowing why rather than discovering it at runtime.
The `google-genai` SDK speaks Gemini's specific wire protocol. A local runner like Ollama
speaks its own (and, separately, an OpenAI-compatible one). Pointing one client at the
other's endpoint sends requests in a shape the server can't parse. The clean move isn't to
trick the Gemini client — it's to swap in a different client behind a seam, so nothing
*above* the seam can tell which one answered.

### Choosing the model — `qwen2.5-coder:7b`

Two constraints decide this: the task is **SQL generation**, and the machine is a **16 GB
M1 Pro**. The binding one is the first — for text-to-SQL, whether the model was
code/instruction-tuned matters more than raw size. `qwen2.5-coder:7b` is the strongest
small open model for SQL right now; at 4-bit quantization it's about 4.7 GB on disk and in
RAM, which leaves ~10 GB free so it isn't fighting Docker and Postgres for memory. (You
already had `gemma3:4b` pulled from somewhere — it's fine for the easy narration call, but
noticeably weaker at SQL, which is the call that actually matters. Don't split backends
just to save a gigabyte; run one model for both.)

```fish
ollama pull qwen2.5-coder:7b     # ollama itself was already installed
uv add ollama                    # the Python client
```

### The seam: one function, two engines, a validated object either way

Here is the insight that makes this a small change instead of a rewrite. Both engines,
despite different APIs, converge on the *same* end state: **a validated Pydantic object.**
Gemini takes a `response_schema` and hands you `response.parsed`, already validated. Ollama
takes a JSON-schema `format` and hands you JSON *text* that you validate yourself with
`schema.model_validate_json(...)`. Different mechanics, identical result. So the seam is a
function that takes `(system_prompt, contents, schema)` and returns a `schema` instance —
and every caller (`get_sql`, `narrate`) stops caring which engine ran:

```python
LLM_BACKEND = os.environ.get("LLM_BACKEND", "gemini")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

M = TypeVar("M", bound=BaseModel)


def _gemini_structured(system_prompt: str, contents: str, schema: type[M]) -> M:
    response = gemini_client().models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    parsed = response.parsed
    if not isinstance(parsed, schema):
        raise RuntimeError(f"model did not return the expected schema: {parsed!r}")
    return parsed


def _ollama_structured(system_prompt: str, contents: str, schema: type[M]) -> M:
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": contents},
        ],
        format=schema.model_json_schema(),   # the Ollama equivalent of response_schema
        options={"temperature": 0, "num_ctx": 8192},
    )
    content = response.message.content
    if content is None:
        raise RuntimeError("model returned an empty response")
    return schema.model_validate_json(content)


def generate_structured(system_prompt: str, contents: str, schema: type[M]) -> M:
    if LLM_BACKEND == "ollama":
        return _ollama_structured(system_prompt, contents, schema)
    return _gemini_structured(system_prompt, contents, schema)
```

`get_sql` collapses to one line — `generate_structured(system_prompt, question, SQLAnswer).sql`
— and `narrate` calls `generate_structured(..., Narration).answer`. Both original
`generate_content` bodies disappear into the seam. That the same `SQLAnswer` / `Narration`
classes from Steps 4 and 9 work *unchanged* against a completely different model is the
whole payoff of having forced a structured shape back then: the schema is the contract, and
the contract is engine-independent.

### Three details that will bite if you skip them

- **`num_ctx: 8192` is not optional for narration.** Ollama's default context window is
  **2048 tokens**, and it silently truncates anything longer instead of erroring. Your
  narrator prompt stuffs *every returned row* into the message; a wide result quietly pushes
  the top of the prompt — the rules — out of the window, and the model misbehaves for a
  reason you'll never see in a stack trace. Set the window wide enough that the whole prompt
  survives.
- **`temperature: 0` for the same reason Step 15 cared about determinism.** The free-tier
  Gemini path left temperature at its default; locally you want 0, so the SQL is as
  reproducible as a non-deterministic model gets and your eval measures the *prompt*, not
  the sampler.
- **The Gemini client had to become lazy.** Previously the module built
  `genai.Client(api_key=os.environ["GEMINI_API_KEY"])` at import time. But the entire point
  of the local backend is to develop *without* a live Gemini key — and reading
  `os.environ["GEMINI_API_KEY"]` at import would `KeyError` before `LLM_BACKEND=ollama` ever
  got a chance to matter. So construction moved behind `gemini_client()`, built on first use.
  A backend you can turn off has to also turn off everything it demanded — the key included.

### The graceful-degradation catch had to widen

Step 11 made `narrate` fail *open*: if the narrator call dies, print the table anyway. It
caught `errors.APIError` — a Gemini-shaped exception. Ollama fails in its own vocabulary:
`ollama.ResponseError` for an API-level failure, and the builtin `ConnectionError` when the
daemon isn't running at all. So the "unavailable" catch becomes a tuple spanning both
engines, while a *malformed* response (the `RuntimeError` / Pydantic `ValidationError`) still
propagates as the real bug it is — exactly the asymmetry Step 11 argued for, now stated once
for two backends:

```python
LLM_UNAVAILABLE = (errors.APIError, ollama.ResponseError, ConnectionError)
```

### The eval payoff — the thing you came here for

With a local backend, the per-request pause has nothing to pace, so the eval script drops
it automatically:

```python
REQUEST_PAUSE = 0 if os.environ.get("LLM_BACKEND") == "ollama" else 4
```

Now `uv run python src/eval_pipeline.py` runs flat-out, as often as you like, and you can
raise `RUNS_PER_CASE` past 3 to get a tighter accuracy estimate that the free tier could
never have afforded. The rate limit that made eval a rationed ritual is simply gone from the
inner loop.

### Two honesty notes, so this doesn't mislead you

- **Accuracy is per-backend; don't cross-compare.** The local model may score differently
  from `flash-lite` on the same `CASES` — usually a little lower on the trickier SQL. A drop
  when you switch engines is *not* a regression in your prompt. Eval numbers are only
  comparable within one backend.
- **The local eval is a fast proxy, not the final word.** What you deploy is Gemini, so
  before you trust a prompt change *for production*, run the eval once against
  `LLM_BACKEND=gemini` (spending a little of that scarce quota deliberately) to confirm the
  change holds on the real engine. Develop against local for speed; ratify against Gemini
  before you believe it.

### Switching

It's one environment variable. `.env` now carries:

```
LLM_BACKEND=ollama
OLLAMA_MODEL=qwen2.5-coder:7b
```

Comment `LLM_BACKEND` out (or set it to `gemini`) to run against Gemini again. Deployment,
which never sets the variable, gets the `"gemini"` default for free.

### What I changed and ran for this step (you gave the go-ahead)

Unlike Steps 1–16, which I wrote for you to type, here you explicitly asked me to make it
work — so for this step I did edit and run:

- Edited `src/ask_question.py`: added the `ollama` import and `LLM_BACKEND` / `OLLAMA_MODEL`
  constants; made the Gemini client lazy behind `gemini_client()`; added
  `generate_structured` and its two backend helpers; rewrote `get_sql` and `narrate` to go
  through the seam; added the `LLM_UNAVAILABLE` tuple.
- Edited `src/eval_pipeline.py`: made `REQUEST_PAUSE` backend-aware.
- Ran `uv add ollama` (→ `ollama==0.6.2`, recorded in `pyproject.toml` / `uv.lock`) and
  `ollama pull qwen2.5-coder:7b`.
- Added the `LLM_BACKEND` / `OLLAMA_MODEL` lines to `.env` (gitignored, as always).
- Verified end to end against the local model — see the verification note at the bottom.

Nothing touched the database or the schema; the local switch is entirely application-side.

---

## Step 18 — Letting the audit trail earn its columns

Step 16 built the audit log in one sitting, before you had ever *used* it. That's the right
way to start — you can't design a log around questions you haven't asked yet — but it means
the first schema is a guess. Now you've browsed it for real (Step 17's rate-limit debugging,
the Ollama-vs-Gemini comparison), and the guess has visible seams. This step is the
correction, and every change is driven by something the log actually failed to tell you —
not by a checklist of "good audit practice." That distinction matters: **columns should earn
their place by answering a question you really asked.**

### The timestamp isn't wrong — you're reading it in the wrong timezone

The first complaint is the loudest: the `asked_at` values don't match your clock. A row says
`17:12`, it's `01:12 AM` where you sit. The instinct is to "store local time instead." That
instinct is a trap, and resisting it is the single most important audit lesson in this step.

`asked_at` is a `timestamptz`, which does **not** store a wall-clock reading. It stores an
*absolute instant*, internally in UTC, and renders it in whatever timezone the viewer asks
for. `17:12+00` and `01:12` Malaysia time are the **same instant** — the data was never
wrong, only displayed in UTC. Storing local time instead would actively break the log:

- **Ordering corrupts twice a year.** A bare `01:30` with no zone repeats on the
  daylight-saving fall-back hour. An audit log you can't reliably order is not an audit log.
- **"Local" stops meaning anything the moment there's a second server**, or a reader in
  another country. UTC has exactly one meaning everywhere; that's the whole point of it.

So the rule, which is the universal standard for logs: **store the absolute instant (UTC),
convert to local only for display.** The storage stays `timestamptz`; the *display* is what
we fix — and the browsing view below is where we do it.

### `row_count` — a column that never earned its keep

You noticed `row_count` never told you anything: some rows say 1, some say 5, and the number
never once changed a decision. That's a correct read. Worse, it actively misled you in Step
17 — the `array_agg` disaster showed `row_count = 1`, because the 2,928 bloated values were
all inside a *single* row. A signal that reads "fine" during your worst result is not a
signal worth keeping. It has exactly one narrow use — spotting a query that matched *nothing*
— which isn't worth a permanent column here. We drop it. Keeping a column you never act on
is how schemas rot.

### `model` — the column Step 17 proved you needed

Here's the change with the clearest evidence behind it. When you had one Ollama run and one
Gemini run of the same question sitting next to each other in the log, you could only tell
them apart by *reading the SQL and inferring which model wrote it.* That's exactly the
question an audit log should answer directly. Recording which model produced each answer is
standard practice in any system that runs more than one — and you now run two. A `model`
column turns "squint at the SQL and guess" into a value you can filter and group by. (In a
larger setup this grows into a little cluster — `model`, `temperature`, a prompt version,
latency, token counts for cost — but `model` is the one carrying its weight today.)

### `details` — a JSONB bag, and the discipline of *not* storing everything

Your fourth instinct was the subtlest and the most correct: not every future capability will
produce a `generated_sql`. A summarizer, a classifier, a chat turn — each returns a
differently-shaped response. Hard-coding a typed column per capability doesn't scale. The
mature answer is a **`jsonb` column** — call it `details` — that holds whatever
capability-specific structure a given request produced, while the handful of things you
*filter and sort on* (`question`, `model`, `asked_at`, `error`) stay as typed columns. JSONB
future-proofs the log without giving up queryability; Postgres can index and query *into* it.

But storing a flexible bag makes it tempting to dump *everything* in, and that's where audit
design meets governance. Two disciplines to hold:

- **Store the shape, not the payload.** We record `{"kind": "sql", "columns": [...]}` — the
  *kind* of response and the *columns* it returned — but **not the result rows themselves.**
  Rows can be huge (Step 17's 2,928-element array) and can carry sensitive data; an audit
  table that quietly accumulates every value your users ever pulled becomes a liability, not
  an asset. The `kind` discriminator is the future-proofing hinge: tomorrow's summarizer
  writes `{"kind": "summary", ...}` into the same column, and old queries still work.
- **Log enough to reconstruct, and no more.** That's the actual industry principle behind
  "how much should an audit trail store" — not "log everything," not "log minimally," but
  *log what you'd need to investigate later, minus what you can't justify retaining.* For
  this project, `question + model + generated_sql + details + error` clears that bar.

We keep `generated_sql` as its own typed column, not folded into `details`, because for
*this* capability it's the single artifact you most want to read and grep — it's earned a
typed home. `details` is for the variable rest.

### The migration — evolve in place, don't rebuild

The audit table already holds real history (including those `429` rows from Step 17 that
document a genuine rate-limit event). You don't want to lose that, so this is an `ALTER`, not
a drop-and-recreate:

```fish
make db-create name=evolve_query_audit
```

```sql
-- +goose Up
ALTER TABLE query_audit ADD COLUMN model text;
ALTER TABLE query_audit ADD COLUMN details jsonb;
ALTER TABLE query_audit DROP COLUMN row_count;

CREATE VIEW v_query_audit AS
SELECT asked_at AT TIME ZONE 'Asia/Kuala_Lumpur' AS asked_local,
       model,
       question,
       left(generated_sql, 80) AS sql_preview,
       error IS NOT NULL        AS failed
FROM query_audit;

-- +goose Down
DROP VIEW v_query_audit;
ALTER TABLE query_audit ADD COLUMN row_count integer;
ALTER TABLE query_audit DROP COLUMN details;
ALTER TABLE query_audit DROP COLUMN model;
```

The new columns are nullable, so every existing row simply gets `NULL` for `model` and
`details` — the historical rows predate those facts, and a blank is the honest record of "we
didn't capture this at the time." No backfill, no fabricated data.

**What the view is, in one sentence:** a *saved query you can read like a table* — the exact
same mechanism as Phase 4's `v_sales_margin`, pointed at the audit log. It stores no data of
its own; it re-runs its `SELECT` each time. So `SELECT * FROM v_query_audit ORDER BY
asked_local DESC LIMIT 10;` is now your compact, local-time, one-line-per-query browsing
view, while the full-fidelity data stays in `query_audit` for the rare moment you need a
complete error or the whole SQL. That's the same storage-vs-presentation split as the
timestamp fix: keep everything raw underneath, read through a tidy window on top.

(One honest tradeoff: the view hard-codes `'Asia/Kuala_Lumpur'`, which bakes a location into
it. That's fine for a personal browsing convenience. The more portable alternative is to
leave the view in UTC and put `SET timezone = 'Asia/Kuala_Lumpur';` in your `~/.psqlrc` so
*every* session displays local time. Either is defensible; this picks the one that makes the
view self-contained.)

```fish
make db-migrate
make db-status
```

### Wiring it into the script

`write_audit` loses `row_count` and gains `model` and `details`. The one non-obvious detail
is the JSONB: **psycopg won't turn a plain `dict` into `jsonb` on its own** — you wrap it in
`Jsonb(...)` so the driver knows the intent (and does it safely, as a parameter, never as
string-built SQL):

```python
from psycopg.types.json import Jsonb


def write_audit(
    question: str,
    model: str,
    generated_sql: str | None,
    details: dict[str, Any] | None,
    error: str | None,
) -> None:
    with psycopg.connect(os.environ["DATABASE_URL_AUDIT_PLAIN"]) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO query_audit (question, model, generated_sql, details, error) "
            "VALUES (%s, %s, %s, %s, %s)",
            (question, model, generated_sql,
             Jsonb(details) if details is not None else None, error),
        )
```

The model to record comes from whichever backend is active, computed once near the top:

```python
ACTIVE_MODEL = OLLAMA_MODEL if LLM_BACKEND == "ollama" else MODEL_NAME
```

And the two call sites in `main()` — success records the shape in `details`, failure records
the model it tried and no details:

```python
    except Exception as e:
        write_audit(question, ACTIVE_MODEL, None, None, str(e))
        raise

    write_audit(question, ACTIVE_MODEL, query, {"kind": "sql", "columns": colnames}, None)
```

Note the ordering is *unchanged* from Step 16: the audit write still happens **before**
`narrate`, so a flaky narrator can't stop the log from recording what the pipeline really
did. That's also why `details` stores the columns and not the narration sentence — the
narration doesn't exist yet at audit time, and deliberately so.

### Verify

Run one bounded question (a single-row scalar — don't re-run the collection query that
flooded the terminal in Step 17), then read it back through the view:

```fish
env LLM_BACKEND=ollama uv run python src/ask_question.py "what's our total margin?"
```
```fish
make psql
```
```sql
SELECT * FROM v_query_audit ORDER BY asked_local DESC LIMIT 5;   -- compact, local time, model shown
SELECT model, details FROM query_audit ORDER BY asked_at DESC LIMIT 1;  -- the JSONB bag
```

The newest row should show `model = qwen2.5-coder:7b`, an `asked_local` that matches your
wall clock, and `details = {"kind": "sql", "columns": ["total_margin"]}`.

### What I changed and ran for this step (you asked me to)

As with Step 17, you asked me to implement this one rather than hand it to you:

- New migration `migrations/20260711134832_evolve_query_audit.sql`: adds `model` and
  `details`, drops `row_count`, creates `v_query_audit`. Applied it with `make db-migrate`.
- `src/ask_question.py`: added the `Jsonb` import and `ACTIVE_MODEL`; changed `write_audit`'s
  signature and INSERT; updated both call sites in `main()`.
- Verified end to end: ran the scalar question on the local backend and confirmed the new row
  carries the model, local-time display, and the `details` JSONB.

The migration is reversible (`make db-rollback` runs the `Down`), which restores `row_count`
and drops the two new columns and the view — though the dropped `row_count` history is gone
for good, which is the point.

---

### On Steps 15–16, what I did and didn't do

I wrote these two steps for you to work through and type yourself, same as the rest of the
phase — I did **not** create `src/eval_pipeline.py`, run the migration, add the `auditor`
role, add the `.env` line, or execute anything against your database. Those are all writes,
credentials, or new files, which are yours to do.

The two `expected` values in the starter eval set (`6031937.59` and `2`) are the ones
already verified earlier in this walkthrough. Any case you add, verify its answer in
`make psql` first — never record a number you haven't seen the database return. And after
you run the `add_query_audit` migration, the `auditor` verification step (insert succeeds,
select denied) is the thing to actually watch happen, rather than take on faith.

Tell me when you want the PROGRESS.md box ticked.

---

### What I verified while writing this (read-only, nothing in your DB changed)

- Confirmed (from earlier phases, unchanged since) that all three views exist and their
  previously-verified totals still hold: `v_sales_margin` margin **6,031,937.59**,
  `v_delivery_performance` South breach rate **12.4%**.
- Ran the full breach-rate ranking read-only to get the numbers used in Step 9:
  South **12.4%**, Central **8.2%**, North **4.0%**. These are the figures your narration
  should reproduce once Fix 1 removes the `LIMIT 1` — if they don't match, the SQL
  changed, not the prose.
- Confirmed against Google's current docs that `gemini-2.5-flash-lite` is on the free
  tier, that `google-genai` is the current SDK, and that passing a Pydantic class as
  `response_schema` yields a validated instance on `response.parsed`.
- Read the installed SDK's source (`google/genai/_api_client.py`, v2.10.0) to establish
  the Step 11 claim rather than infer it: `retry_args()` returns
  `stop_after_attempt(1)` when `retry_options` is `None`, and `_RETRY_HTTP_STATUS_CODES`
  already contains `503`. Also confirmed `HttpOptions.retry_options` is the field that
  switches it on. If you upgrade `google-genai`, re-check that grep before trusting
  Step 11 — this is a private module, and its defaults are not an API contract.
- Queried the real `category` domain for Step 13, read-only: **Berries, Citrus, Pome,
  Tropical** — no `Orange`, which is why that filter matched nothing. Confirmed
  `Navel Orange` and `Valencia Orange` are both filed under `Citrus`, and that both
  `product_name ILIKE '%orange%'` and `category = 'Citrus'` return **2**. Also confirmed
  the per-category product counts (Berries 1, Citrus 2, Pome 2, Tropical 3) — useful for
  sanity-checking `load_enums` once you wire it up.
- I have not created the `llm_reader` role, added the `.env` lines, requested an API key,
  or written `src/ask_question.py` — every one of those is a write, a credential, or an
  external API interaction, and per this project's rule, those are yours to do.

**On you to run:** getting an API key from AI Studio, the migration, Step 6's
sanity-check, Step 7's verification, Step 9's two fixes, and Step 11's two fixes. If the
printed SQL looks wrong before you even look at the result (e.g. it references a raw
table instead of a view), that's worth catching by eye — a good sign the schema
description in Step 3 needs a clearer comment, not that something in the Python is
broken.

A note on sequencing Step 9: do Fix 1 alone first and re-run both questions. Seeing how
much better the *raw tuples* get, with no narrator in play, is the whole point of that
step — if you add both fixes at once you'll credit the prose for work the SQL did.
