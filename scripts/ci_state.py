#!/usr/bin/env python3
"""Durable state for automated runs, stored as encrypted assets of private GitHub Releases.

Commands: keygen, seed, restore, save, versions, verify, rollback, report, publication-fetch,
bundle-save, bundle-load.
GitHub access uses GITHUB_TOKEN (Actions) or your 'gh auth login' (laptop); archives are encrypted
with the key in STATE_ENCRYPTION_KEY. STATE_LOCAL_DIR switches to a local folder for a rehearsal.
Nothing here touches Google Sheets or a retailer. See docs/GITHUB_ACTIONS_SETUP.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker import release_store as rs
from laptop_tracker import state_crypto as sc
from laptop_tracker import state_store as ss

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root (default).")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="Create a new encryption key file (never printed).")
    keygen.add_argument("--out", type=Path, required=True, help="New file; must not exist.")
    seed = sub.add_parser("seed", help="Create the FIRST state version from local files (once).")
    seed.add_argument("--run-id", default="seed")
    restore = sub.add_parser("restore", help="Restore the latest valid state into the workspace.")
    restore.add_argument("--version", help="Restore this version (number or tag), not the latest.")
    restore.add_argument("--overwrite", action="store_true", help="Replace existing local files.")
    save = sub.add_parser("save", help="Validate and save the post-run state as a new version.")
    save.add_argument("--run-id", required=True)
    save.add_argument("--note", default="")
    sub.add_parser("versions", help="List published state versions (oldest first).")
    verify = sub.add_parser("verify", help="Download, decrypt and validate a version; no restore.")
    verify.add_argument("--version", help="Version to check (default: the latest).")
    rollback = sub.add_parser("rollback", help="Publish a new version with an earlier one's data.")
    rollback.add_argument("--to", required=True, help="Version number or tag to return to.")
    report = sub.add_parser("report", help="Storage size, growth and unfinished drafts.")
    report.add_argument("--warn-mb", type=int, default=ss.DEFAULT_WARN_MB)
    pfetch = sub.add_parser(
        "publication-fetch", help="Unpack a recorded publication attempt and its tab snapshots."
    )
    pfetch.add_argument("--publication-id", required=True)
    pfetch.add_argument("--attempt", type=int, help="Attempt number (default: the latest).")
    pfetch.add_argument("--dest", type=Path, required=True)
    bsave = sub.add_parser("bundle-save", help="Store a validated run's bundle (encrypted).")
    bsave.add_argument("--run-dir", type=Path, required=True)
    bsave.add_argument("--record", type=Path, help="JSON file merged into the run manifest.")
    bload = sub.add_parser("bundle-load", help="Download a saved run bundle into the workspace.")
    bload.add_argument("--run-id", required=True)
    bload.add_argument("--dest", type=Path, required=True)
    return parser


def render_report(report: dict) -> str:
    lines = ["### Stored tracker data (private GitHub Releases)"]
    for kind, group in report["groups"].items():
        lines.append(
            f"- {kind}: {group['releases']} release(s), {group['bytes'] / 1048576:.1f} MB "
            f"(average {group['average_bytes'] / 1048576:.1f} MB)"
        )
    lines.append(f"- total: {report['total_bytes'] / 1048576:.1f} MB")
    lines += [f"- WARNING: {w}" for w in report["warnings"]]
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    store: rs.ReleaseStore | None = None,
    ring: sc.KeyRing | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "keygen":
            try:
                key_id = sc.write_key_file(args.out)
            except sc.CryptoError as exc:
                raise ss.StateError(str(exc)) from exc
            print(
                json.dumps(
                    {
                        "key_file": str(args.out),
                        "key_id": key_id,
                        "next": "Store it as the GitHub Secret STATE_ENCRYPTION_KEY and back it up "
                        "(see docs/GITHUB_ACTIONS_SETUP.md). The key itself was not printed.",
                    },
                    indent=2,
                )
            )
            return 0
        if store is None or ring is None:
            default_store, default_ring = ss.store_and_keys()
            store, ring = store or default_store, ring or default_ring
        if args.command == "seed":
            result = ss.seed(store, ring, args.root, run_id=args.run_id)
        elif args.command == "restore":
            result = ss.restore(
                store, ring, args.root, version=args.version, overwrite=args.overwrite
            )
        elif args.command == "save":
            result = ss.save(store, ring, args.root, run_id=args.run_id, note=args.note)
        elif args.command == "versions":
            result = {"versions": ss.list_versions(store)}
        elif args.command == "verify":
            result = ss.verify_version(store, ring, args.version)
        elif args.command == "rollback":
            result = ss.rollback(store, ring, args.to)
        elif args.command == "report":
            result = ss.storage_report(store, warn_mb=args.warn_mb)
            ss.write_step_summary(render_report(result))
        elif args.command == "publication-fetch":
            result = ss.fetch_publication_attempt(
                store, ring, args.publication_id, args.dest, args.attempt
            )
        elif args.command == "bundle-save":
            record = json.loads(args.record.read_text()) if args.record else {}
            result = ss.save_bundle(store, ring, args.run_dir, args.run_dir.resolve().name, record)
        else:  # bundle-load
            result = ss.load_bundle(store, ring, args.run_id, args.dest)
    except ss.StateError as exc:
        print(f"State error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
