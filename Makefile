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
	psql "$(DATABASE_URL_PLAIN)"

probe:
	psql "$(DATABASE_URL_PLAIN)" -f scripts/probe.sql
