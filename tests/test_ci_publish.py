"""Unattended publication: idempotency, partial writes, reconciliation and safe retries.

The Sheets publisher itself is covered by test_sheet_publication.py and test_dashboard_layout.py;
here it is replaced by a scripted stand-in so every failure mode can be produced on demand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from laptop_tracker import ci_publish as cp
from laptop_tracker import state_store as ss
from laptop_tracker.variation_publication import DATA_TABS, RUN_LOG_TAB

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_ci_gate import make_run  # noqa: E402

import ci_publish as script  # noqa: E402

PUB = "PUB-abc"
HEADER = [
    "Publication ID",
    "Publication status",
    "Publication attempts",
    "Tabs written",
    "Readback",
]


class FakeSheet:
    def __init__(self, rows=None, quota_errors=0):
        self.rows = rows or []
        self.quota_errors = quota_errors
        self.reads = 0

    def worksheet(self, title):
        assert title == RUN_LOG_TAB
        outer = self

        class Tab:
            def get_all_values(self):
                outer.reads += 1
                if outer.quota_errors:
                    outer.quota_errors -= 1
                    raise RuntimeError("APIError [429]: Quota exceeded for quota metric 'Read'")
                return [HEADER, *outer.rows]

        return Tab()


def row(status="SUCCESS", readback="PASSED", pub=PUB):
    return [pub, status, "1", ", ".join(DATA_TABS), readback]


class Publisher:
    """Stands in for publish_variation_prices.main and records every call."""

    def __init__(
        self,
        tmp,
        *,
        report=None,
        code=0,
        preview_status="PREVIEW_ONLY",
        preview_id=PUB,
        rollback_status="SUCCESS",
    ):
        self.calls: list[list[str]] = []
        self.report, self.code = report, code
        self.preview_status, self.preview_id = preview_status, preview_id
        self.rollback_status = rollback_status
        self.n = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        root = Path(argv[argv.index("--output-root") + 1])
        self.n += 1
        if "--preview" in argv:
            folder = root / f"2026111010000{self.n}_preview"
            folder.mkdir(parents=True)
            (folder / "publication_report.json").write_text(
                json.dumps({"status": self.preview_status, "publication_id": self.preview_id})
            )
            return 0 if self.preview_status == "PREVIEW_ONLY" else 1
        if "--rollback" in argv:
            folder = root / f"2026111010000{self.n}_rollback"
            folder.mkdir(parents=True)
            (folder / "rollback_report.json").write_text(
                json.dumps({"status": self.rollback_status, "restored": list(DATA_TABS)})
            )
            return 0
        folder = root / f"2026111010000{self.n}_write"
        folder.mkdir(parents=True)
        (folder / "backup_PILOT_PRICE_COMPARISON.json").write_text("{}")
        if self.report is not None:
            (folder / "publication_report.json").write_text(json.dumps(self.report))
        return self.code

    def writes(self):
        return [c for c in self.calls if "--write-sheets" in c]

    def rollbacks(self):
        return [c for c in self.calls if "--rollback" in c]


def good_report(**over):
    return {
        "status": "SUCCESS",
        "publication_id": PUB,
        "tabs_written": list(DATA_TABS),
        "readback_issues": [],
        **over,
    }


def run_publish(tmp_path, publisher, sheet, **kw):
    return cp.publish_run(
        publication_id=PUB,
        publisher_args=["--variation-dir", "v"],
        output_root=tmp_path / "pubs",
        run_dir=tmp_path,
        open_spreadsheet=lambda read_only: sheet,
        publisher_main=publisher,
        data_tabs=DATA_TABS,
        sleep=lambda s: None,
        log=lambda m: None,
        **kw,
    )


# the publisher wrapper ------------------------------------------------------------------------


def test_a_clean_publication_is_published_once(tmp_path) -> None:
    publisher = Publisher(tmp_path, report=good_report())
    outcome = run_publish(tmp_path, publisher, FakeSheet())
    assert outcome.classification == cp.PUBLISHED and outcome.ok
    assert len(publisher.writes()) == 1 and not publisher.rollbacks()


def test_an_already_published_run_is_never_written_again(tmp_path) -> None:
    publisher = Publisher(tmp_path, report=good_report())
    outcome = run_publish(tmp_path, publisher, FakeSheet([row()]))
    assert outcome.classification == cp.ALREADY_PUBLISHED and outcome.ok
    assert publisher.calls == []  # no write, not even a preview


@pytest.mark.parametrize(
    "status,readback",
    [
        ("IN_PROGRESS", "NOT_RUN"),
        ("PARTIAL_FAILURE", "NOT_RUN"),
        ("READBACK_FAILED", "FAILED (2 issue(s))"),
    ],
)
def test_an_earlier_unsuccessful_attempt_does_not_count_as_published(
    tmp_path, status, readback
) -> None:
    publisher = Publisher(tmp_path, report=good_report())
    outcome = run_publish(tmp_path, publisher, FakeSheet([row(status, readback)]))
    assert outcome.classification == cp.PUBLISHED and len(publisher.writes()) == 1


def test_other_publications_in_the_run_log_are_ignored(tmp_path) -> None:
    publisher = Publisher(tmp_path, report=good_report())
    outcome = run_publish(tmp_path, publisher, FakeSheet([row(pub="PUB-other")]))
    assert outcome.classification == cp.PUBLISHED


def test_a_failure_before_any_write_is_reported_and_not_retried_or_rolled_back(tmp_path) -> None:
    report = {"status": "ACCESS_FAILED", "publication_id": PUB, "error": "RefreshError"}
    publisher = Publisher(tmp_path, report=report, code=1)
    outcome = run_publish(tmp_path, publisher, FakeSheet())
    assert outcome.classification == cp.FAILED_BEFORE_WRITE and not outcome.ok
    assert (
        len(publisher.writes()) == 1 and not publisher.rollbacks()
    )  # one attempt, nothing to undo


def test_a_refused_destination_is_a_failure_before_write(tmp_path) -> None:
    report = {"status": "DESTINATION_REFUSED", "publication_id": PUB}
    outcome = run_publish(tmp_path, Publisher(tmp_path, report=report, code=1), FakeSheet())
    assert outcome.classification == cp.FAILED_BEFORE_WRITE


def test_a_partial_write_is_rolled_back_once_and_never_retried(tmp_path) -> None:
    report = {
        "status": "PARTIAL_FAILURE",
        "publication_id": PUB,
        "tabs_written": list(DATA_TABS[:2]),
        "readback_issues": [],
        "error": "APIError: 500",
    }
    publisher = Publisher(tmp_path, report=report, code=1)
    sheet = FakeSheet([row("PARTIAL_FAILURE", "NOT_RUN")])
    outcome = run_publish(tmp_path, publisher, sheet)
    assert outcome.classification == cp.PARTIAL_WRITE and not outcome.ok
    assert len(publisher.writes()) == 1 and len(publisher.rollbacks()) == 1
    assert outcome.rollback["status"] == "SUCCESS"
    assert publisher.rollbacks()[0][1].endswith("_write")  # restores the snapshots of THIS write


def test_a_failed_readback_is_rolled_back_so_the_sheet_never_keeps_unverified_data(
    tmp_path,
) -> None:
    report = {
        "status": "READBACK_FAILED",
        "publication_id": PUB,
        "tabs_written": list(DATA_TABS),
        "readback_issues": ["PILOT_PRICE_COMPARISON row 4 differs"],
    }
    publisher = Publisher(tmp_path, report=report, code=1)
    sheet = FakeSheet([row("READBACK_FAILED", "FAILED (1 issue(s))")])
    outcome = run_publish(tmp_path, publisher, sheet)
    assert outcome.classification == cp.PARTIAL_WRITE and outcome.readback_issues == 1
    assert len(publisher.rollbacks()) == 1


def test_auto_rollback_can_be_disabled(tmp_path) -> None:
    report = {"status": "PARTIAL_FAILURE", "publication_id": PUB, "tabs_written": ["x"]}
    publisher = Publisher(tmp_path, report=report, code=1)
    outcome = run_publish(tmp_path, publisher, FakeSheet(), auto_rollback=False)
    assert outcome.classification == cp.PARTIAL_WRITE and not publisher.rollbacks()


def test_a_write_that_succeeded_but_failed_to_report_is_not_rewritten_or_rolled_back(
    tmp_path,
) -> None:
    # The six-tab readback uses the per-minute read quota, so the final run-log read can fail
    # after the SUCCESS row was written (seen on 2026-11-05).
    report = {
        "status": "PARTIAL_FAILURE",
        "publication_id": PUB,
        "tabs_written": list(DATA_TABS),
        "readback_issues": [],
        "error": "Run-log update failed: APIError [429]",
    }
    publisher = Publisher(tmp_path, report=report, code=1)
    sheets = iter([FakeSheet(), FakeSheet([row()])])  # nothing before, SUCCESS/PASSED afterwards
    outcome = cp.publish_run(
        publication_id=PUB,
        publisher_args=[],
        output_root=tmp_path / "pubs",
        run_dir=tmp_path,
        open_spreadsheet=lambda read_only: next(sheets),
        publisher_main=publisher,
        data_tabs=DATA_TABS,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert outcome.classification == cp.SUCCEEDED_UNREPORTED and outcome.ok
    assert len(publisher.writes()) == 1 and not publisher.rollbacks()


def test_read_quota_errors_are_waited_out_not_treated_as_failures(tmp_path) -> None:
    sleeps: list[float] = []
    sheet = FakeSheet([row()], quota_errors=2)
    found = cp.read_live_log_row(sheet, PUB, sleep=sleeps.append)
    assert found.status == "SUCCESS" and len(sleeps) == 2


def test_other_read_errors_are_not_swallowed(tmp_path) -> None:
    class Broken:
        def worksheet(self, title):
            raise PermissionError("403 forbidden")

    with pytest.raises(PermissionError):
        cp.read_live_log_row(Broken(), PUB, sleep=lambda s: None)


def test_an_unreadable_run_log_stops_before_any_write(tmp_path) -> None:
    class Down:
        def worksheet(self, title):
            raise PermissionError("403")

    publisher = Publisher(tmp_path, report=good_report())
    outcome = run_publish(tmp_path, publisher, Down())
    assert outcome.classification == cp.FAILED_BEFORE_WRITE and publisher.calls == []


def test_a_publisher_that_builds_another_publication_is_refused(tmp_path) -> None:
    publisher = Publisher(tmp_path, report=good_report(publication_id="PUB-other"))
    outcome = run_publish(tmp_path, publisher, FakeSheet())
    assert outcome.classification == cp.PARTIAL_WRITE and not outcome.ok


# the script around it -------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path):
    store = ss.LocalBackend(tmp_path / "bucket")
    return store


def prepare(tmp_path, **kw):
    run = make_run(tmp_path, **kw)
    # make_run's report time is fixed in 2026-11; keep the age check out of these tests
    return run


def invoke(tmp_path, run, publisher, sheet, backend, *extra, write=True):
    argv = ["--run-dir", str(run), "--max-age-hours", "100000", *extra]
    if write:
        argv.append("--write")
    return script.main(
        argv,
        backend=backend,
        publisher=publisher,
        open_spreadsheet=lambda read_only: sheet,
        sleep=lambda s: None,
    )


def test_preview_mode_never_writes(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    publisher = Publisher(tmp_path, report=good_report())
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend, write=False) == 0
    assert publisher.writes() == [] and any("--preview" in c for c in publisher.calls)


def test_production_mode_publishes_and_records_everything_durably(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    ss.save_bundle(backend, run, run.name, {})
    publisher = Publisher(tmp_path, report=good_report())
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend) == 0
    record, _ = ss.read_publication_record(backend, PUB)
    assert record["last_status"] == cp.PUBLISHED and len(record["attempts"]) == 1
    names = backend.list(f"publications/{PUB}/")
    assert any(n.endswith("backup_PILOT_PRICE_COMPARISON.json") for n in names)  # recovery data
    latest = json.loads(backend.get("publications/latest_success.json")[0])
    assert latest["publication_id"] == PUB and latest["counts"]["selected_variations"] == 100
    manifest = json.loads(backend.get(f"runs/{run.name}/run_manifest.json")[0])
    assert manifest["publication_status"] == cp.PUBLISHED


def test_a_blocked_gate_stops_before_the_publisher_is_even_called(tmp_path, backend) -> None:
    run = prepare(tmp_path, discovery_code=2, run_status="PARTIAL_SUCCESS", failures=True)
    publisher = Publisher(tmp_path, report=good_report())
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend) == 1
    assert publisher.calls == []


def test_accepting_retailer_failures_is_an_explicit_choice(tmp_path, backend) -> None:
    run = prepare(tmp_path, discovery_code=2, run_status="PARTIAL_SUCCESS", failures=True)
    ss.save_bundle(backend, run, run.name, {})
    publisher = Publisher(tmp_path, report=good_report())
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend, "--accept-retailer-failures") == 0


def test_a_live_preview_refusal_blocks_the_write(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    publisher = Publisher(tmp_path, report=good_report(), preview_status="DESTINATION_REFUSED")
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend) == 1
    assert publisher.writes() == []


def test_a_changed_publication_id_between_collection_and_publish_blocks(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    publisher = Publisher(tmp_path, report=good_report(), preview_id="PUB-different")
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend) == 1
    assert publisher.writes() == []


def test_rerunning_after_success_is_a_no_op_and_adds_no_duplicate_record(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    ss.save_bundle(backend, run, run.name, {})
    sheet = FakeSheet()
    publisher = Publisher(tmp_path, report=good_report())
    assert invoke(tmp_path, run, publisher, sheet, backend) == 0
    sheet.rows.append(row())  # the Sheet now shows the publication as done
    assert invoke(tmp_path, run, publisher, sheet, backend) == 0
    assert len(publisher.writes()) == 1  # the second invocation wrote nothing
    record, _ = ss.read_publication_record(backend, PUB)
    assert [a["status"] for a in record["attempts"]] == [cp.PUBLISHED, cp.ALREADY_PUBLISHED]


def test_a_failed_publication_exits_nonzero_and_still_keeps_the_backups(tmp_path, backend) -> None:
    run = prepare(tmp_path)
    ss.save_bundle(backend, run, run.name, {})
    report = {"status": "PARTIAL_FAILURE", "publication_id": PUB, "tabs_written": ["x"]}
    publisher = Publisher(tmp_path, report=report, code=1)
    assert invoke(tmp_path, run, publisher, FakeSheet(), backend) == 1
    record, _ = ss.read_publication_record(backend, PUB)
    assert record["last_status"] == cp.PARTIAL_WRITE
    assert backend.list(f"publications/{PUB}/")
    assert not backend.exists("publications/latest_success.json")


def test_storage_must_be_configured_for_production_publication(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GCS_STATE_BUCKET", raising=False)
    monkeypatch.delenv("STATE_LOCAL_DIR", raising=False)
    run = prepare(tmp_path)
    publisher = Publisher(tmp_path, report=good_report())
    code = script.main(
        ["--run-dir", str(run), "--write", "--max-age-hours", "100000"],
        publisher=publisher,
        open_spreadsheet=lambda r: FakeSheet(),
    )
    assert code == 1 and publisher.calls == []
