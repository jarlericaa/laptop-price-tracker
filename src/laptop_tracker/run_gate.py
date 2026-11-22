"""Publication readiness gate and run diagnostics, read from a weekly run's own artifacts.

The gate trusts files the pipeline wrote (``status.json``, ``stages.jsonl``, the discovery summary
and the publisher's own preview report), never console text. It adds no business rule: the matching,
mapping, coverage and comparability policies stay in the pipeline and in the publisher's preview,
which already refuses an invalid plan. It only decides whether *unattended* publication may go on,
and it reports why not.

Three conditions beyond the existing ones are deliberate, documented safety extensions:

* retailer request failures are assessed by ``retailer_failure_policy``: minor, isolated failures
  publish with a warning, significant ones block unless a person explicitly accepts them (a person
  never overrides missing or inconsistent discovery files or any other check here);
* a run older than ``max_age_hours`` is superseded and is not published;
* an optional, owner-configured relative drop in matching rows against the last publication.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import retailer_failure_policy as rfp

PASS, PASS_WITH_WARNINGS, BLOCKED = "PASS", "PASS_WITH_WARNINGS", "BLOCKED"
READY = "READY_TO_PUBLISH"
# Order of the driver's stages (scripts/run_variation_week.sh) and the exit codes it accepts.
EXPECTED_STAGES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("sales_refresh", (0,)),
    ("catalog_check", (0,)),
    ("variation_build", (0,)),
    ("variation_audit", (0,)),
    ("selection_audit", (0,)),
    ("catalog_corrections", (0,)),
    ("catalog_preview", (0,)),
    ("catalog", (0,)),
    ("discovery_preview", (0,)),
    ("discovery", (0, 2)),
    ("prices", (0,)),
    ("alt_audit", (0,)),
    ("variation_reports", (0,)),
    ("diagnostics", (0,)),
    ("publish_preview", (0,)),
)
REQUIRED_DIRS = ("shopee_variations", "variation_reports", "context_reports")
RETAILERS = ("datablitz", "laptop_factory", "pcx", "pcc", "oct")
DEFAULT_MAX_AGE_HOURS = 24 * 7


@dataclass
class GateResult:
    run_id: str
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def decision(self) -> str:
        """``BLOCKED``, ``PASS_WITH_WARNINGS`` or ``PASS``."""
        if self.blocking:
            return BLOCKED
        return PASS_WITH_WARNINGS if self.warnings else PASS

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "decision": self.decision,
            "ok": self.ok,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "metrics": self.metrics,
        }


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_stages(run_dir: Path) -> list[dict]:
    path = run_dir / "stages.jsonl"
    stages = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                stages.append(json.loads(line))
    return stages


def retailer_diagnostics(run_dir: Path) -> dict[str, dict]:
    """Per retailer: searches by outcome, failed page inspections and HTTP 429 responses, from the
    discovery run's own index files."""
    table: dict[str, dict] = {
        r: {
            "searches": 0,
            "search_errors": 0,
            "not_sent_failing": 0,
            "inspections": 0,
            "inspection_failures": 0,
            "http_429": 0,
            "other_http_errors": 0,
        }
        for r in RETAILERS
    }
    discovery = run_dir / "discovery"
    for row in _rows(discovery / "search_requests.csv"):
        entry = table.setdefault(row["retailer_id"], dict.fromkeys(table["oct"], 0))
        outcome = row.get("outcome", "")
        entry["searches"] += 1
        entry["search_errors"] += outcome == "SEARCH_ERROR"
        entry["not_sent_failing"] += outcome == "NOT_SENT_RETAILER_FAILING"
        status = row.get("http_status", "")
        if status == "429":
            entry["http_429"] += 1
        elif status.isdigit() and int(status) >= 400:
            entry["other_http_errors"] += 1
    for row in _rows(discovery / "product_inspections.csv"):
        entry = table.setdefault(row["retailer_id"], dict.fromkeys(table["oct"], 0))
        entry["inspections"] += 1
        failed = row.get("outcome", "") == "FETCH_FAILED"
        entry["inspection_failures"] += failed
        if failed:
            entry["other_http_errors"] += 1
    # Every row of a collection round's rate_limit_events.csv is one HTTP 429 response.
    for events in sorted(discovery.glob("collection_rounds/*/rate_limit_events.csv")):
        for row in _rows(events):
            entry = table.setdefault(row.get("retailer_id", ""), dict.fromkeys(table["oct"], 0))
            entry["http_429"] += 1
    return table


