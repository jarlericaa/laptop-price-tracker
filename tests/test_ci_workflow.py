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
from laptop_tracker import release_store as rs
from laptop_tracker import state_crypto as sc
from laptop_tracker import state_store as ss
from laptop_tracker.config import Settings
from laptop_tracker.run_gate import EXPECTED_STAGES
from laptop_tracker.sheets_source import READ_ONLY_SCOPES, READ_WRITE_SCOPES, load_credentials

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/weekly-laptop-tracker.yml"
sys.path.insert(0, str(ROOT / "scripts"))
from test_ci_gate import make_run  # noqa: E402
from test_ci_publish import PUB, FakeSheet, Publisher, good_report  # noqa: E402
from test_ci_state import add_observation, make_root  # noqa: E402

import ci_plan  # noqa: E402
import ci_publish as publish_script  # noqa: E402
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
    jobs = workflow["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read"}
    # Only the two jobs that store state / publication records may write, and only contents.
    assert jobs["collect"]["permissions"] == {"contents": "write"}
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert "id-token" not in WORKFLOW.read_text()  # no cloud federation any more


def test_no_google_cloud_dependency_remains() -> None:
    text = WORKFLOW.read_text()
    for forbidden in (
        "google-github-actions",
        "workload",
        "Workload",
        "GCS_",
        "GCP_",
        "gcloud",
        "GOOGLE_USE_ADC",
    ):
        assert forbidden not in text, forbidden
    assert "google-cloud" not in (ROOT / "requirements.txt").read_text()
    assert "google-cloud" not in (ROOT / "pyproject.toml").read_text()
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        assert "google.cloud" not in path.read_text(), path


def test_long_jobs_have_a_three_hour_timeout_and_runs_never_overlap(workflow) -> None:
    assert workflow["jobs"]["collect"]["timeout-minutes"] == 180
    assert all("timeout-minutes" in job for job in workflow["jobs"].values())
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["concurrency"]["group"]


def test_a_rerun_after_a_partial_attempt_is_an_explicit_default_off_manual_choice(workflow) -> None:
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["rerun_after_partial"]["type"] == "boolean"
    assert inputs["rerun_after_partial"]["default"] is False
    assert "schedule" in workflow["on"] and "inputs" not in workflow["on"]["schedule"][0]
    step = next(s for _, s in all_steps(workflow) if "scripts/ci_publish.py" in s.get("run", ""))
    # Reaches the shell only through the environment, and only the literal "true" confirms it.
    assert step["env"]["RERUN_AFTER_PARTIAL"] == "${{ inputs.rerun_after_partial }}"
    assert '[ "$RERUN_AFTER_PARTIAL" = "true" ]' in step["run"]
    assert "--allow-rerun-after-partial" in step["run"]
    assert "inputs." not in step["run"]


def test_the_publish_job_stays_gated_and_has_time_for_throttled_quota_waits(workflow) -> None:
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "production" and publish["runs-on"] == "ubuntu-24.04"
    assert 30 <= publish["timeout-minutes"] <= 180  # bounded, and room for ~2 min of waiting
    assert publish["permissions"] == {"contents": "write"}
    assert workflow["env"]["PYTHON_VERSION"] == "3.14"


def test_the_quota_layer_needs_only_packages_the_hosted_runner_installs() -> None:
    import ast

    tree = ast.parse((ROOT / "src/laptop_tracker/sheets_quota.py").read_text())
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    third_party = {m.split(".")[0] for m in imported if m} & {"requests", "google", "gspread"}
    installed = (ROOT / "requirements.txt").read_text()
    assert {"requests", "gspread", "google-auth"} <= set(re.findall(r"^[a-z-]+", installed, re.M))
    assert third_party == {"requests", "google", "gspread"}
    # Nothing outside the standard library and requirements.txt (no new service, no extra step).
    stdlib_only = imported - third_party - {"laptop_tracker", "__future__"}
    assert all(m in sys.stdlib_module_names for m in stdlib_only if m), stdlib_only


def test_automation_stays_off_unless_the_variable_says_otherwise() -> None:
    assert ci_config.decide_mode("schedule", {}, {}).skip
    assert ci_config.decide_mode("schedule", {}, {"WEEKLY_AUTOMATION": "off"}).skip


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


ALLOWED_SECRETS = {
    "GITHUB_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "STATE_ENCRYPTION_KEY",
    "STATE_ENCRYPTION_KEY_PREVIOUS",
}


