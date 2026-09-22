"""Les échecs intermédiaires ne deviennent pas des succès utilisateur."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from actions import browser_control as bc, computer_control as cc, reminder as reminders


def test_failed_typing_never_presses_enter(monkeypatch):
    monkeypatch.setattr(cc, '_type_text', lambda text: 'Aucun outil de saisie disponible')
    press = Mock()
    monkeypatch.setattr(cc, '_press_key', press)
    result = cc.computer_control({'action': 'type', 'text': 'codex', 'press_enter': True})
    press.assert_not_called()
    assert 'validé par Entrée' not in result


def test_failed_enter_is_reported(monkeypatch):
    monkeypatch.setattr(cc, '_type_text', lambda text: 'Texte tapé : codex')
    monkeypatch.setattr(cc, '_press_key', lambda key: 'Aucun outil de clavier')
    assert 'non validée' in cc.computer_control({'action': 'type', 'text': 'codex', 'press_enter': True})


def test_missing_window_prevents_typing(monkeypatch):
    monkeypatch.setattr(cc, '_focus_window', lambda target: 'Aucune fenêtre trouvée')
    typed = Mock()
    monkeypatch.setattr(cc, '_type_text', typed)
    result = cc.computer_control({'action': 'type', 'text': 'codex', 'window': 'kitty'})
    typed.assert_not_called()
    assert 'annulée' in result


def test_healthy_browser_probe_preserves_page(monkeypatch):
    session = bc.BrowserSession('chrome')
    class Page:
        def is_closed(self): return False
        async def evaluate(self, expression): return 1  # vraie signature Playwright
    page = Page()
    session._page = page
    session._context = SimpleNamespace(browser=None)
    relaunch = AsyncMock()
    monkeypatch.setattr(session, '_relaunch_disconnected_browser', relaunch)
    asyncio.run(session._ensure_context())
    relaunch.assert_not_called()
    assert session._page is page


def test_browser_protocol_failure_retries_only_once(monkeypatch):
    session = bc.BrowserSession('chrome')
    session._context = SimpleNamespace(browser=None, pages=[], new_page=AsyncMock(side_effect=RuntimeError('Protocol error')))
    relaunch = AsyncMock()
    monkeypatch.setattr(session, '_relaunch_disconnected_browser', relaunch)
    import pytest
    with pytest.raises(RuntimeError, match='après une tentative'):
        asyncio.run(session._ensure_context())
    assert relaunch.await_count == 1


def _registry(monkeypatch, tmp_path):
    tomorrow = datetime.now() + timedelta(days=1)
    entries = [dict(task_name='later', message='second', datetime=(tomorrow + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), os='linux', scheduler='systemd', job_id='later'),
               dict(task_name='earlier', message='first', datetime=tomorrow.strftime('%Y-%m-%d %H:%M'), os='linux', scheduler='systemd', job_id='earlier')]
    monkeypatch.setattr(reminders, '_load_registry', lambda: entries)
    monkeypatch.setattr(reminders, '_scripts_dir', lambda: tmp_path)
    monkeypatch.setattr(reminders, '_events_dir', lambda: tmp_path)
    remove = Mock()
    monkeypatch.setattr(reminders, '_unregister_reminder', remove)
    return remove


def test_cancel_number_matches_display_order_and_stops_timer(monkeypatch, tmp_path):
    remove = _registry(monkeypatch, tmp_path)
    run = Mock(side_effect=lambda argv, **kwargs: SimpleNamespace(
        returncode=3 if 'is-active' in argv else 0,
        stdout='inactive' if 'is-active' in argv else '',
    ))
    monkeypatch.setattr(reminders.kit, 'run', run)
    assert 'first' in reminders._cancel_reminder('1')
    assert run.call_args_list[0].args[0] == ['systemctl', '--user', 'stop', 'earlier.timer', 'earlier.service']
    remove.assert_called_once_with('earlier')


def test_failed_cancel_keeps_registry_and_script(monkeypatch, tmp_path):
    remove = _registry(monkeypatch, tmp_path)
    script = tmp_path / 'earlier.py'
    script.write_text('reminder')
    monkeypatch.setattr(reminders.kit, 'run', Mock(return_value=SimpleNamespace(returncode=1)))
    assert 'non confirmée' in reminders._cancel_reminder('1')
    remove.assert_not_called()
    assert script.exists()


def test_failed_clipboard_paste_is_not_reported_as_typing(monkeypatch):
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda name: False)
    monkeypatch.setattr(cc, '_clipboard_copy', lambda text: True)
    monkeypatch.setattr(cc, '_clipboard_paste', lambda: 'Impossible de coller')
    assert 'Échec' in cc._type_text('codex')


def test_cdp_recovery_does_not_close_user_context(monkeypatch):
    session = bc.BrowserSession('chrome')
    context = SimpleNamespace(close=AsyncMock())
    session._context = context
    session._is_cdp = True
    session._pw = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(session, '_init_playwright', AsyncMock())
    asyncio.run(session._relaunch_disconnected_browser())
    context.close.assert_not_awaited()


def test_active_timer_never_counts_as_cancelled(monkeypatch, tmp_path):
    remove = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(reminders.kit, 'run', Mock(return_value=SimpleNamespace(returncode=0, stdout='active')))
    assert 'non confirmée' in reminders._cancel_reminder('1')
    remove.assert_not_called()