def retailer_failures(summary: dict, retailers: dict[str, dict]) -> list[str]:
    """Plain-language list of request failures in a discovery run (empty when there were none).

    Every complete run on record (2026-11-03 and 2026-11-05) has none; the throttled runs of
    2026-11-09 had ``PROVIDER_FAILURE`` targets, searches not sent after repeated errors and
    failed page fetches."""
    found: list[str] = []
    for name, v in retailers.items():
        parts = []
        if v["search_errors"]:
            parts.append(f"{v['search_errors']} search error(s)")
        if v["not_sent_failing"]:
            parts.append(f"{v['not_sent_failing']} search(es) not sent after repeated errors")
        if v["inspection_failures"]:
            parts.append(f"{v['inspection_failures']} page fetch(es) failed")
        if v["other_http_errors"] and not parts:
            parts.append(f"{v['other_http_errors']} HTTP error(s)")
        if parts:
            suffix = f" ({v['http_429']} HTTP 429)" if v["http_429"] else ""
            found.append(f"{name}: " + ", ".join(parts) + suffix)
    provider = (summary.get("discovery_outcomes") or {}).get("PROVIDER_FAILURE", 0)
    stopped = (summary.get("search_stop_reasons") or {}).get("RETAILER_STOPPED_AFTER_ERRORS", 0)
    if (provider or stopped) and not found:
        found.append(
            f"{provider} target(s) with provider failures, {stopped} search(es) "
            "stopped after errors"
        )
    return found