def test_only_the_expected_secrets_are_used_and_only_in_step_environments(workflow) -> None:
    text = WORKFLOW.read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == ALLOWED_SECRETS
    assert "secrets." not in str(workflow.get("env", {}))  # never workflow-wide
    for name, job in workflow["jobs"].items():
        assert "secrets." not in str(job.get("env", {})), name  # never job-wide
        assert "secrets." not in str(job.get("outputs", {})), name
        for step in job["steps"]:
            assert "secrets." not in str(step.get("with", {}))
            assert "secrets." not in step.get("run", "")
    assert not re.search(r"echo[^\n]*(SERVICE_ACCOUNT_JSON|ENCRYPTION_KEY|GITHUB_TOKEN)", text)


def test_each_step_receives_only_the_secrets_it_needs(workflow) -> None:
    driver = next(s for s in workflow["jobs"]["collect"]["steps"] if s.get("id") == "driver")
    # The scraper handles untrusted web content: it gets Sheets access but neither the state key
    # nor a GitHub token.
    assert set(driver["env"]) == {"SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"}
    for _, step in all_steps(workflow):
        env = step.get("env", {})
        if "ci_state.py" in step.get("run", ""):
            assert "GITHUB_TOKEN" in env and "STATE_ENCRYPTION_KEY" in env
            assert "GOOGLE_SERVICE_ACCOUNT_JSON" not in env  # storage never needs Sheets access


def test_both_gates_share_one_policy_configuration_from_optional_variables(workflow) -> None:
    from laptop_tracker.retailer_failure_policy import ENV_NAMES

    env = workflow["env"]
    assert {k for k in env if k.startswith("RETAILER_FAILURE_")} == set(ENV_NAMES.values())
    for name in ENV_NAMES.values():
        assert env[name] == "${{ vars." + name + " }}"  # a Variable only: never a literal
    for _, step in all_steps(workflow):  # no step overrides them: collect and publish agree
        assert not any(k.startswith("RETAILER_FAILURE_") for k in step.get("env", {}))
    gates = [
        s
        for _, s in all_steps(workflow)
        if re.search(r"ci_(run_gate|publish)\.py", s.get("run", ""))
    ]
    assert len(gates) == 2  # the collect gate and the publish job's gate, each one script call
    # A warning is not a failure: only the gate step's exit code decides, and the collect job
    # fails (so publish is skipped) exactly when the gate, the pipeline, state or bundle fail.
    final = next(
        s for s in workflow["jobs"]["collect"]["steps"] if "critical step" in s.get("name", "")
    )
    assert '"$GATE" != "success"' in final["run"] and "decision" in final["run"]
    assert "needs.collect.result == 'success'" in workflow["jobs"]["publish"]["if"]


def test_no_step_installs_or_logs_in_to_a_cloud_cli(workflow) -> None:
    for _, step in all_steps(workflow):
        assert "gcloud" not in step.get("run", "")
        assert not step.get("uses", "").startswith("google-github-actions")


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


def test_state_is_restored_before_the_run_and_saved_even_on_failure(workflow) -> None:
    steps = {s.get("id"): s for s in workflow["jobs"]["collect"]["steps"] if s.get("id")}
    order = [s.get("id") or s.get("name") for s in workflow["jobs"]["collect"]["steps"]]
    assert order.index("restore") < order.index("driver") < order.index("save")
    assert "always()" in steps["save"]["if"] and "restore.outcome" in steps["save"]["if"]
    assert steps["driver"]["continue-on-error"] is True  # so save runs, then the job fails
    # Releases have no lease: one run at a time comes from the concurrency group, and saving
    # re-checks that the state did not move (see tests/test_ci_state.py).
    assert not any("lock" in str(s.get("run", "")) for s in workflow["jobs"]["collect"]["steps"])
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
                    r"\b(seed|save|versions|verify|rollback|report|restore|publication-fetch|bundle-save|bundle-load|plan|check)\b",
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


# missing and invalid Variables and Secrets ----------------------------------------------------


