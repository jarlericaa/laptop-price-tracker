#!/usr/bin/env python3
"""Manual publication of reviewed Phase 10 pilot price reports to dedicated Sheets tabs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.sheet_publication import (
    DATA_TABS,
    PUBLISHER_TABS,
    RUN_LOG_HEADER,
    RUN_LOG_TAB,
    SUMMARY_TAB,
    DestinationError,
    PublicationInputError,
    build_plan,
    check_ownership,
    load_pilot_report,
    publish,
    read_tab,
    write_plan_csv,
    write_plan_notes_csv,
)

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "output"
HTTP_TIMEOUT_SECONDS = (15.0, 90.0)  # (connect, read/send)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Publish reviewed Phase 10 pilot price reports (from track_prices.py) to five "
            "dedicated tabs: PILOT_PRICE_COMPARISON, PILOT_PRICE_SUMMARY (a minimal price grid "
            "of the comparison's own prices), PILOT_PRICE_HISTORY, PILOT_PRICE_REVIEW and "
            "PILOT_PRICE_RUN_LOG. --preview validates and inspects the destination read-only; "
            "--write-sheets backs up existing publisher tabs locally, replaces their content, "
            "and verifies every cell by readback. Never fetches retailer pages, reruns matching, "
            "imports observations, reads raw, or writes any other tab."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Validate and plan; no writes.")
    mode.add_argument(
        "--write-sheets", action="store_true", help="Publish to the five pilot tabs."
    )
    parser.add_argument(
        "--report-dir", type=Path, required=True, help="Reviewed track_prices.py report folder."
    )
    parser.add_argument(
        "--pilot-scope", type=Path, required=True, help="Authorized pilot configuration CSV."
    )
    parser.add_argument(
        "--collection-dir",
        type=Path,
        help="collect_and_match.py run folder (default: <report-dir>/../collection, if present).",
    )
    parser.add_argument(
        "--selected-list",
        type=Path,
        help="Verified selected-list CSV (default: the input recorded in the run summary).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT / "sheet_publications",
        help="Parent of the local publication report folder.",
    )
    return parser.parse_args(argv)


def open_configured_spreadsheet(read_only: bool):
    from laptop_tracker.config import Settings
    from laptop_tracker.sheets_source import connect, open_spreadsheet

    settings = Settings.from_env()
    client = connect(settings, read_only=read_only)
    # A stalled request fails after this many seconds instead of hanging the publication.
    client.set_timeout(HTTP_TIMEOUT_SECONDS)
    return open_spreadsheet(client, settings.spreadsheet_id or "")


def recorded_selected_list(report_dir: Path) -> Path | None:
    """The selected-list file the reports were built from, found by its recorded SHA-256."""
    try:
        summary = json.loads((report_dir / "price_tracking_run_summary.json").read_text())
        wanted = summary["selected_list"]["sha256"]
        for name, digest in summary["input_sha256"].items():
            if digest == wanted:
                path = Path(name)
                return path if path.is_absolute() else ROOT / path
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def main(
    argv: list[str] | None = None,
    *,
    spreadsheet_factory: Callable[[bool], object] | None = None,
    now: datetime | None = None,
) -> int:
    args = parse_args(argv)
    published_at = (now or datetime.now(UTC)).astimezone(UTC)
    mode = "write" if args.write_sheets else "preview"
    collection_dir = args.collection_dir
    if collection_dir is None and (args.report_dir.parent / "collection").is_dir():
        collection_dir = args.report_dir.parent / "collection"
    selected_list = args.selected_list or recorded_selected_list(args.report_dir)

    # 1. Validate every input and build the complete plan before any Sheets access.
    try:
        report = load_pilot_report(
            args.report_dir,
            args.pilot_scope,
            collection_dir=collection_dir,
            selected_list=selected_list,
        )
        if selected_list is None:
            raise PublicationInputError(["the verified selected list could not be located"])
        plan = build_plan(report)
    except (PublicationInputError, OSError, ValueError) as exc:
        issues = getattr(exc, "issues", None) or [str(exc)]
        print("Publication stopped before any Sheets access. Input issues:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1

    run_dir = args.output_root / f"{published_at.strftime('%Y%m%dT%H%M%S%fZ')}_{mode}"
    run_dir.mkdir(parents=True, exist_ok=False)
    notes: dict[str, int] = {}
    for title, tab in plan.tabs.items():
        write_plan_csv(run_dir / f"plan_{title}.csv", tab)
        if any(cell.note or cell.italic for row in tab.grid() for cell in row):
            notes[title] = write_plan_notes_csv(run_dir / f"plan_{title}_notes.csv", tab)
    week = report.summary["report_week"]
    local_report: dict = {
        "mode": mode.upper(),
        "publication_id": plan.publication_id,
        "started_at_utc": published_at.isoformat(timespec="seconds"),
        "report_week": week,
        "report_cutoff_utc": report.summary["as_of_utc"],
        "sales_snapshot": report.summary["sales_snapshot"],
        "collection": {
            k: (report.collection_summary or {}).get(k)
            for k in ("run_id", "started_at_utc", "finished_at_utc", "run_status")
        },
        "counts": report.counts,
        "planned_rows": {title: len(tab.rows) for title, tab in plan.tabs.items()}
        | {RUN_LOG_TAB: "previous publications + 1"},
        "planned_cell_notes": notes,
        "input_fingerprints": report.fingerprints,
        "published_content_sha256": plan.content_sha256,
    }

    def finish(code: int) -> int:
        local_report["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        (run_dir / "publication_report.json").write_text(
            json.dumps(local_report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"Local publication report: {run_dir}.")
        return code

    print(
        f"Reporting week {week['start']} to {week['end_exclusive']} (exclusive, "
        f"Asia/Singapore), {week['status']}; report cutoff {report.summary['as_of_utc']}."
    )
    print(f"Shopee sales snapshot: {report.summary['sales_snapshot'].get('label', '')}.")
    print(
        f"Shopee snapshot prices (historical; never in the retailer minimums): "
        f"{report.counts.get('shopee_snapshot_prices', 0)} of {len(report.comparison)} "
        f"configurations. {SUMMARY_TAB}: {len(plan.tabs[SUMMARY_TAB].rows)} rows, "
        f"{notes.get(SUMMARY_TAB, 0)} annotated cells."
    )
    counts = report.counts
    print(
        f"Pilot scope: {counts['pilot_configurations']} configurations "
        f"(of {report.summary['selected_list']['configurations']} in the comparison report)."
    )
    print(
        f"Outcomes: comparable prices {counts['comparable_prices']}; matched but excluded "
        f"{counts['matched_excluded_cells']}; retailer cells needing match review "
        f"{counts['review_required_cells']}; cells with no confirmed matched offer "
        f"{counts['not_observed_cells']}."
    )
    print(
        f"History observations requiring match review in this week: "
        f"{counts['week_matching_problems']}."
    )
    outside_scope = counts.get("collection_exact_outside_scope", 0)
    if outside_scope:
        print(
            f"Collection exact matches outside pilot scope: {outside_scope}; excluded from "
            "pilot comparison and history. Related search mismatches remain in review."
        )

    # 2. Open the configured spreadsheet: read-only scope for preview, write scope to publish.
    factory = spreadsheet_factory or open_configured_spreadsheet
    try:
        spreadsheet = factory(not args.write_sheets)
        states = {title: read_tab(spreadsheet, title) for title in PUBLISHER_TABS}
    except Exception as exc:  # Access problems are reported without credentials.
        local_report |= {"status": "ACCESS_FAILED", "error": f"{type(exc).__name__}: {exc}"}
        print(
            f"Could not open or read the configured spreadsheet ({type(exc).__name__}: {exc}). "
            "No Sheet was changed.",
            file=sys.stderr,
        )
        return finish(1)

    expected = {title: plan.tabs[title].header for title in DATA_TABS} | {
        RUN_LOG_TAB: RUN_LOG_HEADER
    }
    ownership = [p for t in PUBLISHER_TABS if (p := check_ownership(states[t], expected[t]))]
    local_report["destination_before"] = {
        title: {"exists": s.exists, "used_rows": s.used_rows} for title, s in states.items()
    }
    print(f"Destination spreadsheet: {getattr(spreadsheet, 'title', '(configured)')}.")
    for title in PUBLISHER_TABS:
        state = states[title]
        before = f"exists, {state.used_rows} used rows" if state.exists else "new tab"
        rows = (
            f"1 header + {len(plan.tabs[title].rows)} rows"
            if title in plan.tabs
            else "1 header + earlier publications + this publication"
        )
        print(f"  {title}: {before}; would write {rows}.")
    if ownership:
        local_report |= {"status": "DESTINATION_REFUSED", "issues": ownership}
        for problem in ownership:
            print(f"Destination refused: {problem}.", file=sys.stderr)
        return finish(1)

    if not args.write_sheets:
        local_report["status"] = "PREVIEW_ONLY"
        print("Preview only: no Sheet was changed.")
        return finish(0)

    # 3. Publish: local backup, full replacement of publisher tabs, readback, run log.
    try:
        outcome = publish(
            spreadsheet,
            plan,
            published_at=published_at,
            backup_dir=run_dir,
            progress=lambda step: print(f"  ... {step}", flush=True),
        )
    except DestinationError as exc:
        local_report |= {"status": "DESTINATION_REFUSED", "error": str(exc)}
        print(f"Publication refused before any write: {exc}", file=sys.stderr)
        return finish(1)
    spreadsheet_id = getattr(spreadsheet, "id", "")
    local_report |= {
        "status": outcome.status,
        "tabs_written": outcome.tabs_written,
        "readback_issues": outcome.readback_issues,
        "error": outcome.error,
        "tab_links": {
            title: f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={gid}"
            for title, gid in outcome.tab_ids.items()
        },
        "backups": sorted(p.name for p in run_dir.glob("backup_*.json")),
        "retry": "Rerun the same --write-sheets command after fixing the cause; it replaces "
        "all five publisher tabs and updates this publication's run-log row.",
    }
    print(
        f"Publication {outcome.status}: tabs written {len(outcome.tabs_written)} of "
        f"{len(PUBLISHER_TABS)}; readback issues {len(outcome.readback_issues)}."
    )
    for title in DATA_TABS:
        print(f"  {title}: {len(plan.tabs[title].rows)} rows planned.")
    if outcome.error:
        print(f"  Error: {outcome.error}", file=sys.stderr)
    for issue in outcome.readback_issues[:10]:
        print(f"  Readback: {issue}", file=sys.stderr)
    return finish(0 if outcome.status == "SUCCESS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
