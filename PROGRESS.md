# PROGRESS.md — coldchain-ops phase tracker

Single source of truth for where the project is. Claude Code: read this first, and when
I tell you a phase is done, update the box here (only when I say so).

## Phase status

- [x] **Phase 0 — Skeleton + Postgres up.** Docker (`pgvector/pg17`) on port 5433,
      `uv` project, deps installed, `.env` with both connection strings.
- [x] **Phase 1 — Star schema.** 8 tables (4 dims, 4 facts), goose migration applied,
      FKs verified, margin probe returned RM340.00, load-order rejection confirmed.
- [x] **Phase 2 — Synthetic data generator.** VERIFIED:
      - Dimensions in Postgres: suppliers (6), products (8), customers (7), dates (731).
      - Column names confirmed to match the schema exactly.
      - All 8 FK constraints confirmed wired.
      - `data/accounting_export.xlsx` generated: 12,491 line rows across 5,000 in-memory
        `order_id`s (1–4 lines/order). Real header at row index 2 (needs `skiprows=2`).
      - Defects confirmed present: 3 mixed date formats; `RM`-prefixed vs plain money
        strings; 4 casing/spacing/reorder variants per product name; ~3% (374) null
        `qty_cartons`. `customer_id` deliberately clean (1–7) as the one trustworthy
        join key.
      - `make seed` rebuilds deterministically from empty (TRUNCATE … RESTART IDENTITY
        CASCADE first).
- [x] **Phase 3 — ETL: clean the messy xlsx into Postgres.** VERIFIED:
      - `src/etl_orders.py`: read (`skiprows=2`) → per-format date parse → product
        canonicalization → money clean → quarantine → header/detail split → load via
        staging + `OVERRIDING SYSTEM VALUE` (export `order_id` 1–5000 preserved as the
        real key; sequence resynced to 5001).
      - `src/generate_logistics.py`: `deliveries` (one per surviving order) +
        `storage_costs` generated against the real IDs.
      - Counts: 12,491 rows in → 374 quarantined (all `null_qty`) → 12,117 clean →
        4,964 orders / 12,117 lines; deliveries 4,964 (== orders); storage 4,065 rows.
      - Step 7 all pass: 0 orphan lines, 0 empty orders, 0 missing dates, 0 null money,
        min line margin +30.10 (no below-cost lines); margin-by-channel×category sane.
      - `make etl` idempotent (`reset-facts` → `etl_orders` → `generate_logistics`);
        logging to `logs/etl.log` with `LOG_LEVEL` control; rejects to
        `data/rejects_orders.csv`.
- [x] **Phase 4 — SQL analytics views.** VERIFIED:
      - Goose migration `20260708100538_add_analytics_views.sql` creates
        `v_sales_margin`, `v_delivery_performance`, `v_storage_cost` (plain views, all
        joins inner, safe per Phase 3's 0-orphan/0-missing-date checks).
      - `v_sales_margin`: 12,117 rows, revenue 19,832,819.59, margin 6,031,937.59 —
        matches raw `order_lines` totals exactly.
      - `v_delivery_performance`: 4,964 rows; on-time defined as delivered within a
        2-hour grace window of ETA (naive `delivered_at <= planned_eta` gives a false
        0.0%); avg delay 1.81h, on-time 66.1%, breach rate 5.8% overall (North 4.0 /
        Central 8.2 / South 12.4).
      - `v_storage_cost`: 4,065 rows, total `daily_cost` 298,337.40 — matches raw
        computation.
- [x] **Phase 5 — Semantic product search (pgvector).** VERIFIED:
      - Goose migration enables the `vector` extension and adds `products.embedding
        vector(384)`.
      - `src/generate_product_embeddings.py`: embeds `product_name`/`category`/`brand`
        per product via `sentence-transformers` (`all-MiniLM-L6-v2`), writes back through
        raw `psycopg` + `pgvector.psycopg.register_vector` (8/8 products embedded).
      - `src/search_products.py`: embeds a free-text query with the same model, ranks
        products by cosine distance (`<=>`). Query `"citrus fruit"` ranks Navel Orange
        (0.2471) and Valencia Orange (0.2739) top — despite "citrus" never appearing
        literally in the data. Query `"red grapes"` ranks Red Grapes first (0.2837),
        clearly separated from the next-closest (0.52) — confirms both semantic recall
        (no lexical overlap needed) and exact-phrase precision.
- [x] **Phase 6 — Ask a business question in English, get it answered by SQL.** VERIFIED:
      - `src/ask_question.py`: LLM turns a free-text question into ONE validated
        read-only `SELECT` against the three Phase 4 views only, run as a dedicated
        `llm_reader` role (`SELECT` on the views alone — the safety mechanism is the
        role's privileges, not prompt-level trust) — then a second, narration-only LLM
        call turns the raw rows into a two-sentence answer that may only quote figures
        actually present in the rows.
      - Guards added as real failures surfaced, not pre-emptively: structured-output
        schemas (`SQLAnswer`/`Narration` Pydantic models) instead of parsing prose;
        `validate_sql` blocks multi-statement and non-`SELECT`/mutating SQL; retry +
        graceful degradation (`LLM_UNAVAILABLE`) for a flaky upstream so a narration
        failure still prints the raw rows instead of crashing; `load_enums` reads real
        `category`/`channel`/`region` values from the DB into the prompt so the model
        can't guess a filter value that doesn't exist (e.g. `'Orange'` vs the real
        `Citrus`); the narrator is shown the executed SQL so it can honestly say "no
        rows matched this filter" instead of asserting none exist.
      - `query_audit` table (append-only; `auditor` role has `INSERT` only, no
        `SELECT`/`UPDATE`/`DELETE`) logs every question, the model that answered it,
        the generated SQL, a `details` jsonb bag, and any error — evolved from an
        initial `row_count` column (dropped, never used) to `model` + `details` once
        Step 17 made "which backend answered this" worth knowing. `v_query_audit` is a
        local-time browsing view over it.
      - `LLM_BACKEND=ollama` (`qwen2.5-coder:7b` via local Ollama) is a swappable
        second engine behind the same `generate_structured` seam, added so development
        doesn't stall on the Gemini free tier's rate limit — Gemini
        (`gemini-2.5-flash-lite`) stays the default/deployed engine.
      - `src/eval_pipeline.py`: a fixed set of question → known-answer cases, each run
        `RUNS_PER_CASE` times (the model isn't deterministic even at `temperature=0`
        across backends) with a pause between calls to respect the free-tier rate
        limit. Caught a real gap: Gemini was still sampling (temperature unset) and
        "percentage" was underspecified in the prompt (fraction vs ×100) — fixed by
        pinning `temperature=0` on both backends and adding an explicit percentage
        rule; full 10-case suite now passing.

## Key carried-forward facts for Phase 3
- The xlsx's `order_id` is an **in-memory link only**, NOT the database's IDENTITY id.
  Phase 3 must insert order headers, read back the real generated `order_id`s, then remap
  the line rows onto them. This is the subtlety to get right.
- `orders` needs a `source` value — use `'accounting_import'` for these rows (proves the
  ETL landed them; distinguishes from `'system'`/`'whatsapp'` later).
- `deliveries` and `storage_costs` are still empty by design; they get generated in
  Phase 3 once real order IDs exist.
- The missing-value rule for null `qty_cartons` is a *decision to make and document*
  (drop / impute / flag), not just code — an interviewer probes the decision.

## Working agreement (see CLAUDE.md for the full version)
Walkthroughs, not autopilot. Explain → I type it → I run it. You may verify state with
read-only queries when I ask. Ask before mutating anything.
