#!/usr/bin/env python3
"""Durable state for automated runs: seed, restore, save, lock and run bundles.

Storage is Google Cloud Storage (``GCS_STATE_BUCKET``, optional ``GCS_STATE_PREFIX``) through
Application Default Credentials, or a local folder (``STATE_LOCAL_DIR``) for a rehearsal. Nothing
here touches Google Sheets or a retailer. See docs/GITHUB_ACTIONS_SETUP.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker import state_store as ss

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root (default).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Create the FIRST state version from local files (once).")
    restore = sub.add_parser("restore", help="Restore the latest valid state into the workspace.")
    restore.add_argument("--version", help="Restore this version instead of the latest.")
    restore.add_argument("--overwrite", action="store_true", help="Replace existing local files.")
    save = sub.add_parser("save", help="Validate and save the post-run state as a new version.")
    save.add_argument("--run-id", required=True)
    save.add_argument("--note", default="")
    sub.add_parser("versions", help="List saved state versions (newest last).")
    promote = sub.add_parser("promote", help="Make an earlier state version the latest again.")
    promote.add_argument("--version", required=True)
    lock = sub.add_parser("lock", help="Take the state lock.")
    lock.add_argument("--owner", required=True)
    lock.add_argument("--ttl-minutes", type=int, default=360)
    unlock = sub.add_parser("unlock", help="Release the state lock.")
    unlock.add_argument("--owner", default="")
    unlock.add_argument("--force", action="store_true", help="Break a lock held by anyone.")
    bsave = sub.add_parser("bundle-save", help="Upload a validated run's bundle.")
    bsave.add_argument("--run-dir", type=Path, required=True)
    bsave.add_argument("--record", type=Path, help="JSON file merged into the run manifest.")
    bload = sub.add_parser("bundle-load", help="Download a saved run bundle into the workspace.")
    bload.add_argument("--run-id", required=True)
    bload.add_argument("--dest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, *, backend: ss.Backend | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backend = backend or ss.backend_from_env()
        if args.command == "seed":
            result = {"version_id": ss.seed(backend, args.root)}
        elif args.command == "restore":
            result = ss.restore(
                backend, args.root, version_id=args.version, overwrite=args.overwrite
            )
        elif args.command == "save":
            result = ss.save(backend, args.root, run_id=args.run_id, note=args.note)
        elif args.command == "versions":
            result = {"versions": ss.list_versions(backend)}
        elif args.command == "promote":
            result = ss.promote(backend, args.version)
        elif args.command == "lock":
            result = {
                "generation": ss.acquire_lock(backend, args.owner, ttl_minutes=args.ttl_minutes)
            }
        elif args.command == "unlock":
            if args.force:
                backend.delete(ss.LOCK)
                result = {"released": True, "forced": True}
            else:
                result = {"released": ss.release_lock(backend, args.owner)}
        elif args.command == "bundle-save":
            record = json.loads(args.record.read_text()) if args.record else {}
            run_id = args.run_dir.resolve().name
            result = ss.save_bundle(backend, args.run_dir, run_id, record)
        else:  # bundle-load
            result = ss.load_bundle(backend, args.run_id, args.dest)
    except ss.StateError as exc:
        print(f"State error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
