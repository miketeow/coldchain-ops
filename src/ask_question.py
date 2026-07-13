import os
import re
import sys
from typing import Any, LiteralString, TypeVar, cast

import ollama
import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors
from pydantic import BaseModel
from psycopg import sql
from psycopg.types.json import Jsonb

load_dotenv()

MODEL_NAME = "gemini-2.5-flash-lite"

# Which LLM to talk to. "gemini" (the deployed default) or "ollama" (a local model,
# for development when the Gemini free tier runs out — see the walkthrough).
LLM_BACKEND = os.environ.get("LLM_BACKEND", "gemini")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

# The concrete model behind the active backend — recorded in the audit log so runs from
# different models are distinguishable after the fact (see the walkthrough).
ACTIVE_MODEL = OLLAMA_MODEL if LLM_BACKEND == "ollama" else MODEL_NAME

VIEW_SCHEMA = """
You may only query these three views. Never reference any other table.

v_sales_margin(order_line_id, order_id, order_date, year, quarter, month, month_name,
    channel, region, city, category, product_name, brand, qty_cartons, unit_price,
    unit_cost, revenue, cost, margin)
    -- one row per order line. revenue/cost/margin are already computed money columns, in MYR.
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

Alias every output column with a descriptive snake_case name; never emit a bare
sum/avg/count as a column name. Round money to 2 decimal places and percentages to 1,
using round(), so the result is directly readable. When a question asks for a
superlative ("worst", "best", "top"), return the full ranked set rather than only the
top row — the comparison is the answer.
For columns whose allowed values are not listed above (product_name, brand, city,
route), never filter with equality on a value you have not been shown. Use
ILIKE '%substring%' instead, so a near-miss still matches.

{VIEW_SCHEMA}
"""


class SQLAnswer(BaseModel):
    sql: str

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
- If the result is empty or a zero count, do NOT assert that none exist. Say that the
  query matched no rows, and quote the filter it used, so the reader can judge whether
  the filter was the right one.
"""

_gemini_client: genai.Client | None = None


def gemini_client() -> genai.Client:
    """Build the Gemini client on first use, not at import time — so a local
    LLM_BACKEND=ollama run never needs GEMINI_API_KEY to be set at all."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=5),
            ),
        )
    return _gemini_client


M = TypeVar("M", bound=BaseModel)

# Errors that mean "the model is unreachable/overloaded right now", as opposed to
# "the model returned something malformed" (a real bug that must still propagate).
# Gemini raises errors.APIError; Ollama raises ResponseError for API failures and the
# builtin ConnectionError when its daemon isn't running.
LLM_UNAVAILABLE = (errors.APIError, ollama.ResponseError, ConnectionError)


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
        format=schema.model_json_schema(),   # force the JSON shape, same idea as response_schema
        options={"temperature": 0, "num_ctx": 8192},
    )
    content = response.message.content
    if content is None:
        raise RuntimeError("model returned an empty response")
    return schema.model_validate_json(content)


def generate_structured(system_prompt: str, contents: str, schema: type[M]) -> M:
    """One call that returns a validated Pydantic object, whichever backend is active.
    Everything downstream is identical regardless of which model produced it."""
    if LLM_BACKEND == "ollama":
        return _ollama_structured(system_prompt, contents, schema)
    return _gemini_structured(system_prompt, contents, schema)

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

def get_sql(question: str, system_prompt: str) -> str:
    return generate_structured(system_prompt, question, SQLAnswer).sql


FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|grant|truncate)\b", re.I)


def validate_sql(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError("only a single statement is allowed")
    if not stripped.lower().startswith("select"):
        raise ValueError("only SELECT statements are allowed")
    if FORBIDDEN.search(sql):
        raise ValueError(f"query contains a disallowed keyword: {sql}")

def narrate(question: str, colnames: list[str], rows: list[tuple[Any, ...]]) -> str | None:
    table = " | ".join(colnames) + "\n"
    table += "\n".join(" | ".join(str(v) for v in row) for row in rows)

    try:
        return generate_structured(
            NARRATOR_PROMPT,
            f"Question: {question}\n\nQuery result:\n{table}",
            Narration,
        ).answer
    except LLM_UNAVAILABLE as e:
        print(f"[narration unavailable: {e}]", file=sys.stderr)
        return None

def answer_question(
    conn: psycopg.Connection, question: str
) -> tuple[str, list[str], list[tuple]]:
    system_prompt = SYSTEM_PROMPT + "\n" + load_enums(conn)
    query = get_sql(question, system_prompt)
    validate_sql(query)
    with conn.cursor() as cur:
        cur.execute(cast(LiteralString, query))
        assert cur.description is not None
        colnames = [desc.name for desc in cur.description]
        rows = cur.fetchall()
    return query, colnames, rows

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

def main():
    question = sys.argv[1]
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL_LLM_PLAIN"])
        with conn:
            query, colnames, rows = answer_question(conn, question)
    except Exception as e:
        write_audit(question, ACTIVE_MODEL, None, None, str(e))
        raise

    write_audit(question, ACTIVE_MODEL, query, {"kind": "sql", "columns": colnames}, None)
    narration = narrate(question, colnames, rows)
    print(f"\nSQL: {query}\n")
    if narration:
        print(narration)
        print()
    print(colnames)
    for row in rows:
        print(row)

if __name__ == "__main__":
    main()
