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
- [ ] **Phase 4 — SQL analytics views** (`v_sales_margin`, `v_delivery_performance`,
      `v_storage_cost`) + CSV export for the Tableau Public path. ← NEXT
- [ ] **Phase 5 — Tableau dashboards** (Mac-native; Tableau↔Power BI mapping is an
      interview asset).
- [ ] **Phase 6 — pgvector / LLM intelligence layer** (6a semantic search OR 6b
      WhatsApp→structured-order, do one well).

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
