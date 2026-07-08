-- +goose Up
CREATE OR REPLACE VIEW v_sales_margin AS
SELECT
    ol.order_line_id,
    o.order_id,
    o.order_date,
    d.year,
    d.quarter,
    d.month,
    d.month_name,
    c.channel,
    c.region,
    c.city,
    p.category,
    p.product_name,
    p.brand,
    ol.qty_cartons,
    ol.unit_price,
    ol.unit_cost,
    ol.unit_price                    * ol.qty_cartons AS revenue,
    ol.unit_cost                     * ol.qty_cartons AS cost,
    (ol.unit_price - ol.unit_cost)   * ol.qty_cartons AS margin
FROM order_lines ol
JOIN orders    o ON o.order_id    = ol.order_id
JOIN customers c ON c.customer_id = o.customer_id
JOIN products  p ON p.product_id  = ol.product_id
JOIN dates     d ON d.date        = o.order_date;

CREATE OR REPLACE VIEW v_delivery_performance AS
SELECT
    dl.delivery_id,
    o.order_id,
    o.order_date,
    c.region,
    c.channel,
    dl.route,
    dl.dispatched_at,
    dl.planned_eta,
    dl.delivered_at,
    round(extract(epoch FROM (dl.delivered_at - dl.planned_eta)) / 3600.0, 2) AS delay_hours,
    (dl.delivered_at <= dl.planned_eta + interval '2 hours')                  AS on_time,
    dl.temp_excursion
FROM deliveries dl
JOIN orders    o ON o.order_id    = dl.order_id
JOIN customers c ON c.customer_id = o.customer_id;

CREATE OR REPLACE VIEW v_storage_cost AS
SELECT
    sc.storage_cost_id,
    sc.cost_date,
    d.year,
    d.month,
    d.month_name,
    p.product_id,
    p.product_name,
    p.category,
    sc.pallets_stored,
    sc.cost_per_pallet_day,
    sc.pallets_stored * sc.cost_per_pallet_day AS daily_cost
FROM storage_costs sc
JOIN products p ON p.product_id = sc.product_id
JOIN dates    d ON d.date       = sc.cost_date;

-- +goose Down
DROP VIEW IF EXISTS v_storage_cost;
DROP VIEW IF EXISTS v_delivery_performance;
DROP VIEW IF EXISTS v_sales_margin;
