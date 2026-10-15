#!/usr/bin/env python3
"""Offline candidate discovery and search-query planning for reference configurations."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.collection import ROUTES
from laptop_tracker.discovery import (
    CANDIDATE_COLUMNS,
    QUERY_COLUMNS,
    Reference,
    assessment_row,
    build_candidates,
    plan_queries,
    rank_candidates,
)
from laptop_tracker.match_run import ObservationFileError, read_observations_csv
from laptop_tracker.normalization import CORE_MATCH_COLUMNS
from laptop_tracker.verified_top import sha256_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank saved retailer observations as research candidates for reference "
            "configurations and plan search queries. Offline: makes no requests, runs no "
            "searches, and never changes matching, mappings, the catalog, or Google Sheets. "
            "Categories are review priorities, not target assignments."
        )
    )
    parser.add_argument(
        "--references",
        type=Path,
        required=True,
        help="Reference configurations CSV (top_100_verified_configurations.csv layout).",
    )
    parser.add_argument(
        "--target-master",
        type=Path,
        help="Validated TARGET_MASTER snapshot; supplies verified manufacturer part numbers.",
    )
    parser.add_argument(
        "--targets",
        default="",
        help="Comma-separated target IDs to include (default: every reference row).",
    )
    parser.add_argument(
        "--scope-file",
        type=Path,
        help="Optional CSV with a target_id column (for example pilot_scope.csv).",
    )
    parser.add_argument(
        "--observations",
        type=Path,
        action="append",
        default=[],
        help="Saved observation CSV; repeat for several. Later files win for the same record.",
    )
    parser.add_argument(
        "--retailers",
        default=",".join(ROUTES),
        help="Comma-separated retailer IDs to plan queries for (default: all with adapters).",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder.")
    return parser.parse_args(argv)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_references(args: argparse.Namespace) -> list[Reference]:
    wanted = [t.strip() for t in args.targets.split(",") if t.strip()]
    if args.scope_file:
        wanted += [row["target_id"].strip() for row in read_csv(args.scope_file)]
    part_numbers: dict[str, str] = {}
    if args.target_master:
        for row in read_csv(args.target_master):
            part_numbers[row.get("target_id", "")] = row.get("manufacturer_part_number", "")
    rows = read_csv(args.references)
    missing = [c for c in ("target_id", "brand", "model", *CORE_MATCH_COLUMNS) if c not in rows[0]]
    if missing:
        raise ValueError(f"Reference file is missing columns: {', '.join(missing)}")
    by_id = {row["target_id"]: row for row in rows}
    unknown = [t for t in dict.fromkeys(wanted) if t not in by_id]
    if unknown:
        raise ValueError(f"Target IDs not in the reference file: {', '.join(unknown)}")
    chosen = [by_id[t] for t in dict.fromkeys(wanted)] if wanted else rows
    return [
        Reference(
            target_id=row["target_id"],
            rank=row.get("rank", ""),
            brand=row["brand"],
            series=row.get("series", ""),
            model=row["model"],
            core={name: row[name] for name in CORE_MATCH_COLUMNS},
            manufacturer_part_number=part_numbers.get(row["target_id"], "").strip(),
        )
        for row in chosen
    ]


def write_rows(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    retailers = [r.strip() for r in args.retailers.split(",") if r.strip()]
    unknown = [r for r in retailers if r not in ROUTES]
    if unknown:
        print(f"Discovery stopped: no adapter for {', '.join(unknown)}.", file=sys.stderr)
        return 1
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"Discovery stopped: {args.output_dir} already exists.", file=sys.stderr)
        return 1
    try:
        references = load_references(args)
        saved = [
            (item.observation, str(path))
            for path in args.observations
            for item in read_observations_csv(path)
        ]
    except (FileNotFoundError, KeyError, IndexError, ValueError, ObservationFileError) as exc:
        print(f"Discovery stopped: {exc}", file=sys.stderr)
        return 1

    candidates = [c for c in build_candidates(saved) if c.retailer_id in retailers]
    assessments = rank_candidates(references, candidates)
    hosts = {r: sorted(ROUTES[r].adapter_factory().allowed_hosts, key=len)[0] for r in retailers}
    queries = plan_queries(references, assessments, hosts)
    write_rows(args.output_dir / "discovery_candidates.csv", CANDIDATE_COLUMNS,
               (assessment_row(item) for item in assessments))
    write_rows(args.output_dir / "discovery_queries.csv", QUERY_COLUMNS, queries)
    summary = {
        "description": (
            "Offline discovery: research priorities only. No requests, searches, matching "
            "changes, mappings, catalog edits, or Sheets writes."
        ),
        "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "input_sha256": {
            str(p): sha256_file(p)
            for p in (args.references, args.target_master, args.scope_file, *args.observations)
            if p
        },
        "references": len(references),
        "saved_candidates": sum(c.collection_status == "COLLECTED" for c in candidates),
        "discovered_not_fetched": sum(c.collection_status != "COLLECTED" for c in candidates),
        "categories": dict(Counter(item.category for item in assessments)),
        "queries": len(queries),
    }
    (args.output_dir / "discovery_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"References: {len(references)}; saved candidates: {summary['saved_candidates']}; "
          f"discovered, not fetched: {summary['discovered_not_fetched']}.")
    print(f"Candidate categories: {summary['categories']}. Planned queries: {len(queries)}.")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
