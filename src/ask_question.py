import os
import re
import sys
from typing import LiteralString, cast

import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()

PLACEHOLDER_KEY = "placeholder-not-a-real-key"
FIXTURES_PATH = "fixtures/nl_to_sql_fixtures.json"
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
        # sql is dynamic (LLM- or fixture-sourced) text, not a compile-time literal, so
        # psycopg's LiteralString-only overload can't accept it structurally. That
        # overload guards against interpolating untrusted *values* into a query string;
        # here the whole query is untrusted by design, and it's the llm_reader role plus
        # validate_sql above that make running it safe, not this type.
        cur.execute(cast(LiteralString, sql))
        assert cur.description is not None
        colnames = [desc.name for desc in cur.description]
        rows = cur.fetchall()

    print(f"\nSQL: {sql}\n")
    print(colnames)
    for row in rows:
        print(row)

if __name__ == "__main__":
    main()
