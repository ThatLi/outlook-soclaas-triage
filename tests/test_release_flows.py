from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from outlook_triage import cli, diagnostics as d, onboarding as w, scheduler as s
from outlook_triage.config import ConfigurationError, save_settings
from outlook_triage.telegram import TelegramChat, TelegramError


def test_atomic_save_preserves_unrelated_settings_and_literals(tmp_path):
    path = tmp_path / '秘密 settings.env'
    path.write_text('# keep this\nUNRELATED=original\nTELEGRAM_CHAT_ID=old\n', encoding='utf-8')
    secret = "literal'\\${UNRELATED}#secret"
    save_settings(path, {'SOCLAAS_API_KEY': secret, 'TELEGRAM_CHAT_ID': 'confirmed'})
    assert '# keep this\nUNRELATED=original\n' in path.read_text(encoding='utf-8')
    assert dotenv_values(path, interpolate=False)['SOCLAAS_API_KEY'] == secret
    assert dotenv_values(path)['TELEGRAM_CHAT_ID'] == 'confirmed'


def test_interrupted_save_keeps_previous_file_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / 'secrets.env'
    path.write_text('KEY=old\n')
    monkeypatch.setattr('outlook_triage.config.os.replace', Mock(side_effect=OSError('interrupted')))
    with pytest.raises(OSError):
        save_settings(path, {'KEY': 'new'})
    assert path.read_text() == 'KEY=old\n'
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(ConfigurationError):
        save_settings(path, {'KEY': 'injected\nOTHER=bad'})


def test_private_chat_requires_number_and_explicit_confirmation(settings, monkeypatch):
    bot = Mock()
    bot.identity.return_value = {'username': 'my_test_bot'}
    bot.recent_private_chats.return_value = [TelegramChat('11', 'Other', 'private'),
                                           TelegramChat('22', 'Intended', 'private')]
    monkeypatch.setattr(w, 'TelegramClient', lambda *a, **k: bot)
    monkeypatch.setattr(w, 'check_webhook', lambda *a: None)
    answers = iter(['token', '1', '1', '1', '2', '1', '2', '1', '2'])
    monkeypatch.setattr(w, 'answer', lambda *a, **k: next(answers))
    saved = []
    monkeypatch.setattr(w, 'persist', lambda cfg, changes: saved.append(changes) or cfg)
    settings.state_dir.mkdir()
    w.telegram_step(settings)
    assert saved == [{'TELEGRAM_BOT_TOKEN': 'token', 'TELEGRAM_CHAT_ID': '22'}]
    assert bot.recent_private_chats.call_count == 2  # first chat was explicitly rejected
    bot.send_message.assert_not_called()


@pytest.mark.parametrize('cloud,url', [(False, 'https://elsewhere.example/hook'),
                                    (True, 'https://wrong.example/telegram/webhook')])
def test_webhook_conflicts_never_poll_or_remove(settings, monkeypatch, cloud, url):
    settings = replace(settings, cloud_bot_enabled=cloud, relay_url='https://expected.example',
                       relay_token='private', relay_encryption_key='private')
    bot = Mock()
    bot.identity.return_value = {'username': 'test'}
    bot.webhook_info.return_value = {'url': url}
    monkeypatch.setattr(w, 'TelegramClient', lambda *a, **k: bot)
    answers = iter(['token', '1'])
    monkeypatch.setattr(w, 'answer', lambda *a, **k: next(answers))
    with pytest.raises(RuntimeError):
        w.telegram_step(settings)
    bot.recent_private_chats.assert_not_called()
    bot.delete_webhook.assert_not_called()
    bot.set_webhook.assert_not_called()


def test_no_chat_can_wait_and_exit_without_saving(settings, monkeypatch):
    bot = Mock()
    bot.identity.return_value = {'username': 'test'}
    bot.recent_private_chats.return_value = []
    monkeypatch.setattr(w, 'TelegramClient', lambda *a, **k: bot)
    monkeypatch.setattr(w, 'check_webhook', lambda *a: None)
    answers = iter(['token', '1', '1'])
    def answer(*a, **k):
        value = next(answers, None)
        if value is None:
            raise w.Exit
        return value
    monkeypatch.setattr(w, 'answer', answer)
    persist = Mock()
    monkeypatch.setattr(w, 'persist', persist)
    settings.state_dir.mkdir()
    with pytest.raises(w.Exit):
        w.telegram_step(settings)
    persist.assert_not_called()


def test_invalid_numbers_cancel_and_hidden_retention(monkeypatch, capsys):
    values = iter(['bad', '999999999999999999', '', '2'])
    monkeypatch.setattr('builtins.input', lambda *a: next(values))
    assert w.choose('Pick', ['A', 'B']) == 2
    assert 'Enter one of the numbers' in capsys.readouterr().out
    monkeypatch.setattr(w, 'getpass', lambda *a: '')
    assert w.secret_value('Secret', 'do-not-print') == 'do-not-print'
    assert 'do-not-print' not in capsys.readouterr().out
    monkeypatch.setattr(w, 'getpass', lambda *a: 'Exit')
    with pytest.raises(w.Exit):
        w.secret_value('Secret', None)


