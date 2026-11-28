"""The GitHub Actions files, read as YAML: schedule, safety rules, and that every setting is wired."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobagent.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
NOT_SECRET = {"RANK_MODEL", "PROFILE_MODEL", "RANK_TOKEN_BUDGET"}  # repo variables, not secrets


def load(name: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    return workflow.get("on") or workflow[True]  # YAML 1.1 reads the bare key `on` as True


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


ALL = sorted(p.name for p in WORKFLOWS.glob("*.yml"))


def test_the_three_workflows_exist() -> None:
    assert ALL == ["ci.yml", "keepalive.yml", "run.yml"]


# --- run.yml -----------------------------------------------------------------------------------


def test_run_schedule_is_08_and_18_ist() -> None:
    assert triggers(load("run.yml"))["schedule"] == [{"cron": "30 2,12 * * *"}]


def test_manual_mode_defaults_to_dry_run() -> None:
    mode = triggers(load("run.yml"))["workflow_dispatch"]["inputs"]["mode"]
    assert mode["type"] == "choice"
    assert mode["options"] == ["dry-run", "send"]
    assert mode["default"] == "dry-run"


def test_scheduled_runs_send_and_manual_runs_use_the_input() -> None:
    env = load("run.yml")["jobs"]["run"]["env"]
    assert env["MODE"] == "${{ github.event_name == 'schedule' && 'send' || inputs.mode }}"
    [run_step] = [s for s in steps(load("run.yml")) if s.get("name") == "Run"]
    script = run_step["run"]
    assert '[ "$MODE" = "send" ]' in script
    assert "jobagent.jobs.run --dry-run" in script.split("else", 1)[1]  # anything but "send" is a dry run


def test_schedule_is_gated_on_jobagent_enabled() -> None:
    job = load("run.yml")["jobs"]["run"]
    assert job["if"] == "github.event_name == 'workflow_dispatch' || vars.JOBAGENT_ENABLED == 'true'"


def test_runs_never_overlap_and_are_never_cancelled_mid_send() -> None:
    workflow = load("run.yml")
    assert workflow["concurrency"] == {"group": "jobagent-run", "cancel-in-progress": False}
    assert workflow["jobs"]["run"]["timeout-minutes"] == 20


def test_run_migrates_before_running() -> None:
    commands = [s.get("run", "") for s in steps(load("run.yml"))]
    migrate = next(i for i, c in enumerate(commands) if "alembic upgrade head" in c)
    run = next(i for i, c in enumerate(commands) if "jobagent.jobs.run" in c)
    assert migrate < run


def test_every_setting_is_wired_into_the_run() -> None:
    env = load("run.yml")["jobs"]["run"]["env"]
    names = {name.upper() for name in Settings.model_fields}
    assert names <= set(env), f"not passed to the run: {sorted(names - set(env))}"
    for name in names:
        source = "vars" if name in NOT_SECRET else "secrets"
        assert env[name] == f"${{{{ {source}.{name} }}}}"


def test_no_artifact_upload_in_the_run() -> None:
    text = (WORKFLOWS / "run.yml").read_text()
    assert "upload-artifact" not in text
    code = [line.split("#", 1)[0] for line in text.splitlines()]  # comments may mention it
    assert not any("out/" in line for line in code)


def test_the_tiktoken_table_is_cached() -> None:
    [cache] = [s for s in steps(load("run.yml")) if "actions/cache@" in s.get("uses", "")]
    assert cache["with"]["path"] == ".cache/tiktoken"


# --- ci.yml ------------------------------------------------------------------------------------


def test_ci_runs_on_pull_requests_and_pushes_to_main() -> None:
    on = triggers(load("ci.yml"))
    assert "pull_request" in on
    assert on["push"] == {"branches": ["main"]}


def test_ci_checks_lint_types_and_tests_without_secrets() -> None:
    text = (WORKFLOWS / "ci.yml").read_text()
    for command in ("ruff check", "ruff format --check", "pyright", "pytest"):
        assert command in text
    assert "secrets." not in text  # safe for pull requests from forks; DB tests skip


# --- keepalive.yml -----------------------------------------------------------------------------


def test_keepalive_is_weekly_and_reenables_both_scheduled_workflows() -> None:
    workflow = load("keepalive.yml")
    [schedule] = triggers(workflow)["schedule"]
    assert schedule["cron"].split()[2:] == ["*", "*", "1"]  # every Monday
    assert workflow["permissions"] == {"actions": "write"}
    script = steps(workflow)[0]["run"]
    assert "for workflow in run.yml keepalive.yml" in script
    assert "actions/workflows/$workflow/enable" in script


# --- rules for every workflow --------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_actions_are_pinned_to_full_commit_shas(name: str) -> None:
    for step in steps(load(name)):
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


@pytest.mark.parametrize("name", ALL)
def test_permissions_are_explicit_and_minimal(name: str) -> None:
    permissions = load(name)["permissions"]
    assert set(permissions.values()) <= {"read", "write"}
    assert permissions in ({"contents": "read"}, {"actions": "write"})


@pytest.mark.parametrize("name", ALL)
def test_secrets_and_inputs_never_reach_a_shell_line(name: str) -> None:
    for step in steps(load(name)):
        script = step.get("run", "")
        assert "${{" not in script, f"expression inside a run script: {script!r}"  # env vars only
        assert not re.search(r"echo .*\$(DATABASE_URL|OPENAI|GMAIL|CV_BLOB|DIGEST_TO|HEALTHCHECK)", script)


@pytest.mark.parametrize("name", ALL)
def test_checkout_keeps_no_credentials(name: str) -> None:
    for step in steps(load(name)):
        if "actions/checkout@" in step.get("uses", ""):
            assert step["with"]["persist-credentials"] is False


# --- dependabot --------------------------------------------------------------------------------


def test_dependabot_updates_actions_and_uv_weekly() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert {(u["package-ecosystem"], u["schedule"]["interval"]) for u in config["updates"]} == {
        ("github-actions", "weekly"),
        ("uv", "weekly"),
    }