def evaluate(
    run_dir: Path,
    *,
    now: datetime | None = None,
    accept_retailer_failures: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    previous_counts: dict | None = None,
    max_matching_drop_pct: float | None = None,
    policy: rfp.FailurePolicy | None = None,
) -> GateResult:
    policy = policy or rfp.FailurePolicy()
    now = now or datetime.now(UTC)
    result = GateResult(run_id=run_dir.name)
    block, warn = result.blocking.append, result.warnings.append
    metrics = result.metrics

    status = _json(run_dir / "status.json")
    if status is None:
        block("status.json is missing or unreadable: the run did not finish.")
    else:
        metrics["pipeline_status"] = status.get("status")
        if status.get("status") != READY:
            block(f"Pipeline status is {status.get('status')!r}, not {READY}.")

    stages = read_stages(run_dir)
    seen = {s["stage"]: s for s in stages}
    for name, accepted in EXPECTED_STAGES:
        stage = seen.get(name)
        if stage is None:
            block(f"Stage {name} did not run.")
        elif stage.get("exit_code") not in accepted:
            block(f"Stage {name} exited with {stage.get('exit_code')}.")
    if stages:
        metrics["started_at"] = stages[0].get("started_at")
        metrics["finished_at"] = (status or {}).get("finished_at") or stages[-1].get("finished_at")
        start, end = _parse_time(metrics["started_at"]), _parse_time(metrics["finished_at"])
        if start and end:
            metrics["duration_minutes"] = round((end - start).total_seconds() / 60, 1)
        metrics["stage_durations_minutes"] = {
            s["stage"]: round(
                ((_parse_time(s["finished_at"]) - _parse_time(s["started_at"])).total_seconds())
                / 60,
                1,
            )
            for s in stages
        }

    for folder in REQUIRED_DIRS:
        path = run_dir / folder
        if not path.is_dir() or not any(path.iterdir()):
            block(f"Required report folder {folder}/ is missing or empty.")

    summary = _json(run_dir / "discovery" / "discovery_summary.json")
    discovery_code = (seen.get("discovery") or {}).get("exit_code")
    metrics["retailers"] = retailer_diagnostics(run_dir)
    metrics["http_429_total"] = sum(v["http_429"] for v in metrics["retailers"].values())
    if summary is None:
        block("discovery/discovery_summary.json is missing.")
    else:
        run_status = summary.get("run_status")
        metrics["discovery_status"] = run_status
        metrics["discovery_requests_sent"] = summary.get("requests_sent")
        metrics["discovery_exit_code"] = discovery_code
        # PARTIAL_SUCCESS (exit 2) is the normal result of a bounded search: result caps and
        # uncollected plausible candidates. The driver accepts it and so does the gate. Only
        # request failures make the coverage untrustworthy.
        if run_status not in {"SUCCESS", "PARTIAL_SUCCESS"}:
            block(f"Discovery finished with status {run_status!r}.")
        failures = retailer_failures(summary, metrics["retailers"])
        metrics["retailer_failures"] = failures
        assessment = rfp.assess(rfp.read_ledger(run_dir), summary, policy, plain_language=failures)
        for line in assessment.integrity:
            block(line)  # Missing or inconsistent discovery files: never accepted by a person.
        acceptance = {
            "requested": accept_retailer_failures,
            "used": False,
            "overrode_block": False,
        }
        if assessment.blocking and accept_retailer_failures and not assessment.integrity:
            acceptance |= {"used": True, "overrode_block": True}
            for line in assessment.blocking:
                warn(f"{line} Accepted explicitly by a person.")
        else:
            hint = (
                "Acceptance does not apply while the discovery files are unreliable."
                if assessment.integrity and accept_retailer_failures
                else "A person must review the diagnostics and accept the run explicitly to "
                "publish it."
            )
            for line in assessment.blocking:
                block(f"{line} {hint}")
        acceptance["used"] = acceptance["used"] or bool(
            accept_retailer_failures and assessment.warnings and not assessment.integrity
        )
        for line in assessment.warnings:
            warn(f"{line} Accepted explicitly by a person." if acceptance["used"] else line)
        metrics["retailer_failure_policy"] = assessment.metrics | {"manual_acceptance": acceptance}

    previews = sorted((run_dir / "publications").glob("*_preview/publication_report.json"))
    report = _json(previews[-1]) if previews else None
    if report is None:
        block("The publication preview report is missing.")
    else:
        metrics["publication_id"] = report.get("publication_id")
        metrics["preview_status"] = report.get("status")
        metrics["published_content_sha256"] = report.get("published_content_sha256")
        if report.get("status") != "PREVIEW_ONLY":
            block(
                f"The publication preview status is {report.get('status')!r}"
                f"{': ' + str(report.get('issues')) if report.get('issues') else ''}."
            )
        if not report.get("publication_id"):
            block("The preview has no publication ID.")
        counts = report.get("counts") or {}
        metrics["counts"] = counts
        metrics["verified_targets"] = counts.get("selected_variations")
        metrics["configuration_contexts"] = counts.get("distinct_configuration_contexts")
        selected = counts.get("selected_variations") or 0
        matching = counts.get("matching_priced_variation_rows") or 0
        any_price = counts.get("variation_rows_with_any_retailer_price") or 0
        metrics["comparable_price_coverage"] = {
            "matching_rows": matching,
            "rows_with_any_price": any_price,
            "selected_rows": selected,
            "matching_share": round(matching / selected, 3) if selected else None,
        }
        scope = report.get("selection_scope") or {}
        metrics["selection_shortfall"] = scope.get("shortfall")
        if not selected:
            block("The preview selected no variations.")
        if previous_counts and max_matching_drop_pct is not None:
            before = previous_counts.get("matching_priced_variation_rows") or 0
            if before and matching < before * (1 - max_matching_drop_pct / 100):
                block(
                    f"Matching rows fell from {before} to {matching}, more than the configured "
                    f"{max_matching_drop_pct:g}% drop against the last publication."
                )
            metrics["previous_matching_rows"] = before

    reference = _parse_time((report or {}).get("report_cutoff_utc")) or _parse_time(
        metrics.get("finished_at")
    )
    if reference is not None:
        age = (now - reference).total_seconds() / 3600
        metrics["age_hours"] = round(age, 1)
        if age > max_age_hours:
            block(
                f"The run is {age:.0f} hours old (limit {max_age_hours:g}); it is superseded "
                "and will not be published."
            )
    else:
        block("The run has no readable report time.")
    return result