@pytest.mark.parametrize('error', [TelegramError('token supersecret at C:/Users/Private', status_code=401),
                                 OSError('private@example.com 123456'), RuntimeError('secret')])
def test_connectivity_failures_are_sanitized(settings, monkeypatch, capsys, error):
    monkeypatch.setattr(w, 'secret_value', lambda *a: 'private-key')
    monkeypatch.setattr(w, 'SoCLaaSClient', Mock(side_effect=error))
    with pytest.raises(type(error)):
        w.soclaas_step(settings)
    d.display([d.Check('soclaas', 'FAIL', d.safe_failure(error))])
    text = capsys.readouterr().out
    assert 'supersecret' not in text and 'C:/Users/Private' not in text
    assert 'private@example.com' not in text


def test_saved_model_resume_without_reentering(settings, monkeypatch):
    client = Mock()
    client.list_models.return_value = [{'id': settings.model}, {'id': 'another'}]
    monkeypatch.setattr(w, 'SoCLaaSClient', lambda *a: client)
    answers = iter(['', '1'])
    monkeypatch.setattr('builtins.input', lambda *a: next(answers))
    monkeypatch.setattr(w, 'getpass', lambda *a: '')
    saved = Mock(return_value=settings)
    monkeypatch.setattr(w, 'persist', saved)
    w.soclaas_step(settings)
    saved.assert_called_once_with(settings, {'SOCLAAS_API_KEY': settings.soclaas_api_key,
                                            'EMAIL_TRIAGE_MODEL': settings.model})


def prepared_settings(settings):
    for path in (settings.config_dir, settings.data_dir, settings.state_dir, settings.reports_dir, settings.log_file.parent):
        path.mkdir(parents=True, exist_ok=True)
    settings.secrets_file.write_text('PRIVATE=must-stay-private')
    settings.rules_file.write_text('ignored_senders: []')
    settings.database_file.write_bytes(b'unchanged database; never open this')
    return replace(settings, telegram_bot_token='private-token', telegram_chat_id='123456')


def test_doctor_read_only_and_complete_missing_credentials(settings, monkeypatch):
    settings = prepared_settings(settings)
    before = {p: p.read_bytes() for p in settings.config_dir.parent.rglob('*') if p.is_file()}
    outlook = Mock()
    outlook.__enter__ = Mock(return_value=outlook)
    outlook.__exit__ = Mock()
    outlook.profile_metadata.return_value = {'profile': 'Default', 'mailbox': 'Private mailbox'}
    monkeypatch.setattr(d, 'OutlookClient', lambda *a, **k: outlook)
    monkeypatch.setattr(d, 'SoCLaaSClient', lambda *a: NS(verify_model=lambda: None))
    bot = NS(identity=lambda: {}, private_chat=lambda: None, webhook_info=lambda: {'url': ''})
    monkeypatch.setattr(d, 'TelegramClient', lambda *a, **k: bot)
    monkeypatch.setattr(d, 'windows_supported', lambda: True)
    monkeypatch.setattr(d, 'inspect_tasks', lambda *a: [])
    checks = d.collect(settings)
    assert all(c.status == 'PASS' for c in checks)
    after = {p: p.read_bytes() for p in settings.config_dir.parent.rglob('*') if p.is_file()}
    assert before == after
    outlook.newest_messages.assert_not_called()
    outlook.get_message.assert_not_called()
    outlook.mark_read.assert_not_called()
    # Real Telegram constructor fails on an empty token; each probe must isolate that failure.
    from outlook_triage.telegram import TelegramClient
    monkeypatch.setattr(d, 'TelegramClient', TelegramClient)
    checks = d.collect(replace(settings, telegram_bot_token=None), schedules=False)
    assert {c.name for c in checks if c.status == 'FAIL'} == {'configuration', 'telegram', 'webhook'}


def test_doctor_cli_bypasses_normal_initialization(settings, monkeypatch):
    monkeypatch.setattr(d, 'load_settings', lambda: settings)
    monkeypatch.setattr(d, 'collect', lambda *a: [d.Check('windows', 'PASS')])
    for name in ('load_settings', 'ensure_runtime_directories', 'configure_logging', 'Database'):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError('must not run')))
    assert cli.run(NS(command='doctor', pause=False)) == 0


def test_report_whitelist_and_optional_save(tmp_path):
    checks = [d.Check('telegram', 'FAIL', 'token email@example.com chat 998877665544 C:/Users/Private'),
              d.Check('soclaas', 'FAIL', 'HTTP 401'), d.Check('evil-private-name', 'FAIL', 'secret')]
    target = d.save_report(checks, tmp_path)
    text = target.read_text()
    assert not any(secret in text for secret in ['email@', '998877665544', 'C:/Users', 'secret', 'evil-private'])
    report = json.loads(text)
    assert report['checks'][0]['detail'] == ''
    assert report['checks'][1]['detail'] == 'HTTP 401'


