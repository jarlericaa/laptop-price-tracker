# ruff: noqa: F811  (the ``world`` fixture is imported, then named as a test parameter)
"""The publisher under Google Sheets quota pressure (the 2026-11-12 hosted failure).

Uses the in-memory fake spreadsheet of the publisher tests: no network, no real spreadsheet. The
fake counts the requests the real API would count, so the number of worksheet-list and tab reads
per publication is asserted, and quota failures are injected at every phase of a publication.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_variation_publication import (  # noqa: F401 - `world` is a fixture
    NOW,
    SpyingSpreadsheet,
    latest_report,
    run,
    template_tabs,
    world,
)
from test_verified_top import load_script

from laptop_tracker import ci_publish as cp
from laptop_tracker import sheets_quota as q
from laptop_tracker.price_comparison_view import PRICE_TAB
from laptop_tracker.sheet_publication import SNAPSHOT_FIELDS
from laptop_tracker.variation_publication import (
    CONTRACT_PATH,
    DATA_TABS,
    PUBLISHER_TABS,
    RUN_LOG_TAB,
    build_variation_plan,
    legacy_run_log_header,
    load_contract,
    load_variation_report,
)

# The workbook before a first publication has no PRICE COMPARISON tab (the publisher creates it),
# so "as before" comparisons cover the tabs that already existed.
EXISTING_TABS = tuple(t for t in PUBLISHER_TABS if t != PRICE_TAB)
EXISTING_DATA_TABS = tuple(t for t in DATA_TABS if t != PRICE_TAB)


def exhausted() -> q.SheetsRetryExhausted:
    return q.SheetsRetryExhausted(
        "Sheets GET /v4/spreadsheets/<id>",
        attempts=6,
        waited=124.0,
        failure=q.Failure(q.QUOTA, 429, reason="Quota exceeded for 'Read requests per minute'"),
    )


class CountingSpreadsheet(SpyingSpreadsheet):
    """Counts worksheet listings, content reads and snapshot reads; can inject quota failures."""

    def __init__(self, tabs=None) -> None:
        super().__init__(tabs)
        self.listings = 0
        self.snapshot_reads = 0
        self.fail_snapshots = False
        self.fail_reads_after_batches: int | None = None

    def worksheets(self):
        self.listings += 1
        return super().worksheets()

    def fetch_sheet_metadata(self, params=None):
        is_snapshot = params.get("fields") == SNAPSHOT_FIELDS
        self.snapshot_reads += is_snapshot
        if is_snapshot and self.fail_snapshots:
            raise exhausted()
        if self.fail_reads_after_batches is not None and (
            self.batches >= self.fail_reads_after_batches
        ):
            raise exhausted()
        return super().fetch_sheet_metadata(params)


def run_log_rows(fake) -> list[dict[str, str]]:
    values = fake.values(RUN_LOG_TAB)
    return [dict(zip(values[0], row, strict=False)) for row in values[1:]]


def seeded_tabs() -> dict:
    tabs = template_tabs()
    old = [*legacy_run_log_header()]
    tabs[RUN_LOG_TAB] = [old, ["PUB-OLD"] + ["x"] * (len(old) - 1)]
    return tabs


# worksheet metadata is fetched once per phase, not once per tab ------------------------------


def test_a_publication_lists_the_worksheets_once_per_phase_not_once_per_tab(world) -> None:  # noqa: F811
    fake = CountingSpreadsheet(template_tabs())
    code, fake, _ = run(world, fake=fake)
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"
    # preview/inspection, the publisher's own inspection and backups, the readback, and the run
    # log after the tab visibility changed: four phases. Before the fix this was one listing per
    # tab read and per snapshot (about 50 per publication).
    assert fake.listings <= 4
    # Tab contents are still read every time they are needed: nothing was cached or skipped.
    assert len(fake.read_titles) >= 4 * len(PUBLISHER_TABS)
    assert fake.snapshot_reads >= len(PUBLISHER_TABS)


def test_a_preview_lists_the_worksheets_once(world) -> None:  # noqa: F811
    fake = CountingSpreadsheet(template_tabs())
    code, fake, _ = run(world, mode="--preview", fake=fake)
    assert code == 0 and fake.mutations == []
    assert fake.listings == 1


def test_the_cache_does_not_hide_a_tab_created_between_phases(world) -> None:  # noqa: F811
    tabs = template_tabs()
    del tabs["PILOT_PRICE_ALTERNATIVES"]
    fake = CountingSpreadsheet(tabs)
    code, fake, _ = run(world, fake=fake)
    assert code == 1 and fake.mutations == []  # a missing template tab is refused, never created
    fake.add_worksheet("PILOT_PRICE_ALTERNATIVES", 1000, 26)  # the owner prepares the template
    fake.sheets["PILOT_PRICE_ALTERNATIVES"]["grid"] = template_tabs_grid("PILOT_PRICE_ALTERNATIVES")
    fake.mutations.clear()
    code, fake, _ = run(world, fake=fake, now=NOW + timedelta(minutes=5))
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"


def template_tabs_grid(title: str) -> dict:
    return {
        (r, c): {"userEnteredValue": {"stringValue": value}}
        for r, row in enumerate(template_tabs()[title])
        for c, value in enumerate(row)
    }


# the 2026-11-12 incident: the quota outlasts the retry policy before the first write ----------


def test_quota_exhaustion_while_taking_backups_changes_nothing_and_says_so(world, capsys) -> None:  # noqa: F811
    fake = CountingSpreadsheet(seeded_tabs())
    fake.fail_snapshots = True
    before = {t: fake.values(t) for t in EXISTING_TABS}
    code, fake, _ = run(world, fake=fake)

    report = latest_report(world)
    assert code == 1 and fake.mutations == []
    assert report["status"] == "FAILED_BEFORE_WRITE" and report["tabs_written"] == []
    assert "SheetsRetryExhausted" in report["error"] and "HTTP 429" in report["error"]
    assert "stopped before any write" in capsys.readouterr().err
    assert {t: fake.values(t) for t in EXISTING_TABS} == before
    assert PRICE_TAB not in fake.sheets  # not even the new tab was created

    # Once the quota clears, the same publication goes through and the history is intact.
    fake.fail_snapshots = False
    code, fake, _ = run(world, fake=fake, now=NOW + timedelta(minutes=5))
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"
    assert [r["Publication ID"] for r in run_log_rows(fake)][0] == "PUB-OLD"


def test_the_ci_wrapper_classifies_that_failure_as_before_write_and_does_not_roll_back(
    world,
    tmp_path,  # noqa: F811
) -> None:
    fake = CountingSpreadsheet(seeded_tabs())
    fake.fail_snapshots = True
    outcome = publish_through_ci(world, tmp_path, fake)
    assert outcome.classification == cp.FAILED_BEFORE_WRITE and not outcome.ok
    assert fake.mutations == [] and outcome.rollback == {}


# quota exhaustion after the write began is recorded and recoverable --------------------------


def test_quota_exhaustion_during_readback_is_recorded_not_left_in_progress(world) -> None:  # noqa: F811
    fake = CountingSpreadsheet(seeded_tabs())
    fake.fail_reads_after_batches = 1 + len(DATA_TABS)  # every data tab is written, then reads fail
    before = {t: fake.values(t) for t in EXISTING_DATA_TABS}
    code, fake, _ = run(world, fake=fake)
    fake.fail_reads_after_batches = None  # the quota has cleared by the time a person looks

    report = latest_report(world)
    assert code == 1 and report["status"] == "PARTIAL_FAILURE"
    assert "Readback could not be completed" in report["error"]
    assert report["tabs_written"] == list(DATA_TABS)
    rows = run_log_rows(fake)
    assert [r["Publication ID"] for r in rows][0] == "PUB-OLD"  # earlier history kept
    assert rows[-1]["Publication status"] == "PARTIAL_FAILURE"  # never left at IN_PROGRESS

    # The local snapshots restore every data tab exactly as before the attempt.
    folder = sorted(world["out"].iterdir())[-1]
    assert sorted(p.name for p in folder.glob("backup_*.json")) == sorted(
        f"backup_{t}.json" for t in EXISTING_TABS
    )
    code = load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=NOW + timedelta(minutes=9),
    )
    assert code == 0
    assert {t: fake.values(t) for t in EXISTING_DATA_TABS} == before
    # Visibility was never switched: the readback did not complete, so the tabs people use stay
    # and the new, unverified business tab stays hidden.
    assert PRICE_TAB not in fake.visible_tabs()
    assert "PILOT_PRICE_COMPARISON" in fake.visible_tabs()


@pytest.mark.parametrize("failing_batch", [1, 2, 4, 7])
def test_a_write_failure_at_any_point_keeps_history_and_backups_and_can_be_rolled_back(
    world,
    failing_batch,  # noqa: F811
) -> None:
    fake = CountingSpreadsheet(seeded_tabs())
    fake.fail_on_batch = failing_batch
    before = {t: fake.values(t) for t in EXISTING_TABS}
    code, fake, _ = run(world, fake=fake)

    report = latest_report(world)
    assert code == 1 and report["status"] == "PARTIAL_FAILURE"
    rows = run_log_rows(fake)
    assert rows[0]["Publication ID"] == "PUB-OLD" and rows[0]["Publication status"] == "x"
    mine = [r for r in rows if r["Publication ID"] != "PUB-OLD"]
    assert len(mine) == 1 and mine[0]["Publication status"] == "PARTIAL_FAILURE"
    folder = sorted(world["out"].iterdir())[-1]
    assert len(list(folder.glob("backup_*.json"))) == len(EXISTING_TABS)

    code = load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=NOW + timedelta(minutes=9),
    )
    assert code == 0
    assert {t: fake.values(t) for t in EXISTING_DATA_TABS} == {
        t: before[t] for t in EXISTING_DATA_TABS
    }
    after = run_log_rows(fake)  # the run log is a record: rollback never rewrites it
    assert [r["Publication ID"] for r in after] == [r["Publication ID"] for r in rows]


# idempotency and recovery through the unattended wrapper -------------------------------------


class RunLogView:
    """What ``ci_publish`` reads: ``spreadsheet.worksheet(tab).get_all_values()``."""

    def __init__(self, fake) -> None:
        self.fake = fake

    def worksheet(self, title):
        fake = self.fake

        class Tab:
            def get_all_values(self):
                return fake.values(title)

        return Tab()


TICKS = itertools.count(1)


def publication_id(world) -> str:
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    return build_variation_plan(report, load_contract(CONTRACT_PATH)).publication_id


def publish_through_ci(world, tmp_path, fake, **kwargs) -> cp.PublishOutcome:
    script = load_script("publish_variation_prices")

    def publisher(argv):  # every publisher run gets its own timestamped output folder
        return script.main(
            argv,
            spreadsheet_factory=lambda read_only: fake,
            now=datetime(2026, 10, 12, 3, tzinfo=UTC) + timedelta(minutes=next(TICKS)),
        )

    return cp.publish_run(
        publication_id=publication_id(world),
        publisher_args=[
            "--variation-dir",
            str(world["variation"]),
            "--reports-dir",
            str(world["reports"]),
            "--context-report-dir",
            str(world["context"]),
            "--collection-dir",
            str(world["collection"]),
        ],
        output_root=tmp_path / "ci_pubs",
        run_dir=tmp_path,
        open_spreadsheet=lambda read_only: RunLogView(fake),
        publisher_main=publisher,
        data_tabs=DATA_TABS,
        sleep=lambda s: None,
        log=lambda m: None,
        **kwargs,
    )


def test_publishing_the_same_run_twice_writes_it_once(world, tmp_path) -> None:  # noqa: F811
    fake = CountingSpreadsheet(template_tabs())
    first = publish_through_ci(world, tmp_path, fake)
    assert first.classification == cp.PUBLISHED and first.ok
    batches, content = fake.batches, {t: fake.values(t) for t in DATA_TABS}

    second = publish_through_ci(world, tmp_path, fake)
    assert second.classification == cp.ALREADY_PUBLISHED and second.ok
    assert fake.batches == batches  # not one more write
    assert {t: fake.values(t) for t in DATA_TABS} == content
    assert len(run_log_rows(fake)) == 1 and run_log_rows(fake)[0]["Publication attempts"] == "1.0"


def test_after_a_partial_attempt_nothing_is_written_until_a_person_confirms(
    world, tmp_path
) -> None:  # noqa: F811
    fake = CountingSpreadsheet(seeded_tabs())
    fake.fail_on_batch = 4
    before = {t: fake.values(t) for t in EXISTING_DATA_TABS}
    first = publish_through_ci(world, tmp_path, fake)
    # The wrapper rolled the data tabs back; the run log still records the failed attempt.
    assert first.classification == cp.PARTIAL_WRITE and first.rollback["status"] == "SUCCESS"
    assert {t: fake.values(t) for t in EXISTING_DATA_TABS} == before
    assert run_log_rows(fake)[-1]["Publication status"] == "PARTIAL_FAILURE"

    fake.fail_on_batch = None
    batches = fake.batches
    blocked = publish_through_ci(world, tmp_path, fake)
    assert blocked.classification == cp.BLOCKED_PRIOR_ATTEMPT and not blocked.ok
    assert fake.batches == batches  # inspected, not written

    confirmed = publish_through_ci(world, tmp_path, fake, allow_rerun_after_partial=True)
    assert confirmed.classification == cp.PUBLISHED and confirmed.ok
    rows = run_log_rows(fake)
    assert [r["Publication ID"] for r in rows][0] == "PUB-OLD"
    assert rows[-1]["Publication status"] == "SUCCESS" and rows[-1]["Publication attempts"] == "2.0"
    assert sum(1 for r in rows if r["Publication ID"] == publication_id(world)) == 1


def test_the_plan_and_the_content_do_not_depend_on_the_quota_layer(world) -> None:  # noqa: F811
    # The same publication through a counting fake and through the plain fake: identical bytes.
    a = CountingSpreadsheet(template_tabs())
    run(world, fake=a)
    first = json.loads((sorted(world["out"].iterdir())[-1] / "publication_report.json").read_text())
    b = SpyingSpreadsheet(template_tabs())
    run(world, fake=b, now=NOW + timedelta(minutes=3))
    second = latest_report(world)
    assert first["published_content_sha256"] == second["published_content_sha256"]
    assert {t: a.values(t) for t in DATA_TABS} == {t: b.values(t) for t in DATA_TABS}
