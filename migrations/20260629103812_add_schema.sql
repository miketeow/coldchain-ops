-- +goose Up
-- ============================================================
-- coldchain-ops :: business schema (star schema)
-- Dimensions describe "who / what / when". Facts record events
-- that happen to those dimensions (an order, a delivery, a cost).
-- Create dimensions FIRST: facts reference them via foreign keys.
-- ============================================================

-- ---------- DIMENSIONS ----------

-- The global growers/packers CTG imports fruit from.
CREATE TABLE suppliers (
    supplier_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    country       TEXT NOT NULL          -- South Africa, USA, Australia, NZ, Chile...
);

-- The fruit SKUs. brand sits here (a product has one brand);
-- a supplier may supply many products.
CREATE TABLE products (
    product_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_name       TEXT NOT NULL,                       -- "Gala Apple", "Navel Orange"
    category           TEXT NOT NULL,                       -- Citrus, Berries, Pome, Stone, Grapes, Melon, Tropical
    brand              TEXT,                                -- Sunkist, Zespri, Pink Lady... nullable: some are generic
    supplier_id        BIGINT NOT NULL REFERENCES suppliers(supplier_id),
    unit               TEXT NOT NULL DEFAULT 'carton',      -- selling unit
    shelf_life_days    INTEGER NOT NULL,                    -- cold-chain relevance
    default_unit_cost  NUMERIC(12,2) NOT NULL,              -- list/reference cost; real cost is per-transaction on order_lines
    default_unit_price NUMERIC(12,2) NOT NULL               -- list/reference price
);

-- Who CTG sells to: supermarkets, wholesalers, wet markets, ecommerce.
CREATE TABLE customers (
    customer_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_name TEXT NOT NULL,                            -- "Lotus's", "AEON", "Jaya Grocer"
    channel       TEXT NOT NULL,                            -- Hypermarket, Supermarket, Wholesale, Wet Market, Ecommerce
    region        TEXT NOT NULL,                            -- North, Central, South
    city          TEXT
);

-- Date dimension. One row per calendar day. Populated in Phase 2.
-- Pre-computing year/quarter/month/weekday means the BI tool can
-- slice by them WITHOUT computing anything at query time.
CREATE TABLE dates (
    date        DATE PRIMARY KEY,
    year        INTEGER NOT NULL,
    quarter     INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    month_name  TEXT    NOT NULL,                           -- "January"
    week        INTEGER NOT NULL,                           -- ISO week
    day_of_week INTEGER NOT NULL,                           -- 1=Mon ... 7=Sun
    day_name    TEXT    NOT NULL,                           -- "Monday"
    is_weekend  BOOLEAN NOT NULL
);

-- ---------- FACTS ----------

-- Order header. One row per order. The order's monetary total is
-- NOT stored here: it is derived by summing its order_lines.
-- order_date references the date dimension, so dates must be
-- seeded BEFORE orders are loaded.
CREATE TABLE orders (
    order_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id),
    order_date  DATE   NOT NULL REFERENCES dates(date),
    source      TEXT   NOT NULL DEFAULT 'system',           -- 'system' | 'accounting_import' | 'whatsapp'  (provenance)
    status      TEXT   NOT NULL DEFAULT 'fulfilled'         -- 'fulfilled' | 'cancelled'
);

-- Order detail. GRAIN = one row per product per order.
-- This is the lowest level of detail in the whole model, and it is
-- where margin lives: (unit_price - unit_cost) * qty_cartons.
CREATE TABLE order_lines (
    order_line_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      BIGINT NOT NULL REFERENCES orders(order_id),
    product_id    BIGINT NOT NULL REFERENCES products(product_id),
    qty_cartons   INTEGER       NOT NULL,
    unit_price    NUMERIC(12,2) NOT NULL,                   -- what the customer paid per carton
    unit_cost     NUMERIC(12,2) NOT NULL                    -- what it cost CTG per carton
);

-- Delivery/logistics fact. One row per order (a shipment).
-- delay  = delivered_at - planned_eta  (computed later in a view)
-- temp_excursion = did the reefer break cold chain on this run?
CREATE TABLE deliveries (
    delivery_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id       BIGINT NOT NULL REFERENCES orders(order_id),
    route          TEXT   NOT NULL,                         -- "North: Penang-Kedah", "Central: KL-Klang"...
    dispatched_at  TIMESTAMPTZ,
    planned_eta    TIMESTAMPTZ,
    delivered_at   TIMESTAMPTZ,                             -- nullable until delivered
    temp_excursion BOOLEAN NOT NULL DEFAULT FALSE
);

-- Cold-storage cost fact. One row per product per day in storage.
CREATE TABLE storage_costs (
    storage_cost_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cost_date           DATE   NOT NULL REFERENCES dates(date),
    product_id          BIGINT NOT NULL REFERENCES products(product_id),
    pallets_stored      NUMERIC(10,2) NOT NULL,
    cost_per_pallet_day NUMERIC(12,2) NOT NULL
);

-- ---------- INDEXES on foreign-key columns ----------
-- Postgres indexes a PRIMARY KEY automatically, but NOT the FK columns
-- on the "many" side. Joins/filters on these benefit from an index.
CREATE INDEX idx_products_supplier   ON products(supplier_id);
CREATE INDEX idx_orders_customer     ON orders(customer_id);
CREATE INDEX idx_orders_date         ON orders(order_date);
CREATE INDEX idx_order_lines_order   ON order_lines(order_id);
CREATE INDEX idx_order_lines_product ON order_lines(product_id);
CREATE INDEX idx_deliveries_order    ON deliveries(order_id);
CREATE INDEX idx_storage_date        ON storage_costs(cost_date);
CREATE INDEX idx_storage_product     ON storage_costs(product_id);

-- +goose Down
-- Drop in REVERSE dependency order: facts before the dimensions they reference.
DROP TABLE IF EXISTS storage_costs;
DROP TABLE IF EXISTS deliveries;
DROP TABLE IF EXISTS order_lines;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS dates;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS suppliers;
