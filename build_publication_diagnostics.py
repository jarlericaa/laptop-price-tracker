#!/usr/bin/env python3
"""Prepare the complete P1 audit diagnostics and a publication plan (local, preview only).

Reads one run's local files (eligible pool, Phase 1 review/excluded listings, the Shopee variation
ranking and mapping exclusions, a discovery run's search tables) and writes CSVs, a summary, a
publication plan and a README into ``--output-dir`` (under ``data/output/``). Never contacts a
retailer, never reads or writes Google Sheets, and writes nothing outside ``--output-dir``: the
plan lists the tabs that would be published, and every entry says PREVIEW_ONLY_NOT_WRITTEN.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from laptop_tracker.catalog_proposals import (  # noqa: E402
    OutputLocationError,
    check_output_dir,
    read_csv_rows,
    sha256_file,
)
from laptop_tracker.publication_diagnostics import (  # noqa: E402
    DIAGNOSTICS_VERSION,
    ELIGIBILITY_COLUMNS,
    MAPPING_EXCLUSION_COLUMNS,
    NOT_ELIGIBLE_COLUMNS,
    build_checks,
    build_eligibility_variations,
    build_mapping_exclusions,
    build_not_eligible_listings,
    build_search_coverage,
    search_coverage_summary,
    shop_name_lookup,
    write_diagnostics,
)
from laptop_tracker.sheets_writer import ALLOWED_OUTPUT_TABS  # noqa: E402

DEFAULT_RUN = ROOT / "data" / "output" / "union_2026-11-03"
WEEKLY = ROOT / "data" / "output" / "weekly_runs"


def latest_discovery_dir() -> Path | None:
    """The newest ``weekly_runs/*/discovery`` folder that has a ``search_jobs.csv``."""
    found = sorted(WEEKLY.glob("*/discovery/search_jobs.csv"), key=lambda p: p.parent.parent.name)
    return found[-1].parent if found else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument(
        "--discovery-dir", type=Path, help="Default: the newest weekly_runs/*/discovery folder."
    )
    parser.add_argument(
        "--correction-queue",
        type=Path,
        help="Optional catalog_correction_queue.csv; links mapped exclusions to queue rows.",
    )
    parser.add_argument("--output-dir", type=Path, help="Default: <run-dir>/diagnostics.")
    parser.add_argument("--dry-run", action="store_true", help="Print the counts; write nothing.")
    return parser.parse_args(argv)


def optional_rows(path: Path | None) -> list[dict[str, str]]:
    return read_csv_rows(path) if path and path.exists() else []