def render_summary(title: str, result: GateResult, extra: dict | None = None) -> str:
    """Markdown for ``$GITHUB_STEP_SUMMARY``: no secrets, no raw data, only counts and statuses."""
    m = result.metrics
    lines = [
        f"## {title}",
        "",
        f"**Run:** `{result.run_id}`  ",
        f"**Publication ID:** `{m.get('publication_id', 'n/a')}`  ",
        f"**Pipeline status:** `{m.get('pipeline_status', 'n/a')}`  ",
        f"**Gate:** {result.decision}  ",
    ]
    if m.get("started_at"):
        lines.append(f"**Started / finished (UTC):** {m['started_at']} / {m.get('finished_at')}  ")
    if m.get("duration_minutes") is not None:
        lines.append(f"**Duration:** {m['duration_minutes']} min  ")
    for key, value in (extra or {}).items():
        lines.append(f"**{key}:** {value}  ")
    cov = m.get("comparable_price_coverage")
    lines += [
        "",
        f"- Verified targets (selected variations): {m.get('verified_targets', 'n/a')}",
        f"- Configuration contexts: {m.get('configuration_contexts', 'n/a')}",
    ]
    if cov:
        lines.append(
            f"- Comparable-price coverage: {cov['matching_rows']} matching rows "
            f"({cov['matching_share']}), {cov['rows_with_any_price']} with any retailer "
            f"price, of {cov['selected_rows']}"
        )
    lines += [
        f"- Discovery: {m.get('discovery_status', 'n/a')} "
        f"(requests sent {m.get('discovery_requests_sent', 'n/a')}, "
        f"HTTP 429 responses {m.get('http_429_total', 'n/a')})",
        "",
    ]
    retailers = m.get("retailers") or {}
    if retailers:
        lines += [
            "| Retailer | Searches | Search errors | Not sent (failing) | Page fetches | "
            "Page failures | HTTP 429 |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, v in retailers.items():
            lines.append(
                f"| {name} | {v['searches']} | {v['search_errors']} | "
                f"{v['not_sent_failing']} | {v['inspections']} | "
                f"{v['inspection_failures']} | {v['http_429']} |"
            )
        lines.append("")
    lines += _policy_section(m.get("retailer_failure_policy"))
    if result.blocking:
        lines += ["**Blocking:**", *[f"- {b}" for b in result.blocking], ""]
    if result.warnings:
        lines += ["**Warnings:**", *[f"- {w}" for w in result.warnings], ""]
    return "\n".join(lines) + "\n"


def report_to_console(result: GateResult, stream=None) -> None:
    """Blocking reasons and warnings as log lines; in GitHub Actions each warning is also a
    workflow annotation, so it shows on the run page without opening the log. Counts and retailer
    names only."""
    stream = stream or sys.stderr
    for line in result.blocking:
        print(f"BLOCKED: {line}", file=stream)
    for line in result.warnings:
        print(f"WARNING: {line}", file=stream)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            text = line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning title=Retailer request failures::{text}")


def _policy_section(policy: dict | None) -> list[str]:
    """Counts, rates and thresholds of the retailer-failure policy: no targets, URLs or prices."""
    if not policy:
        return []
    req, acceptance = policy["requests"], policy["manual_acceptance"]
    limits = policy["thresholds"]
    lines = [
        "**Retailer request failures**",
        "",
        f"- Automated decision: {policy['automated_decision']} (severity {policy['severity']})",
        f"- Reason: {policy['reason']}",
        f"- Requests attempted: {req['attempted']}; failed: {req['failed']} "
        f"({req['failure_rate_pct'] if req['failure_rate_pct'] is not None else 'n/a'}%)",
        f"- Search requests failed: {req['search_failures']} of {req['searches_attempted']}; "
        f"page inspections failed: {req['inspection_failures']} of {req['inspections_attempted']}",
        f"- Planned searches never sent: {req['searches_not_executed']} of "
        f"{req['searches_planned']} (retailer failing: "
        f"{req['searches_not_sent_retailer_failing']})",
        f"- Thresholds: at most {limits['max_total']} failed in total, "
        f"{limits['max_per_retailer']} per retailer, {limits['max_retailer_pct']:g}% per "
        f"retailer, {limits['max_total_pct']:g}% overall, {limits['max_unsent_pct']:g}% of the "
        "plan unsent",
        "- Manual acceptance: "
        + (
            "used to publish despite a blocking retailer-failure finding"
            if acceptance["overrode_block"]
            else "requested; the run was within policy anyway"
            if acceptance["used"]
            else "requested; nothing needed it"
            if acceptance["requested"]
            else "not used"
        ),
        "",
    ]
    rows = [(name, v) for name, v in policy["retailers"].items() if v["attempted"] or v["failed"]]
    if rows:
        lines += [
            "| Retailer | Attempted | Failed | Failure rate | Search failures | "
            "Inspection failures |",
            "|---|---|---|---|---|---|",
        ]
        for name, v in rows:
            rate = f"{v['failure_rate_pct']}%" if v["failure_rate_pct"] is not None else "n/a"
            lines.append(
                f"| {name} | {v['attempted']} | {v['failed']} | {rate} | "
                f"{v['search_failures']} | {v['inspection_failures']} |"
            )
        lines.append("")
    return lines
