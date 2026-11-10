"""Which mode a GitHub Actions run is in, and whether its configuration is complete.

Kept in Python (not in workflow expressions) so the decision is unit-tested. Only names of
missing settings are ever reported, never values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Repository Variables / Secrets the workflow passes to the pipeline as environment variables.
SHEETS_SETTINGS = ("SPREADSHEET_ID", "GOOGLE_USE_ADC")
STATE_SETTINGS = ("GCS_STATE_BUCKET",)
AUTH_SETTINGS = ("GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_SERVICE_ACCOUNT")

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


def missing_settings(
    env: Mapping[str, str], *, sheets: bool = True, state: bool = True, auth: bool = False
) -> list[str]:
    """Names of required settings that are empty. ``GOOGLE_USE_ADC`` must be true."""
    needed: list[str] = []
    if sheets:
        needed += list(SHEETS_SETTINGS)
    if state:
        needed += list(STATE_SETTINGS)
    if auth:
        needed += list(AUTH_SETTINGS)
    missing = [name for name in needed if not (env.get(name) or "").strip()]
    if sheets and "GOOGLE_USE_ADC" not in missing and not _truthy(env.get("GOOGLE_USE_ADC")):
        missing.append("GOOGLE_USE_ADC (must be true)")
    return missing
