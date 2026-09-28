from __future__ import annotations

from laptop_tracker.config import Settings
from laptop_tracker.schema import REQUIRED_RAW_COLUMNS


def settings(max_validation_errors: int = 1000) -> Settings:
    return Settings(
        spreadsheet_id=None,
        raw_tab_name="raw",
        service_account_file=None,
        service_account_json=None,
        max_validation_errors=max_validation_errors,
        skip_freshness_check=True,
    )


def raw_row(**overrides: str) -> list[str]:
    values = {
        "grass_date": "2026-09-01",
        "shopid": "100",
        "shop_name": "Example Shop",
        "is_cross_border": "0",
        "is_official_store": "1",
        "is_managed": "0",
        "itemid": "200",
        "item_name": "Example Laptop 14",
        "modelid": "300",
        "model_name": "ExampleBook 14",
        "current_price": "29999",
        "total_orders": "1",
        "gmv_usd": "500",
        "amount": "500",
    }
    values.update(overrides)
    return [values[column] for column in REQUIRED_RAW_COLUMNS]


def raw_values(*rows: list[str]) -> list[list[str]]:
    return [REQUIRED_RAW_COLUMNS, *rows]
