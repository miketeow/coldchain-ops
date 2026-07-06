# Makefile at project root
include .env          # pulls in DATABASE_URL_PLAIN etc. as make variables
export                # re-exports all make variables as shell env vars for the recipe

.PHONY: db-up db-down db-migrate db-rollback db-status db-create

db-up:
	docker compose up -d

db-down:
	docker compose down

db-migrate:
	goose -dir migrations postgres "$(DATABASE_URL_PLAIN)" up

db-rollback:
	goose -dir migrations postgres "$(DATABASE_URL_PLAIN)" down

db-status:
	goose -dir migrations postgres "$(DATABASE_URL_PLAIN)" status

# Usage: make db-create name=add_users
db-create:
	goose -dir migrations create $(name) sql

psql:
	psql "$(DATABASE_URL_PLAIN)" $(ARGS)

probe:
	psql "$(DATABASE_URL_PLAIN)" -f scripts/probe.sql

seed:
	psql "$(DATABASE_URL_PLAIN)" -c "TRUNCATE order_lines, orders, deliveries, storage_costs, dates, customers, products, suppliers RESTART IDENTITY CASCADE;"
	uv run python src/seed_dimensions.py
	uv run python src/generate_date_dim.py
	uv run python src/generate_messy_orders.py

reset-facts:
	psql "$(DATABASE_URL_PLAIN)" -c "TRUNCATE order_lines, orders, deliveries, storage_costs RESTART IDENTITY CASCADE;"

etl: reset-facts
	uv run python src/etl_orders.py
	uv run python src/generate_logistics.py
