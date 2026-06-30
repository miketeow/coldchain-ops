import pandas as pd

from db import get_engine

engine = get_engine()

# supplier

suppliers = pd.DataFrame(
    [
        {"supplier_name": "Zespri International", "country": "New Zealand"},
        {"supplier_name": "Sunkist Growers", "country": "USA"},
        {"supplier_name": "Capespan", "country": "South Africa"},
        {"supplier_name": "Rockit Global", "country": "New Zealand"},
        {"supplier_name": "Montague", "country": "Australia"},
        {"supplier_name": "Dole Food Company", "country": "Philippines"},
    ]
)

suppliers.to_sql("suppliers", engine, if_exists="append", index=False)

# read back the ids that postgres assigned
sup = pd.read_sql("SELECT supplier_id, supplier_name FROM suppliers", engine)
sid = dict(zip(sup.supplier_name, sup.supplier_id))

# --- products: each references a supplier by the id we just read -------------
products = pd.DataFrame(
    [
        {
            "product_name": "Gala Apple",
            "category": "Pome",
            "brand": "Rockit",
            "supplier": "Rockit Global",
            "shelf_life_days": 40,
            "default_unit_cost": 38.00,
            "default_unit_price": 55.00,
        },
        {
            "product_name": "Pink Lady Apple",
            "category": "Pome",
            "brand": "Pink Lady",
            "supplier": "Montague",
            "shelf_life_days": 45,
            "default_unit_cost": 42.00,
            "default_unit_price": 60.00,
        },
        {
            "product_name": "Navel Orange",
            "category": "Citrus",
            "brand": "Sunkist",
            "supplier": "Sunkist Growers",
            "shelf_life_days": 30,
            "default_unit_cost": 30.00,
            "default_unit_price": 44.00,
        },
        {
            "product_name": "SunGold Kiwi",
            "category": "Tropical",
            "brand": "Zespri",
            "supplier": "Zespri International",
            "shelf_life_days": 28,
            "default_unit_cost": 45.00,
            "default_unit_price": 62.00,
        },
        {
            "product_name": "Green Kiwi",
            "category": "Tropical",
            "brand": "Zespri",
            "supplier": "Zespri International",
            "shelf_life_days": 35,
            "default_unit_cost": 33.00,
            "default_unit_price": 48.00,
        },
        {
            "product_name": "Red Grapes",
            "category": "Berries",
            "brand": None,
            "supplier": "Capespan",
            "shelf_life_days": 21,
            "default_unit_cost": 50.00,
            "default_unit_price": 70.00,
        },
        {
            "product_name": "Valencia Orange",
            "category": "Citrus",
            "brand": "Sunkist",
            "supplier": "Sunkist Growers",
            "shelf_life_days": 28,
            "default_unit_cost": 28.00,
            "default_unit_price": 41.00,
        },
        {
            "product_name": "Cavendish Banana",
            "category": "Tropical",
            "brand": "Dole",
            "supplier": "Dole Food Company",
            "shelf_life_days": 14,
            "default_unit_cost": 18.00,
            "default_unit_price": 28.00,
        },
    ]
)
products["supplier_id"] = products["supplier"].replace(sid)  # name → real FK
products = products.drop(columns=["supplier"])  # drop the helper col
products.to_sql("products", engine, if_exists="append", index=False)

# --- customers --------------------------------------------------------------
customers = pd.DataFrame(
    [
        {
            "customer_name": "Lotus's Penang",
            "channel": "Hypermarket",
            "region": "North",
            "city": "George Town",
        },
        {
            "customer_name": "AEON Bukit Mertajam",
            "channel": "Hypermarket",
            "region": "North",
            "city": "Bukit Mertajam",
        },
        {
            "customer_name": "Giant Kuala Lumpur",
            "channel": "Supermarket",
            "region": "Central",
            "city": "Kuala Lumpur",
        },
        {
            "customer_name": "Jaya Grocer KLCC",
            "channel": "Supermarket",
            "region": "Central",
            "city": "Kuala Lumpur",
        },
        {
            "customer_name": "Pasar Borong Selera",
            "channel": "Wholesale",
            "region": "North",
            "city": "Penang",
        },
        {
            "customer_name": "TikTok Shop Orders",
            "channel": "Ecommerce",
            "region": "Central",
            "city": None,
        },
        {
            "customer_name": "Cold Storage JB",
            "channel": "Supermarket",
            "region": "South",
            "city": "Johor Bahru",
        },
    ]
)
customers.to_sql("customers", engine, if_exists="append", index=False)

print(
    f"seeded {len(suppliers)} suppliers, {len(products)} products, {len(customers)} customers"
)
