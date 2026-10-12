#!/usr/bin/env python3
"""Manual online collection and matching of explicitly listed retailer product pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.collection import (
    EVIDENCE_COLUMNS,
    FAILURE_COLUMNS,
    REQUEST_COLUMNS,
    RESULT_COLUMNS,
    REVIEW_COLUMNS,
    ManifestError,
    adapter_summary,
    collect,
    collected_observations,
    evidence_rows,
    failure_rows,
    match_collected,
    read_manifest,
    request_rows,
    result_row,
    review_reasons,
    run_status,
    validate_manifest,
)
from laptop_tracker.config import Settings
from laptop_tracker.match_run import TargetSnapshotError, matcher_from_validated
from laptop_tracker.observation_store import (
    ObservationConflictError,
    ObservationStore,
    ObservationStoreError,
    persist_scrape_results,
)
from laptop_tracker.offers import OBSERVATION_COLUMNS, OFFER_DETAIL_COLUMNS
from laptop_tracker.price_tracking import (
    MATCHING_RULES_VERSION,
    PriceTrackingInputError,
    matching_context,
    utc_text,
    validate_selected_list,
)
from laptop_tracker.scraping import Clock, FetchConfig, Fetcher, SystemClock, Transport
from laptop_tracker.sheets_source import SheetsSourceError, read_target_table_values
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    validate_target_master,
)
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    LEDGER_COLUMNS,
    VerifiedRankingInputError,
    read_csv_rows,
    sha256_file,
    sha256_values,
    write_csv,
)

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "output"
OUTPUT_FILES = {
    "requests": "request_report.csv",
    "failures": "request_failures.csv",
    "observations": "observations.csv",
    "results": "match_results.csv",
    "review": "match_review_queue.csv",
    "evidence": "match_evidence.csv",
    "summary": "collection_run_summary.json",
}
EXIT_CODES = {"PREVIEW_ONLY": 0, "SUCCESS": 0, "PARTIAL_SUCCESS": 2, "FAILED": 1}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual online collection and matching. Validates a URL manifest, TARGET_MASTER, and "
            "the selected working list, then (with --live only) fetches each enabled product "
            "page once through the retailer adapters and the shared Fetcher, stores valid "
            "observations, matches them with the five-field ExactMatcher, and writes a "
            "request report, failures, an observation CSV, match results, a review queue, field "
            "evidence, and a run summary into a new run directory. --preview sends no request. "
            "No search, crawling, or URL discovery; never reads raw or writes Google Sheets."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Validate and plan; no requests.")
    mode.add_argument("--live", action="store_true", help="Send the listed retailer requests.")
    parser.add_argument("--manifest", type=Path, required=True, help="URL manifest CSV.")
    catalog = parser.add_mutually_exclusive_group()
    catalog.add_argument(
        "--target-master", type=Path, default=OUTPUT / "target_master_validated.csv"
    )
    catalog.add_argument(
        "--target-master-from-sheets",
        action="store_true",
        help="Read only the TARGET_MASTER tab from the configured spreadsheet (read-only).",
    )
    parser.add_argument(
        "--selected-list",
        type=Path,
        default=OUTPUT / "verified_top_100" / "top_100_verified_configurations.csv",
    )
    parser.add_argument(
        "--contributions",
        type=Path,
        default=OUTPUT / "verified_top_100" / "verified_configuration_contributions.csv",
    )
    parser.add_argument(
        "--store", type=Path, default=ROOT / "data" / "history" / "price_history.sqlite3"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT / "collection_runs",
        help="Parent of the new run directory (default: data/output/collection_runs).",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Exact run directory; it must not exist yet."
    )
    return parser.parse_args(argv)


def load_target_master(args: argparse.Namespace) -> tuple[list[list[str]], str, str]:
    """TARGET_MASTER values, a source label, and its SHA-256."""
    if args.target_master_from_sheets:
        values = read_target_table_values(Settings.from_env(), ("TARGET_MASTER",))[
            "TARGET_MASTER"
        ]
        if values is None:
            raise SheetsSourceError("TARGET_MASTER worksheet not found; tabs are never created.")
        return values, "Sheets tab TARGET_MASTER (read-only)", sha256_values(values)
    return (
        read_table_snapshot_csv(args.target_master),
        str(args.target_master),
        sha256_file(args.target_master),
    )


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
) -> int:
    args = parse_args(argv)
    clock = clock or SystemClock()
    started = clock.now().astimezone(UTC)
    mode = "LIVE" if args.live else "PREVIEW"

    # 1. Validate the catalog, the selected identities, and the whole manifest first.
    try:
        values, catalog_label, catalog_sha256 = load_target_master(args)
        target_master = validate_target_master(values)
        matcher = matcher_from_validated(target_master)
        selected = validate_selected_list(
            read_csv_rows(args.selected_list, CONFIGURATION_COLUMNS, "Selected list"),
            read_csv_rows(args.contributions, LEDGER_COLUMNS, "Contribution ledger"),
            target_master,
            target_master_sha256=catalog_sha256,
            list_sha256=sha256_file(args.selected_list),
        )
        ranks = {item.target_id: item.rank for item in selected.configurations}
        entries = validate_manifest(read_manifest(args.manifest), ranks)
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        KeyError,
        ValueError,
        SheetsSourceError,
        TargetMasterSchemaError,
        TargetSnapshotError,
        VerifiedRankingInputError,
        PriceTrackingInputError,
        ManifestError,
    ) as exc:
        print(f"Collection stopped before any retailer request: {exc}", file=sys.stderr)
        return 1

    run_id = f"{started.strftime('%Y%m%dT%H%M%S%fZ')}_{mode.lower()}"
    run_dir = args.output_dir or args.output_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"Collection stopped: run directory {run_dir} already exists.", file=sys.stderr)
        return 1
    outputs = {key: run_dir / name for key, name in OUTPUT_FILES.items()}
    context = matching_context(
        target_master, target_master_sha256=catalog_sha256, source_label=catalog_label
    )
    enabled = [entry for entry in entries if entry.enabled]
    summary: dict[str, object] = {
        "description": "Manual online collection and matching of listed product pages.",
        "run_id": run_id,
        "mode": mode,
        "started_at_utc": utc_text(started),
        "input_sha256": {
            str(args.manifest): sha256_file(args.manifest),
            catalog_label: catalog_sha256,
            str(args.selected_list): selected.sha256,
            str(args.contributions): sha256_file(args.contributions),
        },
        "selected_list": {
            "configurations": len(selected.configurations),
            "catalog_matches_build": selected.catalog_matches_build,
        },
        "matching_context": {
            "context_id": context.context_id,
            "target_master_sha256": catalog_sha256,
            "configuration_targets": context.configuration_targets,
            "listing_only_targets": context.listing_only_targets,
            "matching_rules_version": MATCHING_RULES_VERSION,
            "matching_rules_sha256": context.matching_rules_sha256,
        },
        "manifest": {
            "rows": len(entries),
            "enabled": len(enabled),
            "disabled": len(entries) - len(enabled),
            "by_retailer": dict(sorted(Counter(e.retailer_id for e in enabled).items())),
        },
        "adapters": adapter_summary(),
    }
    by_retailer = ", ".join(
        f"{k} {v}" for k, v in sorted(Counter(e.retailer_id for e in enabled).items())
    )

    if not args.live:
        write_csv(outputs["requests"], REQUEST_COLUMNS, request_rows(entries, None))
        summary |= {
            "run_status": "PREVIEW_ONLY",
            "requests_sent": 0,
            "finished_at_utc": utc_text(clock.now()),
            "outputs": {key: str(outputs[key]) for key in ("requests", "summary")},
        }
        outputs["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(
            f"Preview only: {len(entries)} manifest rows valid; {len(enabled)} enabled requests "
            f"would be sent ({by_retailer}). No retailer request was made and the store was "
            "not opened."
        )
        print(f"Plan written to {run_dir}.")
        return 0

    # 2. Collect: one shared fetcher, so throttling state is shared across all requests.
    fetcher = Fetcher(transport, FetchConfig(), clock)
    summary["outputs"] = {key: str(path) for key, path in outputs.items()}
    summary["fetch_config"] = {
        "timeout_seconds": fetcher.config.timeout_seconds,
        "max_attempts": fetcher.config.max_attempts,
        "min_request_interval_seconds": fetcher.config.min_request_interval_seconds,
        "max_redirects": fetcher.config.max_redirects,
        "live_transport": fetcher.is_live,
    }
    outcomes = collect(entries, fetcher)
    collected = collected_observations(outcomes)
    write_csv(outputs["requests"], REQUEST_COLUMNS, request_rows(entries, outcomes))
    write_csv(outputs["failures"], FAILURE_COLUMNS, failure_rows(outcomes))
    write_csv(
        outputs["observations"],
        [*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS],
        [observation.to_full_record() for _, _, observation in collected],
    )

    # 3. Persist and match. Nothing is stored or matched when no valid observation exists.
    results: list[dict[str, str]] = []
    evidence: list[dict[str, str]] = []
    store_report: dict[str, object] = {"path": str(args.store), "opened": False}
    stored = False
    if collected:
        try:
            with ObservationStore.open(args.store) as store:
                before = store.counts()
                report, failures_seen = persist_scrape_results(
                    store,
                    [outcome.result for outcome in outcomes],
                    imported_at=started,
                    source_label=f"collect_and_match {run_id}",
                )
                added, matches = match_collected(store, context, matcher, collected, now=started)
                after = store.counts()
            stored = True
            results = [result_row(match, ranks, context) for match in matches]
            evidence = [row for match in matches for row in evidence_rows(match)]
            store_report |= {
                "opened": True,
                "before": before,
                "after": after,
                "import": {
                    "batch_id": report.batch_id,
                    "received": report.received,
                    "new": report.new,
                    "duplicates_in_store": report.duplicates_in_store,
                    "duplicates_in_batch": report.duplicates_in_batch,
                    "scrape_failures_not_stored": failures_seen,
                },
                "match_results_added": added,
            }
        except (ObservationConflictError, ObservationStoreError, sqlite3.Error) as exc:
            store_report["error"] = f"{type(exc).__name__}: {exc}"
            print(f"Store or matching step failed: {exc}", file=sys.stderr)
    write_csv(outputs["results"], RESULT_COLUMNS, results)
    write_csv(
        outputs["review"],
        REVIEW_COLUMNS,
        [{"review_reasons": ";".join(r)} | row for row in results if (r := review_reasons(row))],
    )
    write_csv(outputs["evidence"], EVIDENCE_COLUMNS, evidence)

    status = run_status(outcomes, stored)
    statuses = Counter(outcome.status for outcome in outcomes)
    summary |= {
        "run_status": status,
        "finished_at_utc": utc_text(clock.now()),
        "requests_sent": len(outcomes),
        "request_statuses": dict(sorted(statuses.items())),
        "failure_codes": dict(
            sorted(
                Counter(
                    f"{f.stage.value}:{f.error_code}" for o in outcomes for f in o.result.failures
                ).items()
            )
        ),
        "observations_collected": len(collected),
        "observations_csv_sha256": hashlib.sha256(
            outputs["observations"].read_bytes()
        ).hexdigest(),
        "store": store_report,
        "match_statuses": dict(sorted(Counter(r["match_status"] for r in results).items())),
        "expected_target_checks": dict(
            sorted(Counter(r["expected_target_check"] for r in results).items())
        ),
        "selected_list_statuses": dict(
            sorted(Counter(r["selected_list_status"] or "-" for r in results).items())
        ),
        "availability": dict(sorted(Counter(r["availability"] for r in results).items())),
        "review_rows": sum(1 for row in results if review_reasons(row)),
    }
    outputs["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    # Counts and codes only: URLs, prices, and target IDs stay in the run directory.
    print(
        f"Live collection {status}: {len(outcomes)} requests sent "
        f"({', '.join(f'{k} {v}' for k, v in sorted(statuses.items()))}); "
        f"{len(collected)} valid observations."
    )
    if summary["failure_codes"]:
        codes = ", ".join(f"{k} {v}" for k, v in summary["failure_codes"].items())
        print(f"  Failures: {codes}.")
    if stored:
        imported = store_report["import"]
        print(
            f"  Stored: {imported['new']} new, {imported['duplicates_in_store']} already stored; "
            f"match results added: {store_report['match_results_added']}."
        )
        print(
            "  Matches: "
            + ", ".join(f"{k} {v}" for k, v in summary["match_statuses"].items())
            + "; expected-target checks: "
            + ", ".join(f"{k} {v}" for k, v in summary["expected_target_checks"].items())
            + f"; review rows: {summary['review_rows']}."
        )
    elif collected:
        print("  Observations were exported but NOT stored or matched; see the summary.")
    print(f"Run directory: {run_dir}.")
    return EXIT_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
