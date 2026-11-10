"""Safe, idempotent unattended publication around the existing Sheets publisher.

This module adds no second publisher. It calls ``scripts/publish_variation_prices.py`` (its
``--write-sheets`` mode) and wraps it with the decisions an unattended run needs:

* **Before**: the live run log (``PILOT_PRICE_RUN_LOG``, read-only) says whether this exact
  publication ID is already ``SUCCESS``/``PASSED``. If so nothing is written.
* **One write attempt per invocation.** A write is never retried blindly: the publisher replaces
  whole tabs (so a later, deliberate rerun is safe) but an automatic second attempt after an
  unexplained failure could hide the cause.
* **After**: a failure is classified from the publisher's own local report and the live run-log
  row, never from console text:

  ===================== ==================================================================
  ``FAILED_BEFORE_WRITE``  access or destination refused, inputs invalid: no tab was touched
  ``SUCCEEDED_UNREPORTED`` data written and verified, but the final run-log read failed
                           (a known Sheets read-quota 429); the live row says SUCCESS/PASSED
  ``PARTIAL_WRITE``        some tabs written, or readback found differences
  ===================== ==================================================================

  A partial write is rolled back from the publisher's own snapshots (its ``--rollback``), so the
  dashboard never keeps a half-written publication.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

PUBLISHED = "PUBLISHED"
ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
SUCCEEDED_UNREPORTED = "SUCCEEDED_UNREPORTED"
FAILED_BEFORE_WRITE = "FAILED_BEFORE_WRITE"
PARTIAL_WRITE = "PARTIAL_WRITE"
GOOD = {PUBLISHED, ALREADY_PUBLISHED, SUCCEEDED_UNREPORTED}
RETRY_DELAYS = (20, 30, 45, 65)


@dataclass
class LiveLogRow:
    status: str = ""
    readback: str = ""
    attempts: str = ""
    tabs_written: str = ""


@dataclass
class PublishOutcome:
    classification: str
    publication_id: str
    publisher_exit_code: int | None = None
    publisher_status: str = ""
    live_row: dict = field(default_factory=dict)
    tabs_written: list[str] = field(default_factory=list)
    readback_issues: int = 0
    write_dir: str = ""
    rollback: dict = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.classification in GOOD

    def as_dict(self) -> dict:
        return asdict(self)


def _cell(header: Sequence[str], row: Sequence[str], name: str) -> str:
    index = header.index(name) if name in header else -1
    return row[index] if 0 <= index < len(row) else ""


def read_live_log_row(
    spreadsheet, publication_id: str, *, sleep: Callable[[float], None] = time.sleep
) -> LiveLogRow | None:
    """The run-log row of ``publication_id`` as the live Sheet shows it now (read-only; waits out
    a per-minute read quota instead of failing on it)."""
    from .variation_publication import RUN_LOG_TAB

    last: Exception | None = None
    for delay in (*RETRY_DELAYS, None):
        try:
            values = spreadsheet.worksheet(RUN_LOG_TAB).get_all_values()
            break
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if delay is None or not ("429" in str(exc) or "Quota exceeded" in str(exc)):
                raise
            sleep(delay)
    else:  # pragma: no cover
        raise last  # type: ignore[misc]
    if not values:
        return None
    header = values[0]

    def col(name: str) -> int:
        return header.index(name) if name in header else -1

    id_col = col("Publication ID")
    if id_col < 0:
        return None
    for row in values[1:]:
        if len(row) > id_col and row[id_col] == publication_id:
            return LiveLogRow(
                _cell(header, row, "Publication status"),
                _cell(header, row, "Readback"),
                _cell(header, row, "Publication attempts"),
                _cell(header, row, "Tabs written"),
            )
    return None


def _newest(output_root: Path, suffix: str) -> Path | None:
    folders = sorted(p for p in output_root.glob(f"*_{suffix}") if p.is_dir())
    return folders[-1] if folders else None


def _load_report(folder: Path | None, name: str) -> dict:
    if folder is None:
        return {}
    try:
        return json.loads((folder / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def classify_failure(report: dict, live: LiveLogRow | None, data_tabs: Sequence[str]) -> str:
    status = report.get("status", "")
    written = report.get("tabs_written") or []
    issues = report.get("readback_issues") or []
    if (
        live is not None
        and live.status == "SUCCESS"
        and live.readback == "PASSED"
        and not issues
        and set(written) >= set(data_tabs)
    ):
        return SUCCEEDED_UNREPORTED
    if status in {"", "ACCESS_FAILED", "DESTINATION_REFUSED"} and not written:
        if live is None or live.status not in {"IN_PROGRESS", "PARTIAL_FAILURE"}:
            return FAILED_BEFORE_WRITE
    return PARTIAL_WRITE


def publish_run(
    *,
    publication_id: str,
    publisher_args: Sequence[str],
    output_root: Path,
    run_dir: Path,
    open_spreadsheet: Callable[[bool], object],
    publisher_main: Callable[..., int],
    data_tabs: Sequence[str],
    auto_rollback: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> PublishOutcome:
    """Publish one run once, safely. ``publisher_args`` are the arguments after ``--write-sheets``
    of ``publish_variation_prices.py`` (input folders); ``publisher_main`` is its ``main``."""
    # 1. Already published? Only a SUCCESS row whose readback PASSED counts.
    try:
        before = read_live_log_row(open_spreadsheet(True), publication_id, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        return PublishOutcome(
            FAILED_BEFORE_WRITE,
            publication_id,
            note=f"Could not read the live run log ({type(exc).__name__}); nothing was written.",
        )
    if before is not None and before.status == "SUCCESS" and before.readback == "PASSED":
        log(f"{publication_id} is already published (readback PASSED); nothing to do.")
        return PublishOutcome(
            ALREADY_PUBLISHED,
            publication_id,
            live_row=asdict(before),
            note="The live run log already shows this publication as SUCCESS.",
        )
    if before is not None:
        log(
            f"Earlier attempt of {publication_id} is on record ({before.status}); the publisher "
            "replaces its tabs as a whole, so one deliberate rerun is safe."
        )

    # 2. One write attempt through the existing publisher.
    code = publisher_main(["--write-sheets", *publisher_args, "--output-root", str(output_root)])
    write_dir = _newest(output_root, "write")
    report = _load_report(write_dir, "publication_report.json")
    outcome = PublishOutcome(
        PUBLISHED,
        publication_id,
        publisher_exit_code=code,
        publisher_status=report.get("status", ""),
        tabs_written=list(report.get("tabs_written") or []),
        readback_issues=len(report.get("readback_issues") or []),
        write_dir=str(write_dir) if write_dir else "",
    )
    if report.get("publication_id") not in (None, publication_id):
        outcome.classification = PARTIAL_WRITE
        outcome.note = "The publisher built a different publication ID than the validated run."
        return outcome
    if code == 0 and report.get("status") == "SUCCESS":
        return outcome

    # 3. Reconcile from the live run log, then classify.
    live: LiveLogRow | None = None
    try:
        live = read_live_log_row(open_spreadsheet(True), publication_id, sleep=sleep)
        outcome.live_row = asdict(live) if live else {}
    except Exception as exc:  # noqa: BLE001
        outcome.note = f"Could not reconcile with the live run log ({type(exc).__name__})."
    outcome.classification = classify_failure(report, live, data_tabs)
    if outcome.classification == SUCCEEDED_UNREPORTED:
        outcome.note = (
            "Data tabs written and verified; only the final run-log verification "
            "read failed. The live run-log row says SUCCESS/PASSED."
        )
    elif outcome.classification == FAILED_BEFORE_WRITE:
        outcome.note = (outcome.note + " " if outcome.note else "") + (
            "No tab was changed; fix the cause and rerun the publication."
        )
    elif auto_rollback and write_dir is not None:
        log("Partial write detected; restoring the data tabs from the publisher's snapshots.")
        try:
            rb_code = publisher_main(
                ["--rollback", str(write_dir), "--output-root", str(output_root)]
            )
            rb = _load_report(_newest(output_root, "rollback"), "rollback_report.json")
            outcome.rollback = {
                "exit_code": rb_code,
                "status": rb.get("status", ""),
                "restored": rb.get("restored", []),
            }
        except Exception as exc:  # noqa: BLE001
            outcome.rollback = {"exit_code": None, "status": f"FAILED ({type(exc).__name__})"}
        outcome.note += " Rolled back to the previous publication."
    return outcome
