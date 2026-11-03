import math

from laptop_tracker.sheet_publication import FILLS, STATUS_FILLS, _rgb


def _levels(colour: dict) -> list[int]:
    """The 8-bit levels Sheets stores: it truncates the float it receives."""
    return [math.floor(colour[name] * 255) for name in ("red", "green", "blue")]


def test_rgb_is_exact_eight_bit_value():
    assert _rgb("#D5EFDB") == {"red": 213 / 255, "green": 239 / 255, "blue": 219 / 255}


def test_status_and_column_fills_survive_eight_bit_truncation():
    expected = {
        "match": "#D5EFDB",
        "alt1": "#FFF2A8",
        "alt2": "#FFCF99",
        "recheck": "#DADDE3",
    }
    for name, code in expected.items():
        assert _levels(STATUS_FILLS[name]) == [int(code[i : i + 2], 16) for i in (1, 3, 5)]
    for name, code in {"shopee": "#FFF2DF", "retailer": "#E8EEF8", "in_stock": "#E8F3ED"}.items():
        assert _levels(FILLS[name]) == [int(code[i : i + 2], 16) for i in (1, 3, 5)]
