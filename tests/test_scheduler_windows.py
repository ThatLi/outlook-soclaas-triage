from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "register_windows_tasks.ps1"
SUPPORT_MODULE = PROJECT_ROOT / "scripts" / "OutlookTriage.Scheduler.psm1"
WINDOWS_POWERSHELL = shutil.which("powershell.exe")


pytestmark = pytest.mark.skipif(WINDOWS_POWERSHELL is None, reason="Windows PowerShell 5.1 is required")


def _show_plan(*arguments: str) -> dict:
    completed = subprocess.run(
        [
            WINDOWS_POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *arguments,
            "-ShowPlan",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def _fake_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    executable = project / ".venv" / "Scripts" / "outlook-triage.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    return project


def test_scheduler_plan_contains_complete_windows_task_definition(tmp_path):
    project = _fake_project(tmp_path)
    plan = _show_plan("-ProjectDir", str(project), "-TaskPrefix", "Test Outlook Triage")

    assert Path(plan["projectDir"]) == project.resolve()
    assert Path(plan["executable"]) == (project / ".venv" / "Scripts" / "outlook-triage.exe").resolve()
    assert plan["telegramEnabled"] is False
    assert plan["settings"] == {
        "startWhenAvailable": True,
        "multipleInstances": "IgnoreNew",
        "executionTimeLimit": "PT2H",
    }
    assert plan["principal"]["userId"]
    assert plan["principal"]["logonType"] == "Interactive"
    assert plan["principal"]["runLevel"] == "Limited"

    sync, digest = plan["tasks"]
    assert sync["name"] == "Test Outlook Triage - Sync"
    assert sync["arguments"] == "sync"
    assert sync["triggerTimes"] == ["03:50", "07:50", "11:50", "15:50", "19:50", "23:50"]
    assert digest["name"] == "Test Outlook Triage - Digest"
    assert digest["arguments"] == "digest"
    assert digest["triggerTimes"] == ["08:00"]
    assert all(Path(task["workingDirectory"]) == project.resolve() for task in plan["tasks"])


def test_scheduler_plan_enables_telegram_without_mutating_tasks(tmp_path):
    project = _fake_project(tmp_path)
    plan = _show_plan("-ProjectDir", str(project), "-EnableTelegram")
    assert plan["telegramEnabled"] is True
    assert plan["tasks"][1]["arguments"] == "digest --telegram"
    assert "Telegram" in plan["tasks"][1]["description"]


def test_scheduler_plan_resolves_default_project_directory():
    plan = _show_plan()
    assert Path(plan["projectDir"]) == PROJECT_ROOT.resolve()
    assert Path(plan["executable"]) == (PROJECT_ROOT / ".venv" / "Scripts" / "outlook-triage.exe").resolve()


def test_scheduler_never_starts_a_task_immediately():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Start-ScheduledTask" not in source


def _replacement_decision(actions: list[str], *, replace: bool = False) -> dict:
    environment = os.environ.copy()
    environment["OUTLOOK_TRIAGE_TEST_ACTIONS"] = json.dumps(actions)
    switch = " -ReplaceLegacyWslTasks" if replace else ""
    command = (
        f"Import-Module '{SUPPORT_MODULE}'; "
        "$decoded = ConvertFrom-Json $env:OUTLOOK_TRIAGE_TEST_ACTIONS; "
        f"Get-OutlookTriageReplacementDecision -ExistingActionExecutables @($decoded){switch} | "
        "ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [WINDOWS_POWERSHELL, "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("actions", [[], [r"C:\Tools\outlook-triage.exe"]])
def test_native_or_missing_scheduled_task_is_safe_to_replace(actions):
    assert _replacement_decision(actions) == {
        "allowed": True,
        "usesWsl": False,
        "reason": "no-legacy-wsl-action",
    }


@pytest.mark.parametrize(
    "execute",
    ["wsl", "wsl.exe", "WSL.EXE", r"C:\Windows\System32\wsl.exe", '"C:\\Windows\\System32\\wsl.exe"'],
)
def test_legacy_wsl_action_requires_explicit_replacement(execute):
    blocked = _replacement_decision([execute])
    assert blocked == {
        "allowed": False,
        "usesWsl": True,
        "reason": "legacy-wsl-replacement-required",
    }
    allowed = _replacement_decision([execute], replace=True)
    assert allowed == {
        "allowed": True,
        "usesWsl": True,
        "reason": "legacy-wsl-replacement-authorized",
    }


@pytest.mark.parametrize("execute", ["notwsl.exe", r"C:\Tools\wsl-helper.exe", "pwsh.exe", ""])
def test_non_wsl_action_names_are_not_false_positives(execute):
    assert _replacement_decision([execute])["usesWsl"] is False


def test_scheduler_targets_only_the_two_planned_task_names(tmp_path):
    project = _fake_project(tmp_path)
    plan = _show_plan("-ProjectDir", str(project), "-TaskPrefix", "Exact Prefix")
    assert [task["name"] for task in plan["tasks"]] == ["Exact Prefix - Sync", "Exact Prefix - Digest"]
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("Get-ScheduledTask") == 1
    assert source.count("Register-ScheduledTask") == 2
