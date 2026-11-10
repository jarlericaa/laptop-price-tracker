#!/usr/bin/env python3
"""GitHub Actions helper: decide the run mode and check required configuration.

``plan`` reads EVENT_NAME, INPUT_* and VAR_WEEKLY_AUTOMATION from the environment and writes the
decision to $GITHUB_OUTPUT. ``check`` verifies that the required Variables are present and prints
only the NAMES of missing ones. Standard library only: no dependency install is needed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Loaded by path: importing the laptop_tracker package would import pandas, which the plan job
# deliberately does not install.
_path = Path(__file__).resolve().parents[1] / "src" / "laptop_tracker" / "ci_config.py"
_spec = importlib.util.spec_from_file_location("ci_config", _path)
ci_config = importlib.util.module_from_spec(_spec)
sys.modules["ci_config"] = ci_config
_spec.loader.exec_module(ci_config)
decide_mode, missing_settings = ci_config.decide_mode, ci_config.missing_settings


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    check = sub.add_parser("check")
    check.add_argument("--no-sheets", action="store_true")
    check.add_argument("--no-state", action="store_true")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    if args.command == "plan":
        plan = decide_mode(
            env.get("EVENT_NAME", ""),
            {
                "publish": env.get("INPUT_PUBLISH", ""),
                "existing_run_id": env.get("INPUT_EXISTING_RUN_ID", ""),
                "accept_retailer_failures": env.get("INPUT_ACCEPT_RETAILER_FAILURES", ""),
            },
            {"WEEKLY_AUTOMATION": env.get("VAR_WEEKLY_AUTOMATION", "")},
        )
        lines = {
            "skip": str(plan.skip).lower(),
            "publish": str(plan.publish).lower(),
            "collect": str(plan.collect).lower(),
            "existing_run_id": plan.existing_run_id,
            "accept_partial": str(plan.accept_retailer_failures).lower(),
        }
        target = env.get("GITHUB_OUTPUT")
        text = "".join(f"{k}={v}\n" for k, v in lines.items())
        if target:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(text)
        print(f"Mode: {plan.reason}")
        print(text, end="")
        summary = env.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"**Mode:** {plan.reason}\n")
        return 0

    missing = missing_settings(env, sheets=not args.no_sheets, state=not args.no_state)
    if missing:
        print(
            "Missing or empty repository Variables/Secrets: " + ", ".join(missing), file=sys.stderr
        )
        print("See docs/GITHUB_ACTIONS_SETUP.md, section 'Variables and Secrets'.", file=sys.stderr)
        return 1
    problems = []
    if not args.no_sheets:
        problem, email = ci_config.google_credentials_problem(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
        if problem:
            problems.append(problem)
        else:
            print(f"Google service account: {email} (this account must be Editor on the workbook).")
    if not args.no_state:
        key_problem = ci_config.encryption_key_problem(env["STATE_ENCRYPTION_KEY"])
        if key_problem:
            problems.append(key_problem)
    if problems:
        print("Invalid configuration: " + " ".join(problems), file=sys.stderr)
        return 1
    print("Required configuration is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
