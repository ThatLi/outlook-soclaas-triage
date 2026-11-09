from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from outlook_triage import scheduler as s


def test_development_plan_uses_windowless_python_and_interactive_commands(tmp_path):
    root = tmp_path / 'Development path 含 spaces'
    plan = s.task_plan(root)
    assert Path(plan['executable']) == root / '.venv/Scripts/pythonw.exe'
    assert [t['arguments'] for t in plan['tasks']] == [
        '-m outlook_triage sync', '-m outlook_triage digest --telegram', '-m outlook_triage telegram-listen']
    assert plan['tasks'][0]['times'] == ['03:50', '07:50', '11:50', '15:50', '19:50', '23:50']
    assert plan['tasks'][1]['times'] == ['08:00']


def test_installed_plan_quotes_no_paths_in_arguments_and_names_are_scoped(tmp_path):
    root = tmp_path / 'Installed path 含 spaces'
    root.mkdir()
    (root / 'outlook-triage-background.exe').touch()
    plan = s.task_plan(root, cloud=True)
    assert Path(plan['executable']) == root / 'outlook-triage-background.exe'
    assert plan['tasks'] == [{'suffix': 'Cloud Bridge', 'arguments': 'bridge-listen', 'times': []}]
    assert s.task_plan(root, cloud=False)['prefix'] == plan['prefix']
    assert s.task_plan(tmp_path / 'different')['prefix'] != plan['prefix']
    assert s.task_plan(root, cloud=True, cache_previews=True)['tasks'][0]['arguments'].endswith('--cache-previews')


def collection():
    items = []
    def create(kind):
        item = NS(kind=kind)
        items.append(item)
        return item
    return NS(Create=create, items=items)


def fake_scheduler(monkeypatch, plan):
    definitions = []
    def new_task(_):
        task = NS(RegistrationInfo=NS(), Principal=NS(), Settings=NS(),
                  Actions=collection(), Triggers=collection())
        definitions.append(task)
        return task
    service = NS(NewTask=new_task)
    folder = Mock()
    monkeypatch.setattr(s, '_folder', lambda: (service, folder))
    monkeypatch.setattr(s, '_existing', lambda *a: None)
    return folder, definitions


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='pywin32 user identity')
def test_registered_tasks_have_correct_windows_settings(tmp_path, monkeypatch):
    root = tmp_path / 'Installed space 多'
    root.mkdir()
    (root / 'outlook-triage-background.exe').touch()
    plan = s.task_plan(root)
    folder, definitions = fake_scheduler(monkeypatch, plan)
    s.register_tasks(plan)
    assert folder.RegisterTaskDefinition.call_count == 3
    assert all(call.args[4] is None for call in folder.RegisterTaskDefinition.call_args_list)
    for definition in definitions:
        assert definition.Principal.LogonType == 3
        assert definition.Principal.RunLevel == 0
        assert definition.Settings.WakeToRun is False
        assert definition.Settings.StartWhenAvailable is True
        assert definition.Settings.MultipleInstances == 2
        assert definition.Actions.items[0].Path == plan['executable']
        assert definition.Actions.items[0].WorkingDirectory == str(root)
    assert definitions[0].Settings.ExecutionTimeLimit == 'PT2H'
    assert definitions[1].Settings.RestartInterval == 'PT30M'
    assert definitions[2].Settings.ExecutionTimeLimit == 'PT0S'
    assert definitions[2].Triggers.items[0].kind == 9
    assert not definitions[0].Triggers.items[0].StartBoundary.endswith('Z')
    folder.Run.assert_not_called()  # registration does not start listeners


def test_foreign_task_collision_aborts_before_any_mutation(tmp_path, monkeypatch):
    root = tmp_path
    (root / 'outlook-triage-background.exe').touch()
    plan = s.task_plan(root)
    folder, definitions = fake_scheduler(monkeypatch, plan)
    foreign = NS(Definition=NS(Actions=NS(Count=1, Item=lambda _: NS(Path='other.exe', WorkingDirectory='other'))))
    monkeypatch.setattr(s, '_existing', lambda *a: foreign)
    with pytest.raises(RuntimeError, match='another installation'):
        s.register_tasks(plan)
    assert definitions == []
    folder.RegisterTaskDefinition.assert_not_called()
    s.remove_tasks(plan)
    folder.DeleteTask.assert_not_called()


def test_remove_only_owned_tasks_and_stop_running_instances(tmp_path, monkeypatch):
    plan = s.task_plan(tmp_path)
    action = NS(Path=plan['executable'], WorkingDirectory=plan['root'])
    task = NS(Definition=NS(Actions=NS(Count=1, Item=lambda _: action)), State=4, Stop=Mock())
    folder, _ = fake_scheduler(monkeypatch, plan)
    monkeypatch.setattr(s, '_existing', lambda folder, name: task if name.endswith(' - Sync') else None)
    s.remove_tasks(plan)
    task.Stop.assert_called_once_with(0)
    folder.DeleteTask.assert_called_once_with(f"{plan['prefix']} - Sync", 0)


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='pywin32 COM exceptions')
def test_missing_task_handles_windows_dispatch_exception_without_hiding_denied_access():
    import pywintypes
    folder = Mock()
    folder.GetTask.side_effect = pywintypes.com_error(-2147352567, 'wrapped',
                                                     (0, None, None, None, 0, -2147024894), None)
    assert s._existing(folder, 'missing') is None
    folder.GetTask.side_effect = pywintypes.com_error(-2147352567, 'wrapped',
                                                     (0, None, None, None, 0, -2147024891), None)
    with pytest.raises(pywintypes.com_error):
        s._existing(folder, 'access denied')


@pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='Windows PowerShell required')
def test_source_scheduler_wrappers_share_plan_without_task_access(tmp_path):
    root = Path(__file__).parents[1]
    target = tmp_path / 'Separate path 多 spaces'
    target.mkdir()
    for script, extra in [('register_windows_tasks.ps1', ['-EnableTelegram', '-EnableTelegramCommands']),
                          ('register_cloud_bridge.ps1', [])]:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(root / 'scripts' / script),
                                 '-ProjectDir', str(target), '-ShowPlan', *extra], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=40)
        assert result.returncode == 0, result.stderr
        plan = json.loads(result.stdout)
        # PowerShell's legacy output encoding can replace non-ASCII redirected text;
        # the actual COM executable path is Unicode, without shell interpolation.
        assert plan['tasks'][0]['arguments'].startswith('-m outlook_triage ')
        assert plan['executable'].endswith('pythonw.exe')
