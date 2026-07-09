-- +goose Up
CREATE ROLE llm_reader LOGIN PASSWORD 'llm_reader_pw';
GRANT USAGE ON SCHEMA public TO llm_reader;
GRANT SELECT ON v_sales_margin, v_delivery_performance, v_storage_cost TO llm_reader;

-- +goose Down
REVOKE SELECT ON v_sales_margin, v_delivery_performance, v_storage_cost FROM llm_reader;
REVOKE USAGE ON SCHEMA public FROM llm_reader;
DROP ROLE llm_reader;