def service_account_json(email: str = "tracker@example-project.iam.gserviceaccount.com") -> str:
    """A syntactically valid service-account key made on the fly (never a real credential)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "example-project",
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": email,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


VALID_ENV = {"SPREADSHEET_ID": "sheet-123", "STATE_ENCRYPTION_KEY": sc.generate_key()}


def test_missing_configuration_is_named_but_never_valued(capsys) -> None:
    code = ci_plan.main(["check"], {"SPREADSHEET_ID": "sheet-123"})
    err = capsys.readouterr().err
    assert code == 1
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in err and "STATE_ENCRYPTION_KEY" in err
    assert "sheet-123" not in err
    assert "GCS" not in err and "WORKLOAD" not in err  # nothing from the old design is required


def test_complete_configuration_passes_and_names_the_service_account(capsys) -> None:
    sa = service_account_json()
    env = {**VALID_ENV, "GOOGLE_SERVICE_ACCOUNT_JSON": sa}
    assert ci_plan.main(["check"], env) == 0
    out = capsys.readouterr().out
    assert "tracker@example-project.iam.gserviceaccount.com" in out
    assert json.loads(sa)["private_key"] not in out and VALID_ENV["STATE_ENCRYPTION_KEY"] not in out


@pytest.mark.parametrize(
    "secret,expect",
    [
        ("not json at all", "not valid JSON"),
        ('{"type": "authorized_user"}', "not a service-account key"),
        ('{"type": "service_account", "client_email": "a@b"}', "lacks"),
    ],
)
def test_a_malformed_service_account_secret_is_rejected_without_echoing_it(
    secret, expect, capsys
) -> None:
    assert ci_plan.main(["check"], {**VALID_ENV, "GOOGLE_SERVICE_ACCOUNT_JSON": secret}) == 1
    err = capsys.readouterr().err
    assert expect in err and secret not in err


def test_a_malformed_encryption_key_is_rejected_without_echoing_it(capsys) -> None:
    env = {**VALID_ENV, "GOOGLE_SERVICE_ACCOUNT_JSON": service_account_json()}
    env["STATE_ENCRYPTION_KEY"] = "short-key"
    assert ci_plan.main(["check"], env) == 1
    err = capsys.readouterr().err
    assert "STATE_ENCRYPTION_KEY" in err and "short-key" not in err


# Google Sheets authentication through the Secret ----------------------------------------------


def test_the_json_secret_authenticates_with_least_privilege_scopes(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", service_account_json())
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.setenv("SPREADSHEET_ID", "sheet-123")
    loaded = Settings.from_env(None)
    settings_ok = loaded.service_account_file is None and loaded.service_account_json
    assert settings_ok
    loaded.require_sheet_access()
    read_only = load_credentials(loaded, read_only=True)
    writer = load_credentials(loaded, read_only=False)
    assert list(read_only.scopes) == READ_ONLY_SCOPES
    assert list(writer.scopes) == READ_WRITE_SCOPES
    assert read_only.service_account_email == "tracker@example-project.iam.gserviceaccount.com"


def test_no_credentials_file_is_written_by_the_secret_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", service_account_json())
    monkeypatch.chdir(tmp_path)
    load_credentials(Settings.from_env(None), read_only=True)
    assert list(tmp_path.iterdir()) == []  # nothing to protect or clean up on the runner


def test_a_broken_secret_fails_with_a_safe_message(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account", "x": 1}')
    with pytest.raises(Exception) as caught:  # SheetsSourceError, never the secret text
        load_credentials(Settings.from_env(None), read_only=True)
    assert "private_key" not in str(caught.value) and '"x"' not in str(caught.value)


def test_missing_credentials_still_fail_loudly() -> None:
    with pytest.raises(ValueError, match="GOOGLE_SERVICE_ACCOUNT"):
        settings().require_sheet_access()


def test_local_key_file_configuration_is_unchanged(tmp_path) -> None:
    key = tmp_path / "key.json"
    key.write_text("{}")
    settings(service_account_file=key).require_sheet_access()  # no secret needed locally


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


# the scripts end to end on a local store ------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return rs.LocalReleaseStore(tmp_path / "releases"), sc.KeyRing(sc.parse_key(sc.generate_key()))


def cli(args, storage, root=None):
    store, ring = storage
    return ci_state.main([*(["--root", str(root)] if root else []), *args], store=store, ring=ring)


def test_state_script_seed_restore_save_verify_rollback_cycle(
    tmp_path, storage, capsys, monkeypatch
) -> None:
    # GitHub runners set this, which sends the report summary to a file instead of stdout.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    source = make_root(tmp_path)
    assert cli(["seed"], storage, source) == 0
    runner = tmp_path / "runner"
    assert cli(["restore"], storage, runner) == 0
    add_observation(runner / ss.STORE_REL, 77)
    assert cli(["save", "--run-id", "20261109T010000Z"], storage, runner) == 0
    assert cli(["versions"], storage) == 0 and cli(["verify"], storage) == 0
    assert cli(["report"], storage) == 0
    assert "Stored tracker data" in capsys.readouterr().out
    assert cli(["rollback", "--to", "1"], storage) == 0
    assert [v["seq"] for v in ss.list_versions(storage[0])] == [1, 2, 3]


def test_state_script_restore_without_seed_fails_with_a_clear_message(
    tmp_path, storage, capsys
) -> None:
    assert cli(["restore"], storage, tmp_path / "r") == 1
    assert "seed" in capsys.readouterr().err


def test_state_script_without_a_key_fails_and_never_creates_state(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("STATE_LOCAL_DIR", str(tmp_path / "s"))
    monkeypatch.delenv("STATE_ENCRYPTION_KEY", raising=False)
    assert ci_state.main(["--root", str(make_root(tmp_path)), "seed"]) == 1
    assert "STATE_ENCRYPTION_KEY" in capsys.readouterr().err
    assert not list((tmp_path / "s").glob("*/release.json"))


def test_keygen_writes_a_private_file_and_prints_only_the_key_id(tmp_path, capsys) -> None:
    out = tmp_path / "state.key"
    assert ci_state.main(["keygen", "--out", str(out)]) == 0
    key = out.read_text().strip()
    printed = capsys.readouterr().out
    assert key not in printed and json.loads(printed)["key_id"] == sc.key_id(sc.parse_key(key))
    assert ci_state.main(["keygen", "--out", str(out)]) == 1  # never overwrites a key
    assert out.read_text().strip() == key


def test_second_run_starts_from_what_the_first_saved(tmp_path, storage) -> None:
    cli(["seed"], storage, make_root(tmp_path))
    for n, name in enumerate(("20261102T010000Z", "20261109T010000Z"), start=1):
        runner = tmp_path / f"run{n}"
        assert cli(["restore"], storage, runner) == 0
        add_observation(runner / ss.STORE_REL, 100 + n)
        assert cli(["save", "--run-id", name], storage, runner) == 0
    final = ss.restore(*storage, tmp_path / "final")
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


# the three modes, end to end over the local store ----------------------------------------------


def publish_args(run, *extra):
    return ["--run-dir", str(run), "--max-age-hours", "100000", *extra]


def test_scheduled_production_flow_restore_collect_save_publish_verify_next_week(
    tmp_path, storage
) -> None:
    """Mode C: restore, a collected run, state save, bundle, publish from the bundle in a fresh
    workspace (as the separate publish job does), the record, and the next week's restore."""
    cli(["seed"], storage, make_root(tmp_path))
    plan = ci_config.decide_mode("schedule", {}, {"WEEKLY_AUTOMATION": "publish"})
    assert plan.collect and plan.publish and not plan.skip

    runner = tmp_path / "collect_job"
    assert cli(["restore"], storage, runner) == 0
    run = make_run(tmp_path)  # stands in for the driver's output folder
    add_observation(runner / ss.STORE_REL, 55)
    assert (
        ci_run_gate.main(["--run-dir", str(run), "--no-summary", "--max-age-hours", "100000"]) == 0
    )
    assert cli(["save", "--run-id", run.name], storage, runner) == 0
    assert cli(["bundle-save", "--run-dir", str(run)], storage) == 0

    publish_workspace = tmp_path / "publish_job" / run.name  # a different, empty machine
    assert (
        cli(["bundle-load", "--run-id", run.name, "--dest", str(publish_workspace)], storage) == 0
    )
    publisher = Publisher(tmp_path, report=good_report())
    sheet = FakeSheet()
    code = publish_script.main(
        publish_args(publish_workspace, "--write"),
        store=storage[0],
        ring=storage[1],
        publisher=publisher,
        open_spreadsheet=lambda read_only: sheet,
        sleep=lambda s: None,
    )
    assert code == 0 and len(publisher.writes()) == 1
    assert [r["status"] for r in ss.read_publication_attempts(*storage, PUB)] == ["PUBLISHED"]

    nextweek = ss.restore(*storage, tmp_path / "next_week")
    assert nextweek["rows"]["observations"] == 3 and nextweek["seq"] == 2


