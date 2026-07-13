-- +goose Up
-- Evolve the audit trail: record which model answered, add a flexible JSONB bag for
-- capability-specific extras, and drop row_count (a weak signal we never acted on).
ALTER TABLE query_audit ADD COLUMN model text;
ALTER TABLE query_audit ADD COLUMN details jsonb;
ALTER TABLE query_audit DROP COLUMN row_count;

-- A browsing view: a saved SELECT (same idea as the Phase 4 views) that renders the log
-- compactly and in local time, so day-to-day reads don't need the long truncating query.
-- The full-fidelity data still lives in query_audit; this is just a tidy window onto it.
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