def header_of(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return next(csv.reader(handle), [])


def native(path: Path) -> tuple[str, int, list[str]]:
    """``(file name, rows, header)`` of a Phase 1 file that fills a native P1 tab as it is."""
    return f"phase1/{path.name}", len(read_csv_rows(path)), header_of(path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run = args.run_dir
    discovery = args.discovery_dir or latest_discovery_dir()
    queue_path = (
        args.correction_queue or run / "catalog_corrections" / "catalog_correction_queue.csv"
    )
    inputs = run / "inputs"
    paths = {
        "eligible_model_ranking": inputs / "eligible_model_ranking.csv",
        "eligibility_review": inputs / "eligibility_review.csv",
        "excluded_listings": inputs / "excluded_listings.csv",
        "source_shop_names": inputs / "source_shop_names.csv",
        "match_review": inputs / "match_review_validated.csv",
        "variation_ranking": run / "shopee_variations" / "shopee_variation_ranking.csv",
        "mapping_exclusions": run / "shopee_variations" / "mapping_exclusions.csv",
        "selection_audit": run / "shopee_variations" / "selection_audit.json",
    }
    if discovery:
        paths["search_jobs"] = discovery / "search_jobs.csv"
    try:
        out_dir = check_output_dir(args.output_dir or run / "diagnostics", ROOT)
        if not discovery:
            raise FileNotFoundError("no discovery run with a search_jobs.csv was found")
        eligible = read_csv_rows(paths["eligible_model_ranking"])
        ranking = read_csv_rows(paths["variation_ranking"])
        shops = shop_name_lookup(read_csv_rows(paths["source_shop_names"]))
        audit = json.loads(paths["selection_audit"].read_text(encoding="utf-8"))
        run_summary_path = discovery / "discovery_summary.json"
        run_summary = (
            json.loads(run_summary_path.read_text(encoding="utf-8"))
            if run_summary_path.exists()
            else {}
        )
        eligibility = build_eligibility_variations(eligible, ranking, shops)
        not_eligible = build_not_eligible_listings(
            read_csv_rows(paths["eligibility_review"]),
            read_csv_rows(paths["excluded_listings"]),
            shops,
        )
        mapping = build_mapping_exclusions(
            ranking,
            read_csv_rows(paths["mapping_exclusions"]),
            optional_rows(paths["match_review"]),
            audit,
            optional_rows(queue_path),
        )
        jobs = read_csv_rows(paths["search_jobs"])
        coverage, coverage_columns = build_search_coverage(
            jobs,
            optional_rows(discovery / "search_results.csv"),
            optional_rows(discovery / "candidates.csv"),
            optional_rows(discovery / "product_inspections.csv"),
            optional_rows(discovery / "search_requests.csv"),
        )
    except (OutputLocationError, FileNotFoundError, KeyError) as exc:
        print(f"P1 diagnostics stopped: {exc}", file=sys.stderr)
        return 1
    checks = build_checks(eligible, eligibility, mapping, coverage, jobs)
    summary = {
        "version": DIAGNOSTICS_VERSION,
        "mode": "PREVIEW_ONLY",
        "rows": {
            "eligibility_variations": len(eligibility),
            "eligibility_not_eligible_listings": len(not_eligible),
            "mapping_exclusions_diagnostic": len(mapping),
            "search_coverage": len(coverage),
        },
        "not_eligible_by_disposition": {
            d: sum(1 for r in not_eligible if r["disposition"] == d)
            for d in sorted({r["disposition"] for r in not_eligible})
        },
        "eligibility_by_selection_status": _count(eligibility, "selection_status"),
        "mapping_exclusions_by_reason": _count(mapping, "exclusion_reason"),
        "search_coverage": search_coverage_summary(coverage, jobs, run_summary),
        "discovery_dir": str(discovery),
        "inputs": {name: str(path) for name, path in paths.items()},
        "input_sha256": {name: sha256_file(path) for name, path in paths.items() if path.exists()},
        "checks": checks,
        "checks_passed": all(c["passed"] for c in checks),
    }
    print("P1 diagnostics:", ", ".join(f"{k} {v}" for k, v in summary["rows"].items()))  # type: ignore[union-attr]
    for check in checks:
        print(
            f"  {'PASS' if check['passed'] else 'FAIL'}: {check['check']} (found {check['found']})"
        )
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0 if summary["checks_passed"] else 1
    plan_files = {
        # The native Phase 1 tabs are filled from the run's own Phase 1 files, unchanged.
        "P1_ELIGIBLE_RANKING": native(paths["eligible_model_ranking"]),
        "P1_ELIGIBILITY_REVIEW": native(paths["eligibility_review"]),
        "P1_EXCLUDED_LISTINGS": native(paths["excluded_listings"]),
        "P1_ELIGIBILITY_AUDIT": (
            "eligibility_variations.csv",
            len(eligibility),
            ELIGIBILITY_COLUMNS,
        ),
        "P1_NOT_ELIGIBLE_AUDIT": (
            "eligibility_not_eligible_listings.csv",
            len(not_eligible),
            NOT_ELIGIBLE_COLUMNS,
        ),
        "P1_MAPPING_EXCLUSIONS": (
            "mapping_exclusions_diagnostic.csv",
            len(mapping),
            MAPPING_EXCLUSION_COLUMNS,
        ),
        "P1_SEARCH_COVERAGE": ("search_coverage.csv", len(coverage), coverage_columns),
    }
    write_diagnostics(
        out_dir,
        eligibility=eligibility,
        not_eligible=not_eligible,
        mapping=mapping,
        coverage=coverage,
        coverage_columns=coverage_columns,
        summary=summary,
        plan_files=plan_files,
        allowed_tabs=ALLOWED_OUTPUT_TABS,
    )
    print(f"Written to {out_dir}. Preview only: no Sheet was read or written.")
    return 0 if summary["checks_passed"] else 1


def _count(rows: list[dict[str, str]], column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[column]] = counts.get(row[column], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


if __name__ == "__main__":
    raise SystemExit(main())
