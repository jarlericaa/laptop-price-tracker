#!/usr/bin/env python3
"""Publish one validated run to Google Sheets through publish_variation_prices.py, once, safely.

Steps: gate the run folder, re-run the publisher's own ``--preview`` against the live workbook and
require the same publication ID, skip if the live run log already shows that publication as
SUCCESS/PASSED, write once, reconcile and (for a partial write) roll back, then record the attempt
and keep the publisher's local backups in durable storage. Writes Sheets only with ``--write``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from laptop_tracker import ci_publish as cp
from laptop_tracker import run_gate
from laptop_tracker import state_store as ss


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually publish. Without it only the preview is re-validated.",
    )
    parser.add_argument("--accept-retailer-failures", action="store_true")
    parser.add_argument("--max-age-hours", type=float, default=run_gate.DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--max-matching-drop-pct", type=float, default=None)
    parser.add_argument("--no-auto-rollback", action="store_true")
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="Do not record the attempt in durable storage (local rehearsal).",
    )
    return parser


def publisher_inputs(run_dir: Path) -> list[str]:
    args = [
        "--variation-dir",
        str(run_dir / "shopee_variations"),
        "--reports-dir",
        str(run_dir / "variation_reports"),
        "--context-report-dir",
        str(run_dir / "context_reports"),
    ]
    collection = run_dir / "discovery" / "collection"
    if collection.is_dir():
        args += ["--collection-dir", str(collection)]
    return args


def main(
    argv: list[str] | None = None,
    *,
    backend: ss.Backend | None = None,
    publisher=None,
    open_spreadsheet=None,
    sleep=None,
) -> int:
    args = build_parser().parse_args(argv)
    run_dir = args.run_dir.resolve()
    run_id = run_dir.name
    if publisher is None:
        import publish_variation_prices as pvp

        publisher = pvp.main
        open_spreadsheet = open_spreadsheet or pvp.open_configured_spreadsheet
    from laptop_tracker.variation_publication import DATA_TABS

    try:
        store = None if args.no_state else (backend or ss.backend_from_env())
        previous = None
        if store is not None and args.max_matching_drop_pct is not None:
            found = store.get("publications/latest_success.json")
            previous = json.loads(found[0]).get("counts") if found else None
    except ss.StateError as exc:
        print(f"State error: {exc}", file=sys.stderr)
        return 1
    gate = run_gate.evaluate(
        run_dir,
        accept_retailer_failures=args.accept_retailer_failures,
        max_age_hours=args.max_age_hours,
        max_matching_drop_pct=args.max_matching_drop_pct,
        previous_counts=previous,
    )
    if not gate.ok:
        ss.write_step_summary(run_gate.render_summary("Publication blocked", gate))
        for line in gate.blocking:
            print(f"BLOCKED: {line}", file=sys.stderr)
        return 1
    publication_id = gate.metrics["publication_id"]

    # Re-validate against the live workbook just before writing: same inputs, same ID, no refusal.
    preview_root = run_dir / "publications_publish_job_preview"
    code = publisher(["--preview", *publisher_inputs(run_dir), "--output-root", str(preview_root)])
    preview = cp._load_report(cp._newest(preview_root, "preview"), "publication_report.json")
    if code != 0 or preview.get("status") != "PREVIEW_ONLY":
        print(
            f"BLOCKED: the live preview is {preview.get('status', 'unavailable')} "
            f"(exit {code}); nothing was written.",
            file=sys.stderr,
        )
        return 1
    if preview.get("publication_id") != publication_id:
        print(
            f"BLOCKED: the preview now builds {preview.get('publication_id')}, not the "
            f"validated {publication_id}.",
            file=sys.stderr,
        )
        return 1
    if not args.write:
        ss.write_step_summary(
            f"Live preview re-validated for `{publication_id}`; no write requested."
        )
        print("Preview only: nothing was written.")
        return 0

    started = datetime.now(UTC)
    outcome = cp.publish_run(
        publication_id=publication_id,
        publisher_args=publisher_inputs(run_dir),
        output_root=run_dir / "publications",
        run_dir=run_dir,
        open_spreadsheet=open_spreadsheet,
        publisher_main=publisher,
        data_tabs=DATA_TABS,
        auto_rollback=not args.no_auto_rollback,
        **({"sleep": sleep} if sleep else {}),
    )
    record = {
        "status": outcome.classification,
        "run_id": run_id,
        "started_at_utc": started.isoformat(timespec="seconds"),
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        **outcome.as_dict(),
    }
    if store is not None:
        try:
            ss.append_publication_attempt(store, publication_id, record)
            if outcome.write_dir:
                # The publisher's snapshots of the previous tabs: durable recovery data.
                stamp = Path(outcome.write_dir).name
                ss.upload_tree(
                    store, Path(outcome.write_dir), f"publications/{publication_id}/{stamp}"
                )
            if outcome.ok:
                if outcome.classification != cp.ALREADY_PUBLISHED:
                    store.put(
                        "publications/latest_success.json",
                        json.dumps(
                            {
                                "publication_id": publication_id,
                                "run_id": run_id,
                                "published_at_utc": record["finished_at_utc"],
                                "counts": gate.metrics.get("counts", {}),
                            },
                            indent=2,
                        ).encode(),
                    )
                ss.update_run_manifest(
                    store,
                    run_id,
                    publication_id=publication_id,
                    publication_status=outcome.classification,
                )
        except ss.StateError as exc:
            print(
                f"WARNING: could not record the publication in durable storage: {exc}",
                file=sys.stderr,
            )
            if outcome.ok:
                ss.write_step_summary(f"**WARNING:** published but not recorded durably: {exc}")
                return 1
    extra = {
        "Publication status": outcome.classification,
        "Publisher status": outcome.publisher_status or "n/a",
        "Readback issues": outcome.readback_issues,
        "Live run-log row": json.dumps(outcome.live_row) if outcome.live_row else "n/a",
    }
    if outcome.rollback:
        extra["Rollback"] = json.dumps(outcome.rollback)
    ss.write_step_summary(
        run_gate.render_summary("Publication result", gate, extra)
        + (f"\n{outcome.note}\n" if outcome.note else "")
    )
    print(f"Publication outcome: {outcome.classification}. {outcome.note}")
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
