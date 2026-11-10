"""Which mode a GitHub Actions run is in, and whether its configuration is complete.

Kept in Python (not in workflow expressions) so the decision is unit-tested. Only names of
missing settings are ever reported, never values.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass

# Repository Variables / Secrets the workflow passes to the pipeline as environment variables.
SHEETS_SETTINGS = ("SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")
STATE_SETTINGS = ("STATE_ENCRYPTION_KEY",)

AUTOMATION_OFF = "off"
AUTOMATION_PREVIEW = "preview"
AUTOMATION_PUBLISH = "publish"


@dataclass(frozen=True)
class RunPlan:
    skip: bool
    publish: bool
    collect: bool
    existing_run_id: str
    accept_retailer_failures: bool
    reason: str


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def decide_mode(event: str, inputs: Mapping[str, str], variables: Mapping[str, str]) -> RunPlan:
    """Mode A (manual preview), B (manual production), C (scheduled).

    * ``workflow_dispatch``: preview unless ``publish`` is explicitly true. ``existing_run_id``
      publishes an already collected, validated run without scraping again (it requires
      ``publish``).
    * ``schedule``: governed by the repository variable ``WEEKLY_AUTOMATION``: unset/``off`` skips
      the run entirely, ``preview`` collects and validates without writing, ``publish`` is the
      full unattended production run.
    * Anything else (for example a pull request) never runs the pipeline.
    """
    if event == "workflow_dispatch":
        publish = _truthy(inputs.get("publish"))
        existing = (inputs.get("existing_run_id") or "").strip()
        if existing and not publish:
            return RunPlan(
                True,
                False,
                False,
                existing,
                False,
                "existing_run_id was given without publish=true: nothing to do.",
            )
        accept = _truthy(inputs.get("accept_retailer_failures"))
        mode = "manual production run" if publish else "manual preview (no Sheets write)"
        if existing:
            mode += f", publishing the saved run {existing} without collecting again"
        return RunPlan(False, publish, not existing, existing, accept, mode)
    if event == "schedule":
        level = (variables.get("WEEKLY_AUTOMATION") or AUTOMATION_OFF).strip().lower()
        if level == AUTOMATION_PUBLISH:
            return RunPlan(False, True, True, "", False, "scheduled production run")
        if level == AUTOMATION_PREVIEW:
            return RunPlan(False, False, True, "", False, "scheduled preview run (no write)")
        return RunPlan(
            True,
            False,
            False,
            "",
            False,
            "WEEKLY_AUTOMATION is not 'preview' or 'publish': scheduled run skipped.",
        )
    return RunPlan(True, False, False, "", False, f"event {event!r} does not run the pipeline.")


def google_credentials_problem(value: str) -> tuple[str | None, str]:
    """Check the service-account JSON Secret without ever echoing it. Returns (problem, email);
    the service account's e-mail address is not a secret and helps the owner confirm which
    account must be shared on the workbook."""
    try:
        info = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (paste the whole key file).", ""
    if not isinstance(info, dict) or info.get("type") != "service_account":
        return "GOOGLE_SERVICE_ACCOUNT_JSON is not a service-account key file.", ""
    absent = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
    if absent:
        return f"GOOGLE_SERVICE_ACCOUNT_JSON lacks: {', '.join(absent)}.", ""
    return None, str(info["client_email"])


def encryption_key_problem(value: str) -> str | None:
    """STATE_ENCRYPTION_KEY must be 32 random bytes, base64url encoded."""
    text = (value or "").strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return "STATE_ENCRYPTION_KEY is not base64url text."
    if len(raw) != 32:
        return "STATE_ENCRYPTION_KEY must decode to 32 bytes (use 'ci_state.py keygen')."
    return None


def missing_settings(
    env: Mapping[str, str], *, sheets: bool = True, state: bool = True
) -> list[str]:
    """Names of required settings that are empty (never their values)."""
    needed: list[str] = []
    if sheets:
        needed += list(SHEETS_SETTINGS)
    if state:
        needed += list(STATE_SETTINGS)
    return [name for name in needed if not (env.get(name) or "").strip()]
