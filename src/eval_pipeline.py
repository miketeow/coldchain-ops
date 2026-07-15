import os
import time

import psycopg
from dotenv import load_dotenv

from ask_question import answer_question

load_dotenv()

CASES = [
    {"question": "what's our total margin?", "expected": 6031937.59},
    {"question": "how many different kinds of orange products we have?", "expected": 2},
    {"question": "what's our total revenue from Wholesale orders?", "expected": 2055518.64},
    {"question": "what's our total margin in the South region?", "expected": 276961.75},
    {"question": "what percentage of our deliveries arrive on time?", "expected": 66.1},
    {"question": "how many deliveries had a temperature excursion?", "expected": 289},
    {"question": "what's the average delivery delay in hours for the Central region?", "expected": 1.79},
    {"question": "what's our total storage cost to date?", "expected": 298337.40},
    {"question": "how many distinct products have we sold through the Ecommerce channel?", "expected": 8},
    {"question": "how many total cartons have we sold?", "expected": 388305},
]

RUNS_PER_CASE = 3      # each question is asked this many times, because the model varies
TOLERANCE = 0.01
# Seconds to wait between requests — see "Mind the rate limit" below. A local model
# has no per-minute cap, so there's nothing to space out; only the free Gemini tier needs it.
REQUEST_PAUSE = 0 if os.environ.get("LLM_BACKEND") == "ollama" else 4


def matches(got, expected) -> bool:
    return abs(float(got) - float(expected)) <= TOLERANCE


def main():
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
