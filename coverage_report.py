#!/usr/bin/env python3
"""Offline configuration x retailer coverage report for a saved collection (2026-10-15)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.match_run import matcher_from_validated, read_observations_csv
from laptop_tracker.observation_store import observation_id
from laptop_tracker.pilot_coverage import (
    COVERAGE_COLUMNS,
    COVERAGE_TEXT,
    OFFER_DETAIL_COLUMNS,
    OfferRecord,
    coverage,
)
from laptop_tracker.price_tracking import RETAILERS, utc_text
from laptop_tracker.replay import price_comparability, reparse_saved_pages
from laptop_tracker.target_master import read_table_snapshot_csv, validate_target_master
from laptop_tracker.verified_top import sha256_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Explain, for every configuration in scope and every compared retailer, what the "
            "recorded discovery and collection stages found. Saved observations are matched "
            "again offline with the current rules; nothing is fetched, imported, or published."
        )
    )
    parser.add_argument("--observations", type=Path, required=True, help="Collection CSV.")
    parser.add_argument("--target-master", type=Path, required=True, help="Validated snapshot.")
    parser.add_argument("--scope", type=Path, required=True, help="CSV with a target_id column.")
    parser.add_argument("--match-results", type=Path,
                        help="The collection's match_results.csv (manifest target hints).")
    parser.add_argument("--html-dir", type=Path,
                        help="Saved <content_sha256>.html bodies to re-parse with the adapters.")
    parser.add_argument("--discovery-dir", type=Path,
                        help="Folder with search_plan.csv, search_requests.csv, candidates.csv.")
    parser.add_argument("--collection-dir", type=Path,
                        help="Folder with request_report.csv (and the manifest beside it).")
    parser.add_argument("--manifest", type=Path, help="Collection manifest CSV.")
    parser.add_argument("--history", type=Path, help="price_history.csv from a price report.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder.")
    return parser.parse_args(argv)


def read(path: Path | None) -> list[dict[str, str]] | None:
    if path is None or not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"Coverage stopped: {args.output_dir} already exists.", file=sys.stderr)
        return 1
    matcher = matcher_from_validated(
        validate_target_master(read_table_snapshot_csv(args.target_master))
    )
    observations = read_observations_csv(args.observations)
    stage = "original observation records"
    if args.html_dir:
        observations, failures = reparse_saved_pages(observations, args.html_dir)
        stage = f"saved HTML re-parsed with current adapters ({len(failures)} failures)"
    hints = {
        row["observation_id"]: row.get("expected_target_id", "")
        for row in read(args.match_results) or ()
    }
    offers = []
    for item in observations:
        observation = item.observation
        result = matcher.match_observation(observation)
        key = observation_id(observation)
        offers.append(OfferRecord(
            retailer_id=observation.retailer_id,
            offer_url=observation.offer_url,
            observation_id=key,
            retrieved_at=utc_text(observation.retrieved_at),
            match_status=result.status.value,
            target_id=result.target_id or "",
            candidate_target_ids=result.candidate_target_ids,
            reason_codes=result.reason_codes,
            price=format(observation.price, "f"),
            availability=observation.availability.value,
            price_eligibility=price_comparability(observation, result),
            expected_target_id=hints.get(key, ""),
            bundle_flag={None: "", True: "TRUE", False: "FALSE"}[
                observation.details.bundle_flag if observation.details else None
            ],
        ))
    scope = [row["target_id"] for row in read(args.scope) or ()]
    discovery = args.discovery_dir
    collection = args.collection_dir
    pairs, details = coverage(
        scope,
        [retailer_id for _, retailer_id in RETAILERS],
        offers,
        search_plan=read(discovery / "search_plan.csv") if discovery else None,
        search_requests=read(discovery / "search_requests.csv") if discovery else None,
        candidates=read(discovery / "candidates.csv") if discovery else None,
        manifest=read(args.manifest),
        request_report=read(collection / "request_report.csv") if collection else None,
        history=read(args.history),
    )
    write(args.output_dir / "coverage.csv", COVERAGE_COLUMNS, pairs)
    write(args.output_dir / "coverage_offers.csv", OFFER_DETAIL_COLUMNS, details)
    counts = Counter(row["coverage_status"] for row in pairs)
    summary = {
        "description": "Offline coverage report; no requests, store imports, or Sheets writes.",
        "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "matching_input": stage,
        "input_sha256": {
            str(path): sha256_file(path)
            for path in (args.observations, args.target_master, args.scope, args.match_results,
                         args.manifest, args.history)
            if path and path.is_file()
        },
        "configurations": len(scope),
        "retailers": len(RETAILERS),
        "pairs": len(pairs),
        "offers": len(offers),
        "offer_detail_rows": len(details),
        "coverage_statuses": dict(sorted(counts.items())),
        "discovery_logs": bool(discovery),
    }
    (args.output_dir / "coverage_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Pairs: {len(pairs)} ({len(scope)} configurations x {len(RETAILERS)} retailers); "
          f"offers: {len(offers)}.")
    for status, count in sorted(counts.items()):
        print(f"  {status} ({COVERAGE_TEXT[status]}): {count}")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
