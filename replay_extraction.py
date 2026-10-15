#!/usr/bin/env python3
"""Offline replay of a saved collection through the current extractor, matcher, and adapters."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.match_run import (
    MATCH_EVIDENCE_COLUMNS,
    MATCH_RESULT_COLUMNS,
    ObservationFileError,
    TargetSnapshotError,
    evidence_records,
    is_review_item,
    matcher_from_validated,
    read_observations_csv,
    result_record,
    write_records,
)
from laptop_tracker.offers import OBSERVATION_COLUMNS, OFFER_DETAIL_COLUMNS
from laptop_tracker.replay import (
    COMPARISON_COLUMNS,
    before_row,
    comparison_row,
    conflict_check,
    match_items,
    record_differences,
    reparse_saved_pages,
)
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    validate_target_master,
)
from laptop_tracker.verified_top import sha256_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a saved collection offline. Path 1 matches the original observation CSV "
            "again with the current extractor and matcher. Path 2 (--html-dir) re-parses the "
            "saved response bodies with the current adapters, reusing each observation's "
            "recorded retrieval time; those re-parsed records are written separately and are "
            "never imported. Writes a before/after comparison into a new output directory and "
            "prints counts only. Makes no requests and never writes Google Sheets."
        )
    )
    parser.add_argument("--observations", type=Path, required=True, help="Original CSV.")
    parser.add_argument("--target-master", type=Path, required=True, help="Validated snapshot.")
    parser.add_argument("--html-dir", type=Path, help="Folder of saved <content_sha256>.html.")
    parser.add_argument(
        "--before-results", type=Path, help="The collection's saved match_results.csv."
    )
    parser.add_argument(
        "--cases", type=Path, help="Optional cases.json naming review cases by content SHA-256."
    )
    parser.add_argument(
        "--conflict-check-store",
        type=Path,
        help="Store to copy (read-only) and try importing re-parsed records into the copy.",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder.")
    return parser.parse_args(argv)


def write_rows(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def write_match_reports(folder: Path, matched) -> None:
    folder.mkdir()
    results = [result_record(item, result) for item, result in matched]
    write_records(folder / "offer_match_results.csv", MATCH_RESULT_COLUMNS, results)
    write_records(
        folder / "offer_match_review_queue.csv",
        MATCH_RESULT_COLUMNS,
        (r for r, (_, result) in zip(results, matched, strict=True) if is_review_item(result)),
    )
    write_records(
        folder / "offer_match_evidence.csv",
        MATCH_EVIDENCE_COLUMNS,
        (row for item, result in matched for row in evidence_records(item, result)),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"Replay stopped: {args.output_dir} already exists.", file=sys.stderr)
        return 1
    try:
        targets = validate_target_master(read_table_snapshot_csv(args.target_master))
        matcher = matcher_from_validated(targets)
        observations = read_observations_csv(args.observations)
        before = []
        if args.before_results:
            with args.before_results.open(newline="", encoding="utf-8") as handle:
                before = list(csv.DictReader(handle))
        cases = json.loads(args.cases.read_text(encoding="utf-8")) if args.cases else []
    except (
        FileNotFoundError,
        ValueError,
        TargetMasterSchemaError,
        TargetSnapshotError,
        ObservationFileError,
    ) as exc:
        print(f"Replay stopped: {exc}", file=sys.stderr)
        return 1
    case_ids = {case["content_sha256"]: case["case_id"] for case in cases}

    comparison = [before_row(record, case_ids) for record in before]
    csv_matched = match_items(matcher, observations)
    write_match_reports(args.output_dir / "observation_replay", csv_matched)
    comparison += [
        comparison_row("AFTER: original observation through current extractor", item, result,
                       case_ids)
        for item, result in csv_matched
    ]
    summary: dict = {
        "description": "Offline extraction replay; no requests, no Sheets, no store writes.",
        "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "input_sha256": {
            str(path): sha256_file(path)
            for path in (args.observations, args.target_master, args.before_results)
            if path
        },
        "observations": len(observations),
        "observation_replay_statuses": dict(
            Counter(result.status.value for _, result in csv_matched)
        ),
    }

    if args.html_dir:
        replayed, failures = reparse_saved_pages(observations, args.html_dir)
        folder = args.output_dir / "html_replay"
        html_matched = match_items(matcher, replayed)
        write_match_reports(folder, html_matched)
        write_rows(
            folder / "reparsed_observations_NOT_FOR_IMPORT.csv",
            [*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS],
            (item.observation.to_full_record() for item in replayed),
        )
        originals = {item.source_row_number: item.observation for item in observations}
        write_rows(
            folder / "reparse_differences.csv",
            ("source_row_number", "retailer_id", "changed_columns"),
            (
                {
                    "source_row_number": item.source_row_number,
                    "retailer_id": item.observation.retailer_id,
                    "changed_columns": ";".join(
                        record_differences(originals[item.source_row_number], item.observation)
                    ),
                }
                for item in replayed
            ),
        )
        write_rows(
            folder / "reparse_failures.csv",
            ("source_row_number", "retailer_id", "offer_url", "code", "message"),
            (failure.__dict__ for failure in failures),
        )
        comparison += [
            comparison_row("AFTER: saved HTML through current adapter and extractor", item,
                           result, case_ids)
            for item, result in html_matched
        ]
        summary |= {
            "html_reparsed": len(replayed),
            "html_reparse_failures": len(failures),
            "html_replay_statuses": dict(
                Counter(result.status.value for _, result in html_matched)
            ),
            "reparsed_records_imported": False,
        }
        if args.conflict_check_store:
            summary["store_conflict_check"] = conflict_check(
                args.conflict_check_store,
                [item.observation for item in replayed],
                args.output_dir,
            )

    comparison.sort(key=lambda r: (int(r["source_row_number"] or 0), r["stage"]))
    write_rows(args.output_dir / "before_after_comparison.csv", COMPARISON_COLUMNS, comparison)
    (args.output_dir / "replay_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Observations replayed: {len(observations)}.")
    print(f"  Original records, current extractor: {summary['observation_replay_statuses']}.")
    if args.html_dir:
        print(
            f"  Saved HTML re-parsed: {summary['html_reparsed']} "
            f"(failures {summary['html_reparse_failures']}); "
            f"statuses {summary['html_replay_statuses']}. Not imported."
        )
        if "store_conflict_check" in summary:
            print(f"  Store conflict check: {summary['store_conflict_check'].split(':')[0]}.")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
