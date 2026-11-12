"""The TRACKER_GUIDE refresh that goes with a variation publication (2026-11-05).

TRACKER_GUIDE is the owner's own explanatory tab: a label in column A, its explanation in column
B (merged across), hyperlinks to archived tabs and a hidden-gridline house format. The publisher
never replaces it. It changes only the *text* of the rows whose labels it owns and appends the few
labelled rows the compact dashboard needs, copying the format and merge of an existing row:

- an existing label found in column A: only that row's column B text changes (its format, merge,
  hyperlinks and every other row stay as they are);
- a label that is not there yet: one row is appended after the last used row, formatted like a
  template row. Running the publisher again finds the label and updates it in place, so rows are
  never duplicated;
- a tab without the expected shape (no labels in column A) is skipped with a stated reason.

Every request is scoped to the guide's own sheet ID and a full snapshot is saved first (the
publisher's rollback restores it with the data tabs).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .sheet_publication import (
    DestinationError,
    assert_publisher_tab,
    guard_requests,
    snapshot_rows,
    snapshot_tab,
)

GUIDE_TAB = "TRACKER_GUIDE"
TEMPLATE_LABEL = "Data dates"  # a plain text row whose format new rows copy
CHARS_PER_PIXEL = 1 / 6.2  # Roboto 10 pt averages about 6 px a character
LINE_HEIGHT = 17
ROW_PADDING = 10


@dataclass
class GuidePlan:
    sheet_id: int | None = None
    requests: list[dict] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # labels whose text changes
    appended: list[str] = field(default_factory=list)  # labels added as new rows
    unchanged: list[str] = field(default_factory=list)
    skipped: str = ""  # why nothing is written
    expected: dict[str, str] = field(default_factory=dict)  # label -> text after the write
    snapshot: dict | None = None


def _string(cell: Mapping) -> str:
    return (cell.get("userEnteredValue") or {}).get("stringValue", "")


def _merge_width(merges: Sequence[Mapping], row: int, widths: Sequence[Mapping]) -> int:
    """Pixel width of the merged range that starts in column B of ``row`` (else column B)."""
    for merge in merges:
        if merge.get("startRowIndex") == row and merge.get("startColumnIndex") == 1:
            span = range(merge["startColumnIndex"], merge["endColumnIndex"])
            return sum((widths[c].get("pixelSize") or 100) for c in span if c < len(widths))
    return (widths[1].get("pixelSize") or 100) if len(widths) > 1 else 100


def needed_height(text: str, pixels: int) -> int:
    per_line = max(20, int(pixels * CHARS_PER_PIXEL))
    lines = sum(max(1, math.ceil(len(part) / per_line)) for part in text.split("\n"))
    return lines * LINE_HEIGHT + ROW_PADDING


def plan_guide(spreadsheet, entries: Sequence[tuple[str, str]]) -> GuidePlan:
    """Read the guide once (values, formats, merges, sizes) and plan the label-keyed updates.

    Never raises for a guide that is missing or differently shaped: the result says why it is
    skipped. ``entries`` are ``(label, text)`` pairs; a label found in column A is updated, one
    that is not is appended."""
    plan = GuidePlan()
    try:
        assert_publisher_tab(GUIDE_TAB, (GUIDE_TAB,))
        found = snapshot_tab(spreadsheet, GUIDE_TAB, (GUIDE_TAB,))
    except Exception as exc:  # access problems are reported, never raised (advisory tab)
        plan.skipped = f"could not read {GUIDE_TAB} ({type(exc).__name__}: {exc})"
        return plan
    if found is None:
        plan.skipped = f"{GUIDE_TAB} does not exist; it is never created by the publisher"
        return plan
    plan.snapshot = found
    plan.sheet_id = found["properties"]["sheetId"]
    rows = snapshot_rows(found)
    labels = {_string(row[0]): index for index, row in enumerate(rows) if row and _string(row[0])}
    if not labels:
        plan.skipped = f"{GUIDE_TAB} has no labels in column A; left as it is"
        return plan
    data = (found.get("data") or [{}])[0]
    widths = data.get("columnMetadata", [])
    heights = data.get("rowMetadata", [])
    merges = found.get("merges", [])
    sheet = plan.sheet_id
    template = labels.get(TEMPLATE_LABEL, max(labels.values()))
    next_row = max(labels.values()) + 1
    columns = len(rows[template]) if rows[template] else 2

    def set_height(row: int, text: str, current: int) -> None:
        wanted = needed_height(text, _merge_width(merges, row, widths))
        if wanted > current:
            plan.requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet,
                            "dimension": "ROWS",
                            "startIndex": row,
                            "endIndex": row + 1,
                        },
                        "properties": {"pixelSize": wanted},
                        "fields": "pixelSize",
                    }
                }
            )

    for label, body in entries:
        plan.expected[label] = body
        if label in labels:
            row = labels[label]
            current = _string(rows[row][1]) if len(rows[row]) > 1 else ""
            if current == body:
                plan.unchanged.append(label)
                continue
            plan.updated.append(label)
            plan.requests.append(
                {
                    "updateCells": {
                        "start": {"sheetId": sheet, "rowIndex": row, "columnIndex": 1},
                        "rows": [{"values": [{"userEnteredValue": {"stringValue": body}}]}],
                        "fields": "userEnteredValue",
                    }
                }
            )
            set_height(
                row, body, (heights[row].get("pixelSize") or 21) if row < len(heights) else 21
            )
            continue
        # Append below the last used row, formatted like the template row, merged the same way.
        row = next_row
        next_row += 1
        plan.appended.append(label)
        values = []
        for column in range(max(columns, 2)):
            cell = {
                "userEnteredFormat": (rows[template][column].get("userEnteredFormat") or {})
                if column < len(rows[template])
                else {}
            }
            if column == 0:
                cell["userEnteredValue"] = {"stringValue": label}
            elif column == 1:
                cell["userEnteredValue"] = {"stringValue": body}
            values.append(cell)
        if row >= found["properties"].get("gridProperties", {}).get("rowCount", 0):
            plan.requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet, "gridProperties": {"rowCount": row + 1}},
                        "fields": "gridProperties.rowCount",
                    }
                }
            )
        plan.requests.append(
            {
                "updateCells": {
                    "start": {"sheetId": sheet, "rowIndex": row, "columnIndex": 0},
                    "rows": [{"values": values}],
                    "fields": "userEnteredValue,userEnteredFormat",
                }
            }
        )
        for merge in merges:
            if merge.get("startRowIndex") == template and merge.get("endRowIndex") == template + 1:
                plan.requests.append(
                    {
                        "mergeCells": {
                            "range": dict(
                                merge, sheetId=sheet, startRowIndex=row, endRowIndex=row + 1
                            ),
                            "mergeType": "MERGE_ALL",
                        }
                    }
                )
        plan.requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet,
                        "dimension": "ROWS",
                        "startIndex": row,
                        "endIndex": row + 1,
                    },
                    "properties": {
                        "pixelSize": max(
                            needed_height(body, _merge_width(merges, template, widths)),
                            (heights[template].get("pixelSize") or 21)
                            if template < len(heights)
                            else 21,
                        )
                    },
                    "fields": "pixelSize",
                }
            }
        )
    if plan.requests:
        guard_requests(plan.requests, {sheet})
    return plan


def write_guide(spreadsheet, plan: GuidePlan) -> None:
    """Apply a guide plan (one batch). The caller saved ``plan.snapshot`` first."""
    if plan.skipped or not plan.requests:
        return
    guard_requests(plan.requests, {plan.sheet_id})
    spreadsheet.batch_update({"requests": plan.requests})


def verify_guide(spreadsheet, plan: GuidePlan) -> list[str]:
    """Read the guide back: every planned label has exactly its planned text, once."""
    found = snapshot_tab(spreadsheet, GUIDE_TAB, (GUIDE_TAB,))
    if found is None:
        raise DestinationError(f"{GUIDE_TAB} disappeared during the publication")
    issues = []
    rows = snapshot_rows(found)
    seen: dict[str, list[str]] = {}
    for row in rows:
        if row and _string(row[0]):
            seen.setdefault(_string(row[0]), []).append(_string(row[1]) if len(row) > 1 else "")
    for label, body in plan.expected.items():
        values = seen.get(label, [])
        if len(values) != 1:
            issues.append(f"{GUIDE_TAB} label {label!r} appears {len(values)} times")
        elif values[0] != body:
            issues.append(f"{GUIDE_TAB} label {label!r} text differs after the write")
    return issues


def guide_entries(
    *,
    layout_version: str,
    publication_id: str,
    week: str,
    cutoff: str,
    rows: int,
    published: str,
) -> list[tuple[str, str]]:
    """The guide's text for the compact dashboard. Existing labels are updated in place; the last
    three are appended when the guide does not have them yet."""
    return [
        (
            "STATUS",
            f"Published {published}: {rows} Top 100 Shopee variation rows for the week {week} "
            f"(report cutoff {cutoff}). Layout {layout_version}; publication {publication_id}.",
        ),
        (
            "Shopee prices",
            "Snapshot price: each row uses its own variation's latest eligible price from the "
            "Shopee sales extract at the reporting cutoff, with its source date. It is not a "
            "current or live price. STALE = older than 7 days at the cutoff. A monthly "
            "source-record "
            "date is not an exact price observation time.",
        ),
        (
            "MATCH",
            "Eligible matching configuration. The basis stays visible: MATCH · Listed specs (the "
            "specs agree, the exact product is unknown) and MATCH · Out of stock (the price is "
            "kept "
            "but excluded from in-stock minima).",
        ),
        (
            "ALT 1",
            "One established specification difference, for example ALT 1 · CPU. A close "
            "alternative, not a matching configuration. 'Limited' means the comparison used only a "
            "limited basis (model family or CPU series).",
        ),
        (
            "ALT 2",
            "Two established differences, for example ALT 2 · CPU, Storage (GPU shares the same "
            "budget of two). Unknown fields are never counted as agreements.",
        ),
        (
            "RECHECK",
            "Missing or contradictory evidence: RECHECK · Variant ambiguous, Specs incomplete, "
            "Spec conflict. EXCLUDED · Bundle terms: a matching offer excluded by purchase terms. "
            "PENDING · Not fetched and DEFERRED · Rate limited: waiting to be collected. None of "
            "these is a comparable price.",
        ),
        (
            "Current results",
            f"{rows} variation rows published for the week {week}. PRICE COMPARISON is the "
            "business view. The PILOT_PRICE_* tabs and TOP_100_SHOPEE_VARIATIONS are its hidden "
            "technical sources (PILOT_PRICE_COMPARISON, PILOT_PRICE_OVERVIEW, "
            "PILOT_PRICE_ALTERNATIVES, PILOT_PRICE_REVIEW, PILOT_PRICE_HISTORY, "
            "PILOT_PRICE_RUN_LOG). Earlier configuration results remain below.",
        ),
        (
            "Next step",
            "Each weekly run (run_variation_week.sh) rebuilds the reports and stops at a publish "
            "preview. Publishing is the separate publish_variation_prices.py --write-sheets step "
            "(docs/MAPPED_TOP_100_2026-11-03.md); --rollback restores the previous tabs.",
        ),
        (
            "Data dates",
            "Shopee snapshot dates, retailer observation times and stock status are separate. "
            "NO RESULTS and NO RELEVANT describe what the searches returned: they never mean the "
            "retailer does not sell the laptop, and 'Search limited' means the retailer's catalog "
            "was not exhaustively searched.",
        ),
        (
            "Minimum prices",
            "Visible: the lowest in-stock matching price and the lowest in-stock alternative "
            "price, "
            "always separate. Every other minimum and its basis is in the collapsed 'Other price "
            "minima and bases' column group.",
        ),
        (
            "Layout prepared",
            f"2026-10-27 variation-level structure · compact dashboard layout {layout_version} "
            "(2026-11-05)",
        ),
        (
            "Compact view",
            "Hidden technical tab PILOT_PRICE_COMPARISON (unhide it from the sheet list to "
            "troubleshoot): rank, laptop, seller, selected variation, target specs, the Shopee "
            "snapshot price and its date, a Price / Result pair per retailer, the two in-stock "
            "minima and a row coverage summary. One row per Shopee variation. Reporting week, "
            "cutoff and the colour legend are in the banner rows above the header.",
        ),
        (
            "Expanded columns",
            "Collapsed column groups to the right (click + above the column letters): expanded "
            "specifications, Shopee details, one details group per retailer (full status, "
            "differences target → retailer, unknown fields, warnings, stock, observation time, "
            "offer and candidate links, IDs), other price minima and bases, and IDs, GMV and run "
            "metadata. Each result cell's note holds the same explanation.",
        ),
        (
            "Result colours",
            "In the hidden technical tabs: green MATCH, yellow ALT 1, orange ALT 2, violet "
            "RECHECK / EXCLUDED, grey no price. Every colour also has its text label.",
        ),
        (
            "Price comparison",
            "PRICE COMPARISON: Is this Shopee variation cheaper or dearer than the same laptop "
            "configuration at other retailers? Price Gap = Shopee Price minus the lowest "
            "verified in-stock matching retailer price (the % is over that retailer price). "
            "Positive = Retailer cheaper, negative = Shopee cheaper. Out-of-stock prices "
            "(OOS) are shown but never used for the lowest price; close alternatives are never "
            "shown as prices. No match = search finished, no matching configuration verified; "
            "Review = a candidate needs checking, or the search hit a result cap so the result "
            "is uncertain; Not checked = search not completed. Price Data As Of = when the "
            "prices were taken, not when the tab was published. ⚠ Stale = the "
            "Shopee snapshot is older than 7 days (the gap is still calculated).",
        ),
    ]