def test_manual_preview_mode_saves_state_but_never_publishes(tmp_path, storage) -> None:
    plan = ci_config.decide_mode("workflow_dispatch", {}, {})
    assert plan.collect and not plan.publish
    cli(["seed"], storage, make_root(tmp_path))
    runner = tmp_path / "collect_job"
    cli(["restore"], storage, runner)
    add_observation(runner / ss.STORE_REL, 55)
    assert cli(["save", "--run-id", "20261109T010000Z"], storage, runner) == 0
    assert ss.restore(*storage, tmp_path / "again")["rows"]["observations"] == 3
    assert storage[0].list_releases("tracker-pub-") == []  # no publication record exists


def test_manual_production_can_publish_an_existing_run_without_collecting(
    tmp_path, storage
) -> None:
    run = make_run(tmp_path)
    ss.save_bundle(*storage, run, run.name, {})
    plan = ci_config.decide_mode(
        "workflow_dispatch", {"publish": "true", "existing_run_id": run.name}, {}
    )
    assert not plan.collect and plan.publish and plan.existing_run_id == run.name
    out = tmp_path / "fresh" / run.name
    ss.load_bundle(*storage, run.name, out)
    publisher = Publisher(tmp_path, report=good_report())
    code = publish_script.main(
        publish_args(out, "--write"),
        store=storage[0],
        ring=storage[1],
        publisher=publisher,
        open_spreadsheet=lambda read_only: FakeSheet(),
        sleep=lambda s: None,
    )
    assert (
        code == 0 and ss.published_versions(storage[0]) == []
    )  # collected nothing, saved no state
