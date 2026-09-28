from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from laptop_tracker.periods import StaleSourceError, detect_periods, ensure_fresh


def test_periods_are_detected_dynamically() -> None:
    selection = detect_periods(pd.DataFrame({"period_month": ["2026-07", "2026-08", "2026-09"]}))
    assert selection.mtd_month == "2026-09"
    assert selection.m_minus_1_month == "2026-08"


def test_historical_freshness_check_can_be_disabled() -> None:
    selection = detect_periods(pd.DataFrame({"period_month": ["2020-01"]}))
    ensure_fresh(selection, enabled=False)
    with pytest.raises(StaleSourceError):
        ensure_fresh(
            selection, enabled=True, now=datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Manila"))
        )
