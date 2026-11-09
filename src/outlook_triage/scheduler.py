"""Task Scheduler integration shared by setup, diagnostics and source scripts."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


SUFFIXES = ("Sync", "Digest", "Telegram Commands", "Cloud Bridge")


def installation_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]


def task_prefix(root: Path) -> str:
    identity = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:12]
    return f"Outlook SoCLaaS Triage {identity}"


def task_plan(root: Path | None = None, *, cloud=False, telegram=True, listener=True,
              prefix: str | None = None, cache_previews=False) -> dict:
    root = (root or installation_dir()).resolve()
    installed = bool(getattr(sys, "frozen", False)) or (root / "outlook-triage-background.exe").is_file()
    executable = root / "outlook-triage-background.exe" if installed else root / ".venv/Scripts/pythonw.exe"
    leading = "" if installed else "-m outlook_triage "
    prefix = prefix or task_prefix(root)
    tasks = []
    if cloud:
        tasks.append({"suffix": "Cloud Bridge", "arguments": leading + "bridge-listen" +
                      (" --cache-previews" if cache_previews else ""), "times": []})
    else:
        tasks.append({"suffix": "Sync", "arguments": leading + "sync",
                      "times": ["03:50", "07:50", "11:50", "15:50", "19:50", "23:50"]})
        tasks.append({"suffix": "Digest", "arguments": leading + "digest" + (" --telegram" if telegram else ""),
                      "times": ["08:00"]})
        if listener:
            tasks.append({"suffix": "Telegram Commands", "arguments": leading + "telegram-listen", "times": []})
    return {"root": str(root), "executable": str(executable), "prefix": prefix, "tasks": tasks}


def _folder():
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service, service.GetFolder("\\")


def _existing(folder, name):
    import pywintypes
    try:
        return folder.GetTask(name)
    except pywintypes.com_error as exc:
        code = exc.excepinfo[5] if exc.excepinfo and len(exc.excepinfo) > 5 else exc.hresult
        if code == -2147024894:  # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND), sometimes wrapped by IDispatch
            return None
        raise


def _owned(task, plan):
    actions = task.Definition.Actions
    if actions.Count != 1:
        return False
    action = actions.Item(1)
    return (os.path.normcase(action.Path) == os.path.normcase(plan["executable"])
            and os.path.normcase(action.WorkingDirectory) == os.path.normcase(plan["root"]))


def inspect_tasks(plan: dict) -> list[dict]:
    _, folder = _folder()
    results = []
    for spec in plan["tasks"]:
        task = _existing(folder, f"{plan['prefix']} - {spec['suffix']}")
        owned = bool(task and _owned(task, plan))
        results.append({"task": spec["suffix"], "registered": bool(task), "owned": owned,
                        "enabled": bool(task and task.Enabled),
                        "arguments_match": bool(owned and task.Definition.Actions.Item(1).Arguments == spec["arguments"]),
                        "running": bool(task and task.State == 4),
                        "last_result": int(task.LastTaskResult) if task else None,
                        "executable_exists": Path(plan["executable"]).is_file()})
    return results


def remove_tasks(plan: dict) -> None:
    _, folder = _folder()
    for suffix in SUFFIXES:
        name = f"{plan['prefix']} - {suffix}"
        task = _existing(folder, name)
        if task and _owned(task, plan):
            if task.State in (2, 4):
                task.Stop(0)
            folder.DeleteTask(name, 0)


def register_tasks(plan: dict) -> None:
    if not Path(plan["executable"]).is_file():
        raise RuntimeError("Background program is missing. Reinstall the application or recreate the development environment.")
    service, folder = _folder()
    # Validate every name before changing any task; never replace another installation.
    existing = {}
    for suffix in SUFFIXES:
        task = _existing(folder, f"{plan['prefix']} - {suffix}")
        if task and not _owned(task, plan):
            raise RuntimeError("A background task belongs to another installation. Use that installation to stop it first.")
        existing[suffix] = task
    import win32api
    import win32con
    user = win32api.GetUserNameEx(win32con.NameSamCompatible)
    wanted = {spec["suffix"] for spec in plan["tasks"]}
    for suffix, task in existing.items():
        if task:
            if task.State in (2, 4):
                task.Stop(0)
            if suffix not in wanted:
                task.Enabled = False
    for spec in plan["tasks"]:
        definition = service.NewTask(0)
        definition.RegistrationInfo.Description = "Outlook Triage: signed-in user only; local triggers use Windows time."
        definition.Principal.UserId = user
        definition.Principal.LogonType = 3  # interactive token; never store a password
        definition.Principal.RunLevel = 0
        settings = definition.Settings
        settings.Enabled = True
        settings.StartWhenAvailable = True
        settings.DisallowStartIfOnBatteries = False
        settings.StopIfGoingOnBatteries = False
        settings.WakeToRun = False
        settings.MultipleInstances = 2  # IgnoreNew
        settings.ExecutionTimeLimit = "PT2H" if spec["times"] else "PT0S"
        if not spec["times"] or "--telegram" in spec["arguments"]:
            settings.RestartCount = 47 if spec["times"] else 999
            settings.RestartInterval = "PT30M" if spec["times"] else "PT1M"
        action = definition.Actions.Create(0)
        action.Path = plan["executable"]
        action.Arguments = spec["arguments"]
        action.WorkingDirectory = plan["root"]
        if spec["times"]:
            for clock in spec["times"]:
                trigger = definition.Triggers.Create(2)  # daily, no UTC offset: Windows local time
                trigger.StartBoundary = f"2026-01-01T{clock}:00"
                trigger.DaysInterval = 1
                trigger.Enabled = True
        else:
            trigger = definition.Triggers.Create(9)  # logon
            trigger.UserId = user
            trigger.Enabled = True
        folder.RegisterTaskDefinition(f"{plan['prefix']} - {spec['suffix']}", definition, 6, user, None, 3)
    # Registration never starts processing. Setup's separate final confirmation starts listeners.


def start_listeners(plan: dict) -> None:
    _, folder = _folder()
    for spec in plan["tasks"]:
        if not spec["times"]:
            task = folder.GetTask(f"{plan['prefix']} - {spec['suffix']}")
            if not _owned(task, plan):
                raise RuntimeError("Background task ownership changed; reopen Troubleshooting.")
            task.Run("")


def script_main(args) -> int:
    plan = task_plan(Path(args.project_dir) if args.project_dir else None, cloud=args.cloud,
                     telegram=args.telegram, listener=args.listener, prefix=args.task_prefix,
                     cache_previews=args.cache_previews)
    if args.action == "plan":
        print(json.dumps(plan, indent=2))
    elif args.action == "status":
        print(json.dumps(inspect_tasks(plan), indent=2))
    elif args.action == "stop":
        remove_tasks(plan)
        print("Background tasks owned by this installation removed. Settings and data retained.")
    else:
        from .config import load_settings, ConfigurationError
        settings = load_settings()
        if settings.cloud_bot_enabled != args.cloud:
            raise ConfigurationError("The selected schedule does not match the configured local/cloud mode.")
        from .diagnostics import collect
        if any(row.status == "FAIL" for row in collect(settings, schedules=False)):
            raise ConfigurationError("Required checks failed. Open Troubleshooting before registering schedules.")
        from .onboarding import verified
        if not verified(settings):
            raise ConfigurationError("Complete the separate First-run Check before enabling regular processing.")
        register_tasks(plan)
        print("Background tasks registered. They start at the next scheduled time or Windows sign-in.")
    return 0
