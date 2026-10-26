#!/usr/bin/env python3
"""Propose evidence-backed catalog corrections (local, read-only; nothing is applied).

Reads a validated TARGET_MASTER snapshot, one run's report tables and the saved collection
evidence, and writes ``catalog_proposals.csv``, ``catalog_proposals.json`` and
``REVIEW_README.md`` under ``data/output/``. Never contacts a retailer, never reads or writes
Google Sheets, and never changes the catalog, target IDs, mappings, approvals or history. The
output folder is refused unless it is under ``data/output/`` (and never ``config/``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from laptop_tracker.catalog_proposals import (  # noqa: E402
    OutputLocationError,
    build_proposals,
    check_output_dir,
    load_context,
    load_offer_evidence,
    sha256_file,
    write_outputs,
)

DEFAULT_OUTPUT = ROOT / "data" / "output" / "catalog_proposals_2026-10-22"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-master", type=Path, required=True,
                        help="Validated TARGET_MASTER snapshot (target_master_validated.csv).")
    parser.add_argument("--run-dir", type=Path,
                        help="A top100_* run folder: inputs/ and reports/ tables.")
    parser.add_argument("--selected-list", type=Path)
    parser.add_argument("--contributions", type=Path)
    parser.add_argument("--source-model-map", type=Path)
    parser.add_argument("--collection-dir", type=Path, action="append", default=[],
                        help="Saved collection folder (observations.csv, match_results.csv, "
                             "match_evidence.csv); repeatable. Defaults to <run-dir>/discovery/"
                             "collection.")
    parser.add_argument("--collection-glob", action="append", default=[],
                        help="Glob (relative to the repository) of saved collection folders, "
                             "e.g. 'data/output/top100_*/discovery/collection'; repeatable. "
                             "A page seen in several folders keeps its latest retrieval.")
    parser.add_argument("--screen-brands", default="HP",
                        help="Comma-separated brands whose recorded screen class is compared "
                             "with page diagonals ('*' for every brand). MSI is always checked.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        out_dir = check_output_dir(args.output_dir, ROOT)
    except OutputLocationError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    collections = list(args.collection_dir)
    for pattern in args.collection_glob:
        collections += sorted(p for p in ROOT.glob(pattern) if p.is_dir())
    if not collections and args.run_dir:
        collections = [args.run_dir / "discovery" / "collection"]
    try:
        context = load_context(
            args.target_master, run_dir=args.run_dir, selected_list=args.selected_list,
            contributions=args.contributions, source_model_map=args.source_model_map)
        offers = load_offer_evidence(collections)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Catalog proposals stopped: {exc}", file=sys.stderr)
        return 1
    proposals = build_proposals(
        context, offers,
        screen_brands=[b.strip() for b in args.screen_brands.split(",") if b.strip()])
    inputs = {**context.inputs, **{f"collection_{i}": str(p) for i, p in enumerate(collections)}}
    hashes = {name: sha256_file(Path(path)) for name, path in inputs.items()
              if Path(path).is_file()}
    summary = write_outputs(out_dir, proposals, inputs, hashes)
    print(json.dumps({"output_dir": str(out_dir), "offers_read": len(offers), **summary},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
