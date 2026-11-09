#!/usr/bin/env python3
"""Compare two runs' own artifacts: variation selection, coverage, discovery and request reuse.

Reads ``shopee_variations/shopee_variation_run_summary.json``, ``variation_reports/
variation_coverage.json``, ``discovery/discovery_summary.json`` and ``discovery/search_jobs.csv`` of
a *before* and an *after* run folder (read only) and writes ``before_after_report.json`` and
``before_after_report.md`` into ``--output-dir`` (under ``data/output/``, never ``config/``). A
figure a run did not record is ``NOT_RECORDED``, never zero. Contacts no retailer and no Sheet.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from laptop_tracker.before_after import build_report, load_run, render_markdown  # noqa: E402
from laptop_tracker.catalog_proposals import OutputLocationError, check_output_dir  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before-dir", type=Path, required=True, help="Earlier run folder.")
    parser.add_argument("--after-dir", type=Path, required=True, help="Later run folder.")
    parser.add_argument("--before-label", help="Default: the folder name.")
    parser.add_argument("--after-label", help="Default: the folder name.")
    parser.add_argument("--output-dir", type=Path, help="Default: <after-dir>/before_after.")
    parser.add_argument("--dry-run", action="store_true", help="Print the report; write nothing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for folder in (args.before_dir, args.after_dir):
        if not folder.is_dir():
            print(f"Run folder not found: {folder}", file=sys.stderr)
            return 2
    before = load_run(args.before_label or args.before_dir.name, args.before_dir)
    after = load_run(args.after_label or args.after_dir.name, args.after_dir)
    report = build_report(before, after)
    text = render_markdown(report)
    if args.dry_run:
        print(text)
        return 0
    try:
        out = check_output_dir(args.output_dir or args.after_dir / "before_after", ROOT)
    except OutputLocationError as error:
        print(error, file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / "before_after_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out / "before_after_report.md").write_text(text, encoding="utf-8")
    print(f"Wrote {out / 'before_after_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
