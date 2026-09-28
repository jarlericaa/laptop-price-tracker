"""Dynamic source-period detection and source freshness checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

MANILA_TIMEZONE = "Asia/Manila"


class StaleSourceError(RuntimeError):
    """Raised when the latest raw-data month is not the current Manila month."""


@dataclass(frozen=True)
class PeriodSelection:
    m_minus_1_month: str
    mtd_month: str
    minimum_period: str
    maximum_period: str


def detect_periods(frame: pd.DataFrame) -> PeriodSelection:
    periods = frame["period_month"].dropna().astype(str)
    if periods.empty:
        raise ValueError("No valid monthly periods are available after validation.")
    as_periods = pd.PeriodIndex(periods, freq="M")
    latest = as_periods.max()
    earliest = as_periods.min()
    return PeriodSelection(
        m_minus_1_month=str(latest - 1),
        mtd_month=str(latest),
        minimum_period=str(earliest),
        maximum_period=str(latest),
    )


def ensure_fresh(
    selection: PeriodSelection, enabled: bool = True, now: datetime | None = None
) -> None:
    if not enabled:
        return
    local_now = (
        now.astimezone(ZoneInfo(MANILA_TIMEZONE))
        if now
        else datetime.now(ZoneInfo(MANILA_TIMEZONE))
    )
    expected_month = local_now.strftime("%Y-%m")
    if selection.mtd_month != expected_month:
        raise StaleSourceError(
            f"Raw data is stale: latest month is {selection.mtd_month}, "
            f"expected {expected_month} in Manila."
        )


def period_summary(frame: pd.DataFrame, selection: PeriodSelection, run_id: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "minimum_period": selection.minimum_period,
                "maximum_period": selection.maximum_period,
                "m_minus_1_month": selection.m_minus_1_month,
                "mtd_month": selection.mtd_month,
                "m_minus_1_row_count": int(
                    (frame["period_month"] == selection.m_minus_1_month).sum()
                ),
                "mtd_row_count": int((frame["period_month"] == selection.mtd_month).sum()),
                "total_row_count": len(frame),
            }
        ]
    )
