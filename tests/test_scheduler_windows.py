from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "register_windows_tasks.ps1"
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
        "allowStartOnBatteries": True,
        "stopIfGoingOnBatteries": False,
        "wakeToRun": False,
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
    assert digest["restartCount"] == 0
    assert digest["restartInterval"] is None
    assert all(Path(task["workingDirectory"]) == project.resolve() for task in plan["tasks"])


def test_scheduler_plan_enables_telegram_without_mutating_tasks(tmp_path):
    project = _fake_project(tmp_path)
    plan = _show_plan("-ProjectDir", str(project), "-EnableTelegram")
    assert plan["telegramEnabled"] is True
    assert plan["tasks"][1]["arguments"] == "digest --telegram"
    assert "Telegram" in plan["tasks"][1]["description"]
    assert plan["tasks"][1]["restartCount"] == 47
    assert plan["tasks"][1]["restartInterval"] == "PT30M"


def test_scheduler_plan_resolves_default_project_directory():
    plan = _show_plan()
    assert Path(plan["projectDir"]) == PROJECT_ROOT.resolve()
    assert Path(plan["executable"]) == (PROJECT_ROOT / ".venv" / "Scripts" / "outlook-triage.exe").resolve()


def test_scheduler_plan_does_not_require_installed_executable(tmp_path):
    project = tmp_path / "project-without-venv"
    project.mkdir()

    plan = _show_plan("-ProjectDir", str(project))

    assert Path(plan["projectDir"]) == project.resolve()
    assert Path(plan["executable"]) == (project / ".venv" / "Scripts" / "outlook-triage.exe").resolve()


def test_scheduler_registration_requires_installed_executable_before_task_access(tmp_path):
    project = tmp_path / "project-without-venv"
    project.mkdir()

    completed = subprocess.run(
        [
            WINDOWS_POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ProjectDir",
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "The Windows virtual environment is missing" in completed.stderr
    compact_stderr = "".join(line.strip() for line in completed.stderr.splitlines())
    assert str(project / ".venv" / "Scripts" / "outlook-triage.exe") in compact_stderr
    assert "Get-ScheduledTask" not in completed.stderr


def test_scheduler_never_starts_a_task_immediately():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Start-ScheduledTask" not in source


def test_scheduler_uses_timed_catch_up_without_startup_or_logon_triggers():
    source = SCRIPT.read_text(encoding="utf-8")
    plan = _show_plan("-EnableTelegram")

    assert plan["settings"]["startWhenAvailable"] is True
    assert plan["settings"]["allowStartOnBatteries"] is True
    assert plan["settings"]["stopIfGoingOnBatteries"] is False
    assert plan["settings"]["wakeToRun"] is False
    assert plan["tasks"][1]["triggerTimes"] == ["08:00"]
    assert plan["tasks"][1]["arguments"] == "digest --telegram"
    assert "-AtStartup" not in source
    assert "-AtLogOn" not in source
    assert "-WakeToRun" not in source


def test_scheduler_targets_only_the_two_planned_task_names(tmp_path):
    project = _fake_project(tmp_path)
    plan = _show_plan("-ProjectDir", str(project), "-TaskPrefix", "Exact Prefix")
    assert [task["name"] for task in plan["tasks"]] == ["Exact Prefix - Sync", "Exact Prefix - Digest"]
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Get-ScheduledTask" not in source
    assert source.count("Register-ScheduledTask") == 2
