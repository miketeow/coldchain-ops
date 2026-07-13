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
