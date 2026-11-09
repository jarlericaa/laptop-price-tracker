#!/usr/bin/env python3
"""Audit catalog bindings and write the owner's correction queue (local, read-only on inputs).

Reads the validated catalog copies and the Shopee variation ranking of one run and writes
``catalog_correction_queue.csv/.json/.md`` into ``--output-dir`` (under ``data/output/``, never
``config/``). Never contacts a retailer, never reads or writes Google Sheets, and never changes a
catalog table, mapping, approval or history file: every row is a proposal for owner review with
``applies_automatically`` FALSE. ``--dry-run`` prints the counts and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from laptop_tracker.catalog_corrections import (  # noqa: E402
    build_correction_queue,
    write_queue_outputs,
)
from laptop_tracker.catalog_proposals import (  # noqa: E402
    OutputLocationError,
    check_output_dir,
    read_csv_rows,
    sha256_file,
)

DEFAULT_RUN = ROOT / "data" / "output" / "union_2026-11-03"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--variation-ranking", type=Path)
    parser.add_argument("--target-master", type=Path)
    parser.add_argument("--source-model-map", type=Path)
    parser.add_argument("--match-review", type=Path)
    parser.add_argument("--output-dir", type=Path, help="Default: <run-dir>/catalog_corrections.")
    parser.add_argument("--dry-run", action="store_true", help="Print the counts; write nothing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run = args.run_dir
    paths = {
        "variation_ranking": args.variation_ranking
        or run / "shopee_variations" / "shopee_variation_ranking.csv",
        "target_master": args.target_master or run / "inputs" / "target_master_validated.csv",
        "source_model_map": args.source_model_map
        or run / "inputs" / "source_model_map_validated.csv",
        "match_review": args.match_review or run / "inputs" / "match_review_validated.csv",
    }
    try:
        out_dir = check_output_dir(args.output_dir or run / "catalog_corrections", ROOT)
        queue = build_correction_queue(
            read_csv_rows(paths["variation_ranking"]),
            read_csv_rows(paths["target_master"]),
            read_csv_rows(paths["source_model_map"]),
            read_csv_rows(paths["match_review"]),
        )
    except (OutputLocationError, FileNotFoundError, KeyError) as exc:
        print(f"Catalog correction queue stopped: {exc}", file=sys.stderr)
        return 1
    summary = queue.summary
    print(
        f"Catalog correction queue: {summary['queue_rows']} rows "
        f"({summary['flagged_variation_bindings']} of "
        f"{summary['mapped_variations_in_pool']} mapped variations flagged; "
        f"{summary['flagged_selected_top_variations']} in the selected Top 100)."
    )
    for name, count in summary["rows_by_scope_and_primary_class"].items():  # type: ignore[union-attr]
        print(f"  {name}: {count}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    write_queue_outputs(
        out_dir,
        queue,
        {name: str(path) for name, path in paths.items()},
        {name: sha256_file(path) for name, path in paths.items()},
    )
    print(f"Written to {out_dir}. Nothing was applied; the catalog and Sheets were not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
