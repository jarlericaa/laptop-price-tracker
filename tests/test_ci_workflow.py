"""The GitHub Actions workflow, its mode decisions, authentication and driver compatibility.

These tests read the workflow file as data. Whether GitHub accepts it, and whether Google lets the
workflow authenticate, can only be shown by a hosted run (see docs/GITHUB_ACTIONS_SETUP.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from laptop_tracker import ci_config
from laptop_tracker import state_store as ss
from laptop_tracker.config import Settings
from laptop_tracker.run_gate import EXPECTED_STAGES
from laptop_tracker.sheets_source import READ_ONLY_SCOPES, READ_WRITE_SCOPES, load_credentials

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/weekly-laptop-tracker.yml"
sys.path.insert(0, str(ROOT / "scripts"))
from test_ci_gate import make_run  # noqa: E402
from test_ci_state import add_observation, make_root  # noqa: E402

import ci_plan  # noqa: E402
import ci_run_gate  # noqa: E402
import ci_state  # noqa: E402


@pytest.fixture(scope="module")
def workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML reads the bare key `on` as True
    return data


def all_steps(workflow):
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            yield name, step


# workflow structure ---------------------------------------------------------------------------


def test_untrusted_pull_request_code_never_runs_in_the_production_workflow(workflow) -> None:
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    text = WORKFLOW.read_text()
    assert not re.search(r"^\s*(pull_request|workflow_run)", text, re.M)


def test_the_schedule_is_monday_0917_manila(workflow) -> None:
    assert workflow["on"]["schedule"] == [{"cron": "17 9 * * 1", "timezone": "Asia/Manila"}]


def test_manual_runs_preview_by_default(workflow) -> None:
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["publish"]["default"] is False and inputs["publish"]["type"] == "boolean"
    assert inputs["accept_retailer_failures"]["default"] is False


def test_permissions_are_least_privilege(workflow) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        perms = job["permissions"]
        assert perms["contents"] == "read" and set(perms) <= {"contents", "id-token"}, name
    assert "id-token" not in workflow["jobs"]["plan"]["permissions"]


def test_long_jobs_have_a_three_hour_timeout_and_runs_never_overlap(workflow) -> None:
    assert workflow["jobs"]["collect"]["timeout-minutes"] == 180
    assert all("timeout-minutes" in job for job in workflow["jobs"].values())
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["concurrency"]["group"]


def test_every_action_is_pinned_to_a_full_commit_sha(workflow) -> None:
    uses = [s["uses"] for _, s in all_steps(workflow) if "uses" in s]
    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", reference), reference
    tests = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    for _, job in tests["jobs"].items():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"])


def test_checkout_does_not_persist_the_token(workflow) -> None:
    for _, step in all_steps(workflow):
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False


def test_the_pr_test_workflow_has_no_secrets_and_read_only_rights() -> None:
    text = (ROOT / ".github/workflows/tests.yml").read_text()
    tests = yaml.safe_load(text)
    assert tests["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "vars." not in text and "id-token" not in text


def test_inputs_reach_shell_only_through_environment_variables(workflow) -> None:
    for _, step in all_steps(workflow):
        script = step.get("run", "")
        assert "${{ inputs" not in script and "${{ github.event" not in script
        assert "${{ secrets" not in script


def test_secrets_are_never_echoed_and_none_are_required(workflow) -> None:
    text = WORKFLOW.read_text()
    assert "secrets." not in text  # Workload Identity Federation: no stored key at all
    assert not re.search(r"echo[^\n]*(GOOGLE_APPLICATION_CREDENTIALS|CREDENTIALS_JSON)", text)


def test_publication_happens_only_in_the_gated_publish_job(workflow) -> None:
    for name, step in all_steps(workflow):
        script = step.get("run", "")
        if "--write" in script:
            assert name == "publish"
    publish = workflow["jobs"]["publish"]
    assert "plan.outputs.publish == 'true'" in publish["if"]
    assert publish["needs"] == ["plan", "collect"] and publish["environment"] == "production"
    collect = workflow["jobs"]["collect"]
    assert not any("ci_publish" in s.get("run", "") for s in collect["steps"])


def test_state_is_locked_restored_saved_and_unlocked_even_on_failure(workflow) -> None:
    steps = {s.get("id"): s for s in workflow["jobs"]["collect"]["steps"] if s.get("id")}
    order = [s.get("id") or s.get("name") for s in workflow["jobs"]["collect"]["steps"]]
    assert (
        order.index("lock") < order.index("restore") < order.index("driver") < order.index("save")
    )
    assert "always()" in steps["save"]["if"] and "restore.outcome" in steps["save"]["if"]
    unlock = next(s for s in workflow["jobs"]["collect"]["steps"] if "unlock" in s.get("run", ""))
    assert "always()" in unlock["if"]
    assert steps["driver"]["continue-on-error"] is True  # so save/unlock run, then the job fails
    final = workflow["jobs"]["collect"]["steps"][-1]
    assert final["if"] == "always()" and "exit 1" in final["run"]


def test_only_sanitised_diagnostics_are_uploaded_briefly(workflow) -> None:
    upload = next(s for _, s in all_steps(workflow) if "upload-artifact" in s.get("uses", ""))
    paths = upload["with"]["path"].split()
    assert upload["with"]["retention-days"] <= 7
    assert all(
        Path(p).name in {"status.json", "stages.jsonl", "driver.log", "gate.json"} for p in paths
    )
    assert not any(p.endswith((".sqlite3", ".csv", ".tar.gz")) for p in paths)


def test_the_workflow_calls_the_driver_and_scripts_that_exist(workflow) -> None:
    for _, step in all_steps(workflow):
        for script in re.findall(r"scripts/[\w]+\.(?:py|sh)", step.get("run", "")):
            assert (ROOT / script).is_file(), script


def test_every_cli_subcommand_and_flag_the_workflow_uses_exists() -> None:
    text = WORKFLOW.read_text()
    for script in ("ci_state", "ci_run_gate", "ci_publish", "ci_plan"):
        help_text = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / f"{script}.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        used = re.findall(rf"scripts/{script}\.py([^\n]*(?:\\\n[^\n]*)*)", text)
        for command in used:
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]+", command):
                sub_help = help_text
                match = re.search(
                    r"\b(seed|save|versions|lock|restore|unlock|bundle-save|bundle-load|plan|check)\b",
                    command,
                )
                if match:
                    sub_help = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts" / f"{script}.py"),
                            match.group(1),
                            "--help",
                        ],
                        capture_output=True,
                        text=True,
                    ).stdout
                assert flag in help_text or flag in sub_help, f"{script} has no {flag}"


def test_the_drivers_stages_match_the_gate_and_the_driver_is_ci_safe() -> None:
    driver = (ROOT / "scripts/run_variation_week.sh").read_text()
    stages = re.findall(r"^stage (\w+) ([\d,]+) ", driver, re.M)
    assert [(n, tuple(int(c) for c in codes.split(","))) for n, codes in stages] == [
        (name, accepted) for name, accepted in EXPECTED_STAGES
    ]
    assert "READY_TO_PUBLISH" in driver
    assert "publish_variation_prices.py --write-sheets" not in driver  # the driver never writes
    assert "if [ -f .venv/bin/activate ]" in driver  # runs without a virtualenv in CI


def test_publication_uses_the_existing_publisher_not_a_second_one() -> None:
    script = (ROOT / "scripts/ci_publish.py").read_text()
    assert "import publish_variation_prices" in script
    assert "batch_update" not in script and "gspread" not in script


def test_no_commit_trailers_or_attribution_in_workflow_files() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        assert "Co-Authored-By" not in path.read_text()


# mode decisions -------------------------------------------------------------------------------


def test_mode_a_manual_preview_is_the_default() -> None:
    plan = ci_config.decide_mode("workflow_dispatch", {}, {})
    assert (plan.skip, plan.collect, plan.publish) == (False, True, False)
    plan = ci_config.decide_mode("workflow_dispatch", {"publish": "false"}, {})
    assert not plan.publish


def test_mode_b_manual_production_requires_the_explicit_option() -> None:
    plan = ci_config.decide_mode("workflow_dispatch", {"publish": "true"}, {})
    assert (plan.skip, plan.collect, plan.publish) == (False, True, True)
    assert "production" in plan.reason


def test_publishing_a_saved_run_skips_collection_but_requires_publish() -> None:
    plan = ci_config.decide_mode(
        "workflow_dispatch", {"publish": "true", "existing_run_id": "20261109T065857Z"}, {}
    )
    assert (plan.collect, plan.publish, plan.existing_run_id) == (False, True, "20261109T065857Z")
    refused = ci_config.decide_mode(
        "workflow_dispatch", {"publish": "false", "existing_run_id": "20261109T065857Z"}, {}
    )
    assert refused.skip and not refused.publish


@pytest.mark.parametrize(
    "variables",
    [
        {},
        {"WEEKLY_AUTOMATION": ""},
        {"WEEKLY_AUTOMATION": "off"},
        {"WEEKLY_AUTOMATION": "yes please"},
    ],
)
def test_mode_c_schedule_is_inert_until_it_is_switched_on(variables) -> None:
    plan = ci_config.decide_mode("schedule", {}, variables)
    assert plan.skip and not plan.publish and not plan.collect


def test_mode_c_schedule_can_run_as_preview_or_as_full_production() -> None:
    preview = ci_config.decide_mode("schedule", {}, {"WEEKLY_AUTOMATION": "preview"})
    assert (preview.skip, preview.collect, preview.publish) == (False, True, False)
    prod = ci_config.decide_mode("schedule", {}, {"WEEKLY_AUTOMATION": " PUBLISH "})
    assert (prod.skip, prod.collect, prod.publish) == (False, True, True)
    assert not prod.accept_retailer_failures  # scheduled runs never accept partial coverage


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target", "push", ""])
def test_other_events_never_run_the_pipeline(event) -> None:
    assert ci_config.decide_mode(event, {"publish": "true"}, {"WEEKLY_AUTOMATION": "publish"}).skip


def test_plan_script_writes_github_outputs(tmp_path) -> None:
    out = tmp_path / "out"
    code = ci_plan.main(
        ["plan"],
        {"EVENT_NAME": "schedule", "VAR_WEEKLY_AUTOMATION": "publish", "GITHUB_OUTPUT": str(out)},
    )
    assert code == 0
    values = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert values["publish"] == "true" and values["skip"] == "false" and values["collect"] == "true"


# missing Variables and Secrets ----------------------------------------------------------------


def test_missing_configuration_is_named_but_never_valued(capsys) -> None:
    code = ci_plan.main(["check", "--auth"], {"SPREADSHEET_ID": "sheet-123"})
    err = capsys.readouterr().err
    assert code == 1
    for name in (
        "GCS_STATE_BUCKET",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_SERVICE_ACCOUNT",
        "GOOGLE_USE_ADC",
    ):
        assert name in err
    assert "sheet-123" not in err


def test_complete_configuration_passes() -> None:
    env = {
        "SPREADSHEET_ID": "s",
        "GOOGLE_USE_ADC": "true",
        "GCS_STATE_BUCKET": "b",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": "p",
        "GCP_SERVICE_ACCOUNT": "a@b.iam",
    }
    assert ci_plan.main(["check", "--auth"], env) == 0
    assert ci_config.missing_settings({**env, "GOOGLE_USE_ADC": "false"}) == [
        "GOOGLE_USE_ADC (must be true)"
    ]


# authentication -------------------------------------------------------------------------------


def settings(**kw) -> Settings:
    base = dict(
        spreadsheet_id="sheet",
        raw_tab_name="raw",
        service_account_file=None,
        service_account_json=None,
        max_validation_errors=1,
        skip_freshness_check=False,
    )
    return Settings(**{**base, **kw})


def test_workload_identity_credentials_use_adc_with_least_privilege_scopes(monkeypatch) -> None:
    seen = []

    def fake_default(scopes=None):
        seen.append(list(scopes))
        return "adc-credentials", "project"

    monkeypatch.setattr("google.auth.default", fake_default)
    assert load_credentials(settings(use_adc=True), read_only=True) == "adc-credentials"
    assert load_credentials(settings(use_adc=True), read_only=False) == "adc-credentials"
    assert seen == [READ_ONLY_SCOPES, READ_WRITE_SCOPES]


def test_without_the_adc_opt_in_missing_credentials_still_fail_loudly() -> None:
    with pytest.raises(ValueError, match="GOOGLE_USE_ADC"):
        settings().require_sheet_access()


def test_local_key_file_configuration_is_unchanged(tmp_path) -> None:
    key = tmp_path / "key.json"
    key.write_text("{}")
    s = Settings.from_env(None)  # reads the real environment without .env
    assert s.use_adc is False
    settings(service_account_file=key).require_sheet_access()  # no ADC needed or used


def test_adc_is_opt_in_through_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_USE_ADC", "true")
    assert Settings.from_env(None).use_adc is True
    monkeypatch.setenv("GOOGLE_USE_ADC", "false")
    assert Settings.from_env(None).use_adc is False


# the scripts end to end on a local store ------------------------------------------------------


def test_state_script_seed_restore_save_lock_cycle(tmp_path) -> None:
    backend = ss.LocalBackend(tmp_path / "bucket")
    source = make_root(tmp_path)
    assert ci_state.main(["--root", str(source), "seed"], backend=backend) == 0
    runner = tmp_path / "runner"
    assert ci_state.main(["--root", str(runner), "restore"], backend=backend) == 0
    assert ci_state.main(["--root", str(runner), "lock", "--owner", "me"], backend=backend) == 0
    assert ci_state.main(["lock", "--owner", "you"], backend=backend) == 1  # held
    add_observation(runner / ss.STORE_REL, 77)
    assert ci_state.main(["--root", str(runner), "save", "--run-id", "r1"], backend=backend) == 0
    assert ci_state.main(["unlock", "--owner", "me"], backend=backend) == 0
    assert ci_state.main(["versions"], backend=backend) == 0
    assert len(ss.list_versions(backend)) == 2


def test_state_script_restore_without_seed_fails_with_a_clear_message(tmp_path, capsys) -> None:
    code = ci_state.main(
        ["--root", str(tmp_path / "r"), "restore"], backend=ss.LocalBackend(tmp_path / "bucket")
    )
    assert code == 1 and "seed" in capsys.readouterr().err


def test_second_run_starts_from_what_the_first_saved(tmp_path) -> None:
    backend = ss.LocalBackend(tmp_path / "bucket")
    ci_state.main(["--root", str(make_root(tmp_path)), "seed"], backend=backend)
    for n, name in enumerate(("run1", "run2"), start=1):
        runner = tmp_path / name
        assert ci_state.main(["--root", str(runner), "restore"], backend=backend) == 0
        add_observation(runner / ss.STORE_REL, 100 + n)
        assert (
            ci_state.main(["--root", str(runner), "save", "--run-id", name], backend=backend) == 0
        )
    final = ss.restore(backend, tmp_path / "final")
    assert final["rows"]["observations"] == 4  # 2 seeded + one per run, none lost


def test_gate_script_exit_codes_and_output_file(tmp_path) -> None:
    run = make_run(tmp_path)
    out = tmp_path / "gate.json"
    code = ci_run_gate.main(
        ["--run-dir", str(run), "--output", str(out), "--no-summary", "--max-age-hours", "100000"]
    )
    assert code == 0 and json.loads(out.read_text())["ok"] is True
    blocked = make_run(tmp_path, name="20261110T020000Z", preview_status="DESTINATION_REFUSED")
    assert (
        ci_run_gate.main(["--run-dir", str(blocked), "--no-summary", "--max-age-hours", "100000"])
        == 1
    )
