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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.listed_spec import ListedSpecMatcher
from laptop_tracker.match_run import matcher_from_validated, read_observations_csv
from laptop_tracker.observation_store import observation_id
from laptop_tracker.offer_terms import (
    DEFAULT_OFFER_TERMS_POLICY,
    OFFER_TERMS_POLICIES,
    classify_offer_terms,
)
from laptop_tracker.pilot_coverage import (
    COVERAGE_COLUMNS,
    COVERAGE_TEXT,
    OFFER_DETAIL_COLUMNS,
    OfferRecord,
    coverage,
)
from laptop_tracker.price_tracking import RETAILERS, price_note_codes, utc_text
from laptop_tracker.replay import offer_price_eligibility, price_comparability, reparse_saved_pages
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
    parser.add_argument("--observations", type=Path, required=True, action="append",
                        help="Collection observations CSV (repeatable: earlier collections are "
                             "kept as history; the latest observation of each offer is current).")
    parser.add_argument("--target-master", type=Path, required=True, help="Validated snapshot.")
    parser.add_argument("--scope", type=Path, required=True, help="CSV with a target_id column.")
    parser.add_argument("--match-results", type=Path, action="append", default=[],
                        help="A collection's match_results.csv (manifest target hints; "
                             "repeatable).")
    parser.add_argument("--offer-terms-policy", choices=OFFER_TERMS_POLICIES,
                        default=DEFAULT_OFFER_TERMS_POLICY,
                        help="Commercial-terms rule for price eligibility.")
    parser.add_argument("--html-dir", type=Path,
                        help="Saved <content_sha256>.html bodies to re-parse with the adapters.")
    parser.add_argument("--discovery-dir", type=Path,
                        help="Folder with search_plan.csv, search_requests.csv, candidates.csv "
                             "and, from 2026-10-19 runs, search_jobs.csv.")
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
    target_master = validate_target_master(read_table_snapshot_csv(args.target_master))
    matcher = matcher_from_validated(target_master)
    listed = ListedSpecMatcher(target_master.valid_rows)
    target_rows = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    observations = [item for path in args.observations for item in read_observations_csv(path)]
    stage = "original observation records"
    if args.html_dir:
        observations, failures = reparse_saved_pages(observations, args.html_dir)
        stage = f"saved HTML re-parsed with current adapters ({len(failures)} failures)"
    # Collection lineage: the request that fetched each observation and the configuration it
    # was requested for (a hint, never an assignment).
    hints = {
        row["observation_id"]: (row.get("expected_target_id", ""), row.get("request_id", ""))
        for path in args.match_results for row in read(path) or ()
    }
    offers = []
    seen: set[str] = set()
    for item in observations:
        observation = item.observation
        key = observation_id(observation)
        if key in seen:
            continue  # The same saved observation in two inputs is one observation.
        seen.add(key)
        result = matcher.match_observation(observation)
        association = listed.associate(dict(result.offer_attributes or {}), observation.raw_title,
                                       result.reason_codes)
        terms = classify_offer_terms(observation)
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
            price_eligibility=price_comparability(observation, result, args.offer_terms_policy),
            expected_target_id=hints.get(key, ("", ""))[0],
            request_id=hints.get(key, ("", ""))[1],
            bundle_flag=(flag := {None: "", True: "TRUE", False: "FALSE"}[
                observation.details.bundle_flag if observation.details else None
            ]),
            offer_attributes=dict(result.offer_attributes or {}),
            price_notes=price_note_codes(flag, terms, args.offer_terms_policy),
            offer_terms_category=terms.category,
            offer_index=str(observation.offer_index),
            raw_title=observation.raw_title,
            listed_target_ids=association.target_ids,
            listed_ambiguous_ids=association.ambiguous_ids,
            listed_price_eligibility=offer_price_eligibility(observation, args.offer_terms_policy)
            if association.status else "",
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
        target_rows=target_rows,
        search_jobs=read(discovery / "search_jobs.csv") if discovery else None,
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
            for path in (*args.observations, args.target_master, args.scope,
                         *args.match_results, args.manifest, args.history)
            if path and path.is_file()
        },
        "offer_terms_policy": args.offer_terms_policy,
        "configurations": len(scope),
        "rows_outside_reporting_scope": sum(
            1 for row in pairs if row["in_reporting_scope"] == "FALSE"
        ),
        "blank_price_explanations": dict(sorted(Counter(
            row["coverage_status"] for row in pairs
            if row["in_reporting_scope"] == "TRUE" and row["coverage_status"] != "PRICE_AVAILABLE"
        ).items())),
        "historical_observations": sum(int(row["historical_observations"]) for row in pairs),
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
    print(f"Rows: {len(pairs)} ({len(scope)} configurations x {len(RETAILERS)} retailers, plus "
          f"{summary['rows_outside_reporting_scope']} outside the reporting scope); "
          f"offers: {len(offers)}.")
    for status, count in sorted(counts.items()):
        print(f"  {status} ({COVERAGE_TEXT[status]}): {count}")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