def test_first_run_declined_does_not_classify_send_or_schedule(settings, monkeypatch):
    monkeypatch.setattr(w, 'load_settings', lambda: settings)
    monkeypatch.setattr(w, 'collect', lambda *a, **k: [])
    monkeypatch.setattr(w, 'choose', lambda *a, **k: 2)
    classify = Mock(side_effect=AssertionError('no test consent'))
    monkeypatch.setattr(w, 'SoCLaaSClient', classify)
    register = Mock(side_effect=AssertionError('no background consent'))
    monkeypatch.setattr(s, 'register_tasks', register)
    assert w.verify() == 130
    classify.assert_not_called()
    register.assert_not_called()


def test_failed_checks_block_background(settings, monkeypatch):
    monkeypatch.setattr(w, 'choose', lambda *a, **k: 1)
    monkeypatch.setattr(w, 'collect', lambda *a, **k: [d.Check('soclaas', 'FAIL')])
    register = Mock()
    monkeypatch.setattr(s, 'register_tasks', register)
    w.offer_background(settings)
    register.assert_not_called()


def test_verification_invalidated_by_connection_change(settings):
    settings.state_dir.mkdir()
    (settings.state_dir / 'first-run-verified').write_text(w.fingerprint(settings))
    assert w.verified(settings)
    assert not w.verified(replace(settings, telegram_chat_id='another-chat'))


@pytest.mark.parametrize('code,checks', [(0, [d.Check('windows', 'PASS')]),
                                     (1, [d.Check('soclaas', 'FAIL')]),
                                     (2, [d.Check('schedules', 'WARN')])])
def test_doctor_exit_codes(settings, monkeypatch, code, checks):
    monkeypatch.setattr(d, 'load_settings', lambda: settings)
    monkeypatch.setattr(d, 'collect', lambda *a: checks)
    assert d.doctor() == code


def test_shortcut_pause_runs_even_after_failure(monkeypatch):
    monkeypatch.setattr('sys.argv', ['outlook-triage', 'setup', '--pause'])
    monkeypatch.setattr(cli, 'run', Mock(side_effect=OSError('personal detail')))
    pause = Mock(return_value='')
    monkeypatch.setattr('builtins.input', pause)
    assert cli.main() == 1
    assert 'Press Enter to close' in pause.call_args.args[0]


def test_interrupted_setup_retains_completed_step_and_never_processes(settings, monkeypatch):
    monkeypatch.setattr(w, 'load_settings', lambda: settings)
    monkeypatch.setattr(w, 'windows_supported', lambda: True)
    monkeypatch.setattr(s, 'inspect_tasks', lambda *a: [])
    def completed_step(cfg):
        save_settings(cfg.secrets_file, {'UNRELATED': 'retained', 'OUTLOOK_PROFILE': 'Chosen'})
        return cfg
    def exit_step(cfg):
        raise w.Exit
    monkeypatch.setattr(w, 'outlook_step', completed_step)
    monkeypatch.setattr(w, 'soclaas_step', exit_step)
    monkeypatch.setattr(w, 'SoCLaaSClient', Mock(side_effect=AssertionError('no classification')))
    monkeypatch.setattr(s, 'register_tasks', Mock(side_effect=AssertionError('no scheduling')))
    assert w.setup() == 130
    assert dotenv_values(settings.secrets_file)['UNRELATED'] == 'retained'
    assert dotenv_values(settings.secrets_file)['OUTLOOK_PROFILE'] == 'Chosen'


def test_unconfirmed_receipt_cannot_enable_background(settings, monkeypatch):
    monkeypatch.setattr(w, 'load_settings', lambda: settings)
    monkeypatch.setattr(w, 'collect', lambda *a, **k: [])
    choices = iter([1, 2])
    monkeypatch.setattr(w, 'choose', lambda *a, **k: next(choices))
    ai, bot = Mock(), Mock()
    monkeypatch.setattr(w, 'SoCLaaSClient', lambda *a: ai)
    monkeypatch.setattr(w, 'TelegramClient', lambda *a, **k: bot)
    register = Mock()
    monkeypatch.setattr(s, 'register_tasks', register)
    assert w.verify() == 1
    ai.classify.assert_called_once()
    assert 'fictional' in ai.classify.call_args.args[0].body
    assert not w.verified(settings)
    register.assert_not_called()


def test_secrets_refuse_echoing_fallback(monkeypatch):
    import warnings
    from getpass import GetPassWarning
    def no_console(*a):
        warnings.warn('would echo', GetPassWarning)
        raise AssertionError('must stop before reading secret')
    monkeypatch.setattr(w, 'getpass', no_console)
    with pytest.raises(GetPassWarning):
        w.answer('secret', secret=True)
