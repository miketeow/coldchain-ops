-- +goose Up
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE products ADD COLUMN embedding vector(384);

-- +goose Down
ALTER TABLE products DROP COLUMN IF EXISTS embedding;
DROP EXTENSION IF EXISTS vector;
