import pandas as pd

from db import get_engine

engine = get_engine()

dates = pd.date_range("2024-01-01", "2025-12-31", freq="D")
dim = pd.DataFrame({"date": dates})

dim["year"] = dim["date"].dt.year
dim["quarter"] = dim["date"].dt.quarter
dim["month"] = dim["date"].dt.month
dim["month_name"] = dim["date"].dt.month_name()
dim["week"] = dim["date"].dt.isocalendar().week.astype(int)
dim["day_of_week"] = dim["date"].dt.dayofweek + 1  # 1=Mon … 7=Sun (ISO)
dim["day_name"] = dim["date"].dt.day_name()
dim["is_weekend"] = dim["date"].dt.dayofweek >= 5  # Sat=5, Sun=6

dim["date"] = dim["date"].dt.date  # strip time → pure DATE to match your PK column

dim.to_sql("dates", engine, if_exists="append", index=False)
print(f"seeded {len(dim)} dates: {dim.date.min()} → {dim.date.max()}")
