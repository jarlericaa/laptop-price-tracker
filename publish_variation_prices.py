#!/usr/bin/env python3
"""Manual publication of the Shopee-variation reports (shopee-variation-sheets-1) to Sheets.

Preview is the default stopping point: ``--preview`` validates every input, plans every tab and
reads the live workbook with the read-only scope. ``--write-sheets`` is a separate, explicit step.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.sheet_publication import (
    RETIRED_TABS,
    DestinationError,
    PublicationInputError,
    read_tab,
    rollback,
    write_plan_notes_csv,
)
from laptop_tracker.variation_publication import (
    CONTRACT_PATH,
    DATA_TABS,
    HISTORY_TAB,
    PUBLISHER_TABS,
    PUBLISHER_VERSION,
    REVIEW_TAB,
    RUN_LOG_TAB,
    apply_live_retention,
    build_variation_plan,
    check_destinations,
    load_contract,
    load_variation_report,
    publish_variation,
    retired_tab_titles,
    write_plan_csv,
)
from laptop_tracker.variation_ranking import LIST_NAME

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "output"
HTTP_TIMEOUT_SECONDS = (15.0, 90.0)  # (connect, read/send)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Publish the reviewed Shopee-variation reports (build_variation_reports.py) to the "
            "prepared workbook tabs TOP_100_SHOPEE_VARIATIONS, PILOT_PRICE_COMPARISON, "
            "PILOT_PRICE_OVERVIEW, PILOT_PRICE_ALTERNATIVES, PILOT_PRICE_HISTORY, "
            "PILOT_PRICE_REVIEW and PILOT_PRICE_RUN_LOG. --preview (the default stopping point) "
            "validates and inspects the destination read-only; --write-sheets saves a full local "
            "snapshot of each tab, replaces its content, verifies every cell by readback; "
            "--rollback restores the data tabs from such a snapshot. Never fetches retailer "
            "pages, reruns matching, imports observations, reads raw, creates or writes any other "
            "tab (retired tabs are never recreated), or accepts legacy configuration-grain "
            "reports."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Validate and plan; no writes.")
    mode.add_argument("--write-sheets", action="store_true", help="Publish to the variation tabs.")
    mode.add_argument(
        "--rollback",
        type=Path,
        metavar="PUBLICATION_DIR",
        help="Restore the data tabs backed up in an earlier --write-sheets folder.",
    )
    parser.add_argument(
        "--variation-dir", type=Path, help="build_top_shopee_variations.py output folder."
    )
    parser.add_argument(
        "--reports-dir", type=Path, help="build_variation_reports.py output folder."
    )
    parser.add_argument(
        "--context-report-dir",
        type=Path,
        help="track_prices.py report folder built from the context list.",
    )
    parser.add_argument(
        "--collection-dir", type=Path, help="collect_and_match.py run folder (optional)."
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=CONTRACT_PATH,
        help="Sheet header contract JSON (default: the committed contract).",
    )
    parser.add_argument("--output-root", type=Path, default=OUTPUT / "sheet_publications")
    args = parser.parse_args(argv)
    if args.rollback is None and not (
        args.variation_dir and args.reports_dir and args.context_report_dir
    ):
        parser.error(
            "--preview and --write-sheets require --variation-dir, --reports-dir and "
            "--context-report-dir."
        )
    return args


def open_configured_spreadsheet(read_only: bool):
    from laptop_tracker.config import Settings
    from laptop_tracker.sheets_source import connect, open_spreadsheet

    settings = Settings.from_env()
    client = connect(settings, read_only=read_only)
    client.set_timeout(HTTP_TIMEOUT_SECONDS)
    return open_spreadsheet(client, settings.spreadsheet_id or "")


def main(
    argv: list[str] | None = None,
    *,
    spreadsheet_factory: Callable[[bool], object] | None = None,
    now: datetime | None = None,
) -> int:
    args = parse_args(argv)
    published_at = (now or datetime.now(UTC)).astimezone(UTC)
    if args.rollback is not None:
        return run_rollback(args, published_at, spreadsheet_factory)
    mode = "write" if args.write_sheets else "preview"

    # 1. Validate every input and build the complete plan before any Sheets access.
    try:
        contract = load_contract(args.contract)
        report = load_variation_report(
            args.variation_dir,
            args.reports_dir,
            args.context_report_dir,
            collection_dir=args.collection_dir,
        )
        plan = build_variation_plan(report, contract)
    except (PublicationInputError, OSError, ValueError, KeyError) as exc:
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
    counts = report.counts
    local_report: dict = {
        "mode": mode.upper(),
        "publisher_version": PUBLISHER_VERSION,
        "schema_version": contract.schema_version,
        "ranking_grain": contract.ranking_grain,
        "publication_id": plan.publication_id,
        "started_at_utc": published_at.isoformat(timespec="seconds"),
        "report_week": week,
        "report_cutoff_utc": report.summary["as_of_utc"],
        "sales_snapshot": report.summary["sales_snapshot"],
        "counts": counts,
        "planned_rows": {t: len(tab.rows) for t, tab in plan.tabs.items()}
        | {RUN_LOG_TAB: "previous publications + 1"},
        "planned_cell_notes": notes,
        "retired_tabs_never_written": list(RETIRED_TABS),
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

    print(f"Schema {contract.schema_version}; row grain {contract.ranking_grain}.")
    print(
        f"Reporting week {week['start']} to {week['end_exclusive']} (exclusive, Asia/Singapore), "
        f"{week['status']}; report cutoff {report.summary['as_of_utc']}."
    )
    print(f"Shopee sales snapshot: {report.summary['sales_snapshot'].get('label', '')}.")
    scope = report.top_summary["counts"]
    local_report["selection_scope"] = {
        "list_name": LIST_NAME,
        "selection_policy": report.top_summary["selection_policy"],
        **{
            name: scope[name]
            for name in (
                "eligible_variations",
                "eligible_variations_with_verified_mapping",
                "excluded_from_top_consideration",
                "excluded_by_mapping_failure",
                "selected_variations",
                "distinct_configuration_contexts",
                "shortfall",
                "shortfall_reason",
            )
        },
    }
    print(
        f"{LIST_NAME}: {scope['eligible_variations']} eligible variations, "
        f"{scope['eligible_variations_with_verified_mapping']} with a verified mapping, "
        f"{scope['excluded_from_top_consideration']} excluded; selected "
        f"{scope['selected_variations']} (shortfall {scope['shortfall']})."
    )
    if scope["shortfall"]:
        print(f"Shortfall: {scope['shortfall_reason']}.")
    print(
        f"Selected variations: {counts['selected_variations']}; distinct configuration contexts: "
        f"{counts['distinct_configuration_contexts']}; mapped "
        f"{counts['mapped_selected_variations']}"
        f", unmapped {counts['unmapped_selected_variations']}, without a trusted context "
        f"{counts['rows_without_trusted_context']}; own Shopee price "
        f"{counts['rows_with_own_shopee_price']} of {counts['selected_variations']}."
    )
    print(
        "Retailer coverage: rows with a matching price "
        f"{counts['matching_priced_variation_rows']}, "
        f"rows showing an alternative {counts['alternative_priced_variation_rows']}, rows with any "
        f"price {counts['variation_rows_with_any_retailer_price']}; contexts with a matching price "
        f"{counts['contexts_with_matching_price']} of {counts['distinct_configuration_contexts']}; "
        f"unique matching observations {counts['unique_matching_observations']} behind "
        f"{counts['row_associations_matching']} row associations; ALT 1 offers "
        f"{counts['alt_1_offers']}, ALT 2 offers {counts['alt_2_offers']}."
    )

    # 2. Open the configured spreadsheet: read-only scope for preview, write scope to publish.
    factory = spreadsheet_factory or open_configured_spreadsheet
    try:
        spreadsheet = factory(not args.write_sheets)
        states = {title: read_tab(spreadsheet, title, PUBLISHER_TABS) for title in PUBLISHER_TABS}
        present_retired = retired_tab_titles(spreadsheet)
    except Exception as exc:  # Access problems are reported without credentials.
        local_report |= {"status": "ACCESS_FAILED", "error": f"{type(exc).__name__}: {exc}"}
        print(
            f"Could not open or read the configured spreadsheet ({type(exc).__name__}: {exc}). "
            "No Sheet was changed.",
            file=sys.stderr,
        )
        return finish(1)

    problems, live = check_destinations(states, plan)
    # Earlier history/review records outside this publication's scope stay as they are.
    plan = apply_live_retention(plan, states)
    for title in (HISTORY_TAB, REVIEW_TAB):
        write_plan_csv(run_dir / f"plan_{title}.csv", plan.tabs[title])
    local_report["retained_earlier_records"] = plan.retained
    local_report["planned_rows"] = {t: len(tab.rows) for t, tab in plan.tabs.items()} | {
        RUN_LOG_TAB: "previous publications + 1"
    }
    local_report["published_content_sha256"] = plan.content_sha256
    local_report["destination_before"] = live
    (run_dir / "live_schema.json").write_text(
        json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Destination spreadsheet: {getattr(spreadsheet, 'title', '(configured)')}.")
    for title in PUBLISHER_TABS:
        state = states[title]
        before = f"exists, {state.used_rows} used rows" if state.exists else "MISSING"
        rows = (
            f"1 header + {len(plan.tabs[title].rows)} rows"
            if title in plan.tabs
            else "1 header + earlier publications + this publication"
        )
        kept = plan.retained.get(title)
        extra = f" ({kept} earlier live records retained)" if kept else ""
        print(f"  {title}: {before}; would write {rows}{extra}.")
    if present_retired:
        print(f"Retired tab(s) still present and untouched: {', '.join(present_retired)}.")
    print(f"Retired tabs ({', '.join(RETIRED_TABS)}) are never written or recreated.")
    if problems:
        local_report |= {"status": "DESTINATION_REFUSED", "issues": problems}
        for problem in problems:
            print(f"Destination refused: {problem}.", file=sys.stderr)
        return finish(1)

    if not args.write_sheets:
        local_report.setdefault("status", "PREVIEW_ONLY")
        print("Preview only: no Sheet was changed.")
        return finish(0)

    # 3. Publish: local backup, full replacement of the template tabs, readback, run log.
    try:
        outcome = publish_variation(
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
            t: f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={gid}"
            for t, gid in outcome.tab_ids.items()
        },
        "backups": sorted(p.name for p in run_dir.glob("backup_*.json")),
        "retry": "Rerun the same --write-sheets command after fixing the cause; it replaces all "
        f"{len(PUBLISHER_TABS)} tabs and updates this publication's run-log row.",
        "rollback": f"python publish_variation_prices.py --rollback {run_dir}",
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


def run_rollback(args, started_at: datetime, spreadsheet_factory) -> int:
    """Restore the data tabs from a publication folder's snapshots (write scope)."""
    source = args.rollback
    if not source.is_dir() or not any(source.glob("backup_*.json")):
        print(f"No backup_*.json snapshots in {source}; nothing was changed.", file=sys.stderr)
        return 1
    run_dir = args.output_root / f"{started_at.strftime('%Y%m%dT%H%M%S%fZ')}_rollback"
    run_dir.mkdir(parents=True, exist_ok=False)
    report: dict = {
        "mode": "ROLLBACK",
        "source": str(source),
        "started_at_utc": started_at.isoformat(timespec="seconds"),
    }
    factory = spreadsheet_factory or open_configured_spreadsheet
    try:
        spreadsheet = factory(False)
        outcome = rollback(
            spreadsheet,
            source,
            progress=lambda s: print(f"  ... {s}", flush=True),
            tabs=DATA_TABS,
            allowed=PUBLISHER_TABS,
        )
    except DestinationError as exc:
        report |= {"status": "DESTINATION_REFUSED", "error": str(exc)}
        print(f"Rollback refused: {exc}", file=sys.stderr)
        code = 1
    except Exception as exc:  # Access problems are reported without credentials.
        report |= {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        print(f"Rollback failed ({type(exc).__name__}: {exc}).", file=sys.stderr)
        code = 1
    else:
        report |= {
            "status": outcome.status,
            "restored": outcome.restored,
            "skipped": outcome.skipped,
            "readback_issues": outcome.issues,
        }
        print(f"Rollback {outcome.status}: restored {', '.join(outcome.restored) or 'nothing'}.")
        for line in outcome.skipped + outcome.issues[:10]:
            print(f"  {line}")
        code = 0 if outcome.status == "SUCCESS" else 1
    report["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    (run_dir / "rollback_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Local rollback report: {run_dir}.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
