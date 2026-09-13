"""
test_computer_control.py — Tests unitaires et TDD pour actions/computer_control.py.

Couvre :
  1. Action 'type' avec texte, press_enter=True et ciblage de fenêtre (window/title).
  2. Action 'move' avec curseur Hyprland Lua hl.dsp.cursor.move et repli ydotool.
  3. Nouvelles actions de fenêtres : fullscreen, float, center, close (et alias).
  4. Actions 'press' et 'hotkey' avec mapping keycodes Linux pour ydotool key.
  5. Fallback d'écriture _type_text (ydotool -> wtype -> clipboard paste).
  6. Parsing local en langage naturel (_parse_control_locally).
"""
import subprocess

from actions.computer_control import (
    computer_control,
    _parse_control_locally,
)
import actions.computer_control as cc


# ════════════════════════════════════════════════════════════════════════════
# 1. Action 'type' : press_enter et ciblage de fenêtre (window / title)
# ════════════════════════════════════════════════════════════════════════════

def test_type_with_press_enter(monkeypatch):
    """Vérifie que l'action type avec press_enter=True tape le texte puis presse Entrée."""
    calls = []
    monkeypatch.setattr(cc, '_type_text', lambda text: calls.append(('type', text)) or f'Texte tapé : {text}')
    monkeypatch.setattr(cc, '_press_key', lambda key: calls.append(('press', key)) or f'Touche pressée : {key}')

    res = computer_control({'action': 'type', 'text': 'codex', 'press_enter': True})

    assert ('type', 'codex') in calls
    assert ('press', 'enter') in calls or ('press', 'Return') in calls
    # L'ordre doit être : d'abord la saisie, ensuite Entrée
    assert calls.index(('type', 'codex')) < calls.index(calls[-1])
    assert 'codex' in res


def test_type_with_window_target(monkeypatch):
    """Vérifie que spécifier window ou title appelle d'abord _focus_window avant la saisie."""
    calls = []
    monkeypatch.setattr(cc, '_focus_window', lambda win: calls.append(('focus', win)) or f'Fenêtre {win} focalisée')
    monkeypatch.setattr(cc, '_type_text', lambda text: calls.append(('type', text)) or f'Texte tapé : {text}')
    monkeypatch.setattr(cc, '_press_key', lambda key: calls.append(('press', key)) or f'Touche pressée : {key}')

    computer_control({'action': 'type', 'text': 'ls', 'window': 'kitty'})

    assert ('focus', 'kitty') in calls
    assert ('type', 'ls') in calls
    # Le focus doit précéder la saisie
    assert calls.index(('focus', 'kitty')) < calls.index(('type', 'ls'))


def test_type_with_title_and_enter(monkeypatch):
    """Vérifie le combo complet title + text + enter."""
    calls = []
    monkeypatch.setattr(cc, '_focus_window', lambda win: calls.append(('focus', win)) or f'Fenêtre {win} focalisée')
    monkeypatch.setattr(cc, '_type_text', lambda text: calls.append(('type', text)) or f'Texte tapé : {text}')
    monkeypatch.setattr(cc, '_press_key', lambda key: calls.append(('press', key)) or f'Touche pressée : {key}')

    computer_control({'action': 'type', 'text': 'git status', 'title': 'alacritty', 'enter': True})

    assert ('focus', 'alacritty') in calls
    assert ('type', 'git status') in calls
    assert any(c[0] == 'press' for c in calls)


# ════════════════════════════════════════════════════════════════════════════
# 2. Action 'move' : déplacement curseur via Hyprland Lua et repli ydotool
# ════════════════════════════════════════════════════════════════════════════

def test_move_cursor_hyprland_lua(monkeypatch):
    """Sous Wayland/Hyprland, move doit tenter hl.dsp.cursor.move({ x = 500, y = 300 })."""
    dispatched = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd in ('hyprctl', 'ydotool'))

    def mock_dispatch(legacy_cmd='', legacy_args='', lua_cmd=''):
        dispatched.append({'legacy_cmd': legacy_cmd, 'legacy_args': legacy_args, 'lua_cmd': lua_cmd})
        return True

    if hasattr(cc, '_hypr_dispatch_hyprland'):
        monkeypatch.setattr(cc, '_hypr_dispatch_hyprland', mock_dispatch)
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': dispatched.append({'disp': disp, 'arg': arg}) or True)

    res = computer_control({'action': 'move', 'x': 500, 'y': 300})
    assert '500' in res and '300' in res
    assert any(
        'hl.dsp.cursor.move' in str(d) and '500' in str(d) and '300' in str(d)
        for d in dispatched
    )


def test_move_cursor_ydotool_fallback(monkeypatch):
    """Si Hyprland n'est pas dispo ou échoue, fallback sur ydotool mousemove -a."""
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'ydotool')  # pas de hyprctl
    if hasattr(cc, '_hypr_dispatch_hyprland'):
        monkeypatch.setattr(cc, '_hypr_dispatch_hyprland', lambda *a, **kw: False)
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda *a, **kw: False)

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    res = computer_control({'action': 'move', 'x': 200, 'y': 150})
    assert '200' in res and '150' in res
    assert any('ydotool' in cmd and 'mousemove' in cmd and '-a' in cmd for cmd in run_cmds)


# ════════════════════════════════════════════════════════════════════════════
# 3. Nouvelles actions de fenêtres : fullscreen, float, center, close
# ════════════════════════════════════════════════════════════════════════════

def test_window_action_fullscreen(monkeypatch):
    """Vérifie l'action fullscreen et son alias plein_écran."""
    dispatched = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'hyprctl')
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': dispatched.append((disp, arg)) or True)
    if hasattr(cc, '_hypr_dispatch_hyprland'):
        monkeypatch.setattr(
            cc,
            '_hypr_dispatch_hyprland',
            lambda legacy_cmd='', legacy_args='', lua_cmd='': dispatched.append((legacy_cmd, lua_cmd)) or True
        )

    res = computer_control({'action': 'fullscreen'})
    assert 'plein écran' in res.lower() or 'fullscreen' in res.lower()
    assert any('fullscreen' in str(d).lower() for d in dispatched)

    # Test avec l'alias français
    res_fr = computer_control({'action': 'plein_écran'})
    assert 'plein écran' in res_fr.lower() or 'fullscreen' in res_fr.lower()


def test_window_action_float(monkeypatch):
    """Vérifie l'action float et son alias flottant."""
    dispatched = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'hyprctl')
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': dispatched.append((disp, arg)) or True)
    if hasattr(cc, '_hypr_dispatch_hyprland'):
        monkeypatch.setattr(
            cc,
            '_hypr_dispatch_hyprland',
            lambda legacy_cmd='', legacy_args='', lua_cmd='': dispatched.append((legacy_cmd, lua_cmd)) or True
        )

    res = computer_control({'action': 'float'})
    assert 'flottant' in res.lower() or 'float' in res.lower()
    assert any('float' in str(d).lower() or 'togglefloating' in str(d).lower() for d in dispatched)


def test_window_action_center(monkeypatch):
    """Vérifie l'action center et son alias centrer."""
    dispatched = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'hyprctl')
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': dispatched.append((disp, arg)) or True)
    if hasattr(cc, '_hypr_dispatch_hyprland'):
        monkeypatch.setattr(
            cc,
            '_hypr_dispatch_hyprland',
            lambda legacy_cmd='', legacy_args='', lua_cmd='': dispatched.append((legacy_cmd, lua_cmd)) or True
        )

    res = computer_control({'action': 'center'})
    assert 'centr' in res.lower()
    assert any('center' in str(d).lower() for d in dispatched)


def test_window_action_close(monkeypatch):
    """Vérifie l'action close qui appelle close_window de window_instances."""
    closed_windows = []
    if hasattr(cc, '_hypr_close_window'):
        monkeypatch.setattr(cc, '_hypr_close_window', lambda sel: closed_windows.append(sel) or True)
    monkeypatch.setattr(
        'actions.window_instances.close_window',
        lambda sel, force=False: closed_windows.append(sel) or True,
        raising=False
    )

    res = computer_control({'action': 'close', 'window': 'kitty'})
    assert 'fermé' in res.lower() or 'close' in res.lower()
    assert 'kitty' in closed_windows or any('kitty' in str(w) for w in closed_windows)


def test_focus_window_by_address(monkeypatch):
    """Vérifie que _focus_window localise correctement une fenêtre par son champ address
    (avec préfixe 'address:0x...' ou hexadécimal brut '0x...') et la focalise."""
    focused = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'hyprctl')
    monkeypatch.setattr(cc.time, 'sleep', lambda s: None)

    mock_clients = [
        {"address": "0x55aabbcc", "title": "Editor Window", "class": "code", "workspace": {"id": 1}},
        {"address": "0x99ddeeff", "title": "Terminal", "class": "foot", "workspace": {"id": 1}},
    ]
    monkeypatch.setattr(cc, '_hyprctl_json', lambda cmd: mock_clients if cmd == "clients" else {"id": 1})

    if hasattr(cc, '_hypr_focus_window'):
        monkeypatch.setattr(cc, '_hypr_focus_window', lambda sel: focused.append(sel) or True)
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': focused.append((disp, arg)) or True)

    # 1. Test avec sélecteur préfixé "address:0x55aabbcc"
    res1 = cc._focus_window("address:0x55aabbcc")
    assert "Editor Window" in res1 or "code" in res1
    assert "address:0x55aabbcc" in focused or any("address:0x55aabbcc" in str(x) for x in focused)

    # 2. Test avec adresse hexadécimale brute "0x55aabbcc"
    focused.clear()
    res2 = cc._focus_window("0x55aabbcc")
    assert "Editor Window" in res2 or "code" in res2
    assert "address:0x55aabbcc" in focused or any("address:0x55aabbcc" in str(x) for x in focused)


# ════════════════════════════════════════════════════════════════════════════
# 4. Actions 'press' et 'hotkey' : keycodes Linux & ydotool
# ════════════════════════════════════════════════════════════════════════════

def test_press_key_linux_keycodes(monkeypatch):
    """Vérifie que 'enter' et 'esc' émettent les bons keycodes Linux (28 et 1)."""
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'ydotool')

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    # Touche Enter (28)
    computer_control({'action': 'press', 'keys': 'enter'})
    assert any('ydotool' in cmd and 'key' in cmd and '28:1' in cmd and '28:0' in cmd for cmd in run_cmds)

    # Touche Esc (1)
    run_cmds.clear()
    computer_control({'action': 'press', 'keys': 'esc'})
    assert any('ydotool' in cmd and 'key' in cmd and '1:1' in cmd and '1:0' in cmd for cmd in run_cmds)


def test_hotkey_linux_keycodes(monkeypatch):
    """Vérifie que ctrl+c émet les keycodes Linux pour ctrl (29) et c (46)."""
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'ydotool')

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    computer_control({'action': 'hotkey', 'keys': 'ctrl+c'})
    # Doit émettre 29:1 (ctrl down), 46:1 (c down), 46:0 (c up), 29:0 (ctrl up)
    assert any(
        'ydotool' in cmd and 'key' in cmd
        and any('29:1' in arg for arg in cmd)
        and any('46:1' in arg for arg in cmd)
        for cmd in run_cmds
    )


# ════════════════════════════════════════════════════════════════════════════
# 5. Fallback _type_text : ydotool -> wtype -> clipboard paste
# ════════════════════════════════════════════════════════════════════════════

def test_type_text_ydotool_priority(monkeypatch):
    """Sous Wayland, ydotool type doit être utilisé en priorité."""
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd in ('ydotool', 'wtype'))

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    res = cc._type_text('bonjour')
    assert 'bonjour' in res
    assert any('ydotool' in cmd and 'type' in cmd and 'bonjour' in cmd for cmd in run_cmds)
    assert not any('wtype' in cmd for cmd in run_cmds)


def test_type_text_wtype_fallback(monkeypatch):
    """Si ydotool est absent mais wtype présent, wtype doit être utilisé."""
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'wtype')

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    res = cc._type_text('bonjour')
    assert 'bonjour' in res
    assert any('wtype' in cmd and 'bonjour' in cmd for cmd in run_cmds)


def test_type_text_clipboard_paste_fallback(monkeypatch):
    """Si ni ydotool ni wtype ne sont dispo, fallback sur le presse-papiers."""
    calls = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: False)
    monkeypatch.setattr(cc, '_clipboard_copy', lambda text: calls.append(('copy', text)) or True)
    monkeypatch.setattr(cc, '_clipboard_paste', lambda: calls.append(('paste',)) or 'Collé')

    cc._type_text('test fallback')
    assert ('copy', 'test fallback') in calls
    assert ('paste',) in calls


# ════════════════════════════════════════════════════════════════════════════
# 6. Parsing local en langage naturel (_parse_control_locally)
# ════════════════════════════════════════════════════════════════════════════

def test_parse_control_locally_typing_with_window():
    # « tape codex dans le terminal »
    p1 = _parse_control_locally('tape codex dans le terminal')
    assert p1 is not None
    assert p1['action'] == 'type'
    assert p1['params']['text'] == 'codex'
    assert 'terminal' in p1['params'].get('window', '')

    # « écris ls dans kitty »
    p2 = _parse_control_locally('écris ls dans kitty')
    assert p2 is not None
    assert p2['action'] == 'type'
    assert p2['params']['text'] == 'ls'
    assert 'kitty' in p2['params'].get('window', '')


def test_parse_control_locally_mouse_move():
    # « déplace la souris en 500, 300 »
    p = _parse_control_locally('déplace la souris en 500, 300')
    assert p is not None
    assert p['action'] == 'move'
    assert p['params']['x'] == 500
    assert p['params']['y'] == 300


def test_parse_control_locally_window_actions():
    # « plein écran »
    p_full1 = _parse_control_locally('plein écran')
    assert p_full1 is not None
    assert p_full1['action'] == 'fullscreen'

    # « mets en plein écran »
    p_full2 = _parse_control_locally('mets en plein écran')
    assert p_full2 is not None
    assert p_full2['action'] == 'fullscreen'

    # « centre la fenêtre »
    p_center = _parse_control_locally('centre la fenêtre')
    assert p_center is not None
    assert p_center['action'] == 'center'

    # « rend la fenêtre flottante »
    p_float = _parse_control_locally('rend la fenêtre flottante')
    assert p_float is not None
    assert p_float['action'] == 'float'


# ════════════════════════════════════════════════════════════════════════════
# 7. Non-régression : Fonctionnalités existantes
# ════════════════════════════════════════════════════════════════════════════

def test_existing_brightness_controls(monkeypatch):
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'brightnessctl')
    monkeypatch.setattr(cc, '_run', lambda cmd, timeout=3: subprocess.CompletedProcess(cmd, 0, stdout='4000', stderr=''))
    
    res_set = computer_control({'action': 'brightness_set', 'value': 75})
    assert '75%' in res_set


def test_existing_switch_workspace(monkeypatch):
    dispatched = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'hyprctl')
    monkeypatch.setattr(cc, '_hypr_dispatch', lambda disp, arg='': dispatched.append((disp, arg)) or True)

    res = computer_control({'action': 'switch_workspace', 'workspace': '3'})
    assert 'bureau 3' in res


def test_existing_click_actions(monkeypatch):
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'ydotool')

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    # Clic gauche
    res_left = computer_control({'action': 'click', 'x': 100, 'y': 200, 'button': 'left'})
    assert 'Clic left à (100,200)' in res_left

    # Clic droit
    res_right = computer_control({'action': 'right_click', 'x': 300, 'y': 400})
    assert 'Clic right à (300,400)' in res_right


def test_existing_scroll(monkeypatch):
    run_cmds = []
    monkeypatch.setattr(cc, '_WAYLAND', True)
    monkeypatch.setattr(cc, '_have', lambda cmd: cmd == 'ydotool')

    def mock_run(cmd, *args, **kwargs):
        run_cmds.append(cmd)
        return cc.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(cc.kit, 'run', mock_run)

    res = computer_control({'action': 'scroll', 'direction': 'down', 'amount': 5})
    assert 'Défilement down x5' in res


def test_existing_parse_locally():
    # Workspace
    p_ws = _parse_control_locally('passe au bureau 2')
    assert p_ws == {'action': 'switch_workspace', 'params': {'workspace': '2'}}

    # Clic
    p_cl = _parse_control_locally('clique en 100, 200')
    assert p_cl['action'] == 'click'
    assert p_cl['params'] == {'x': 100, 'y': 200}

    # Scroll
    p_sc = _parse_control_locally('défile vers le bas de 4')
    assert p_sc['action'] == 'scroll'
    assert p_sc['params'] == {'direction': 'down', 'amount': 4}

    # Presse-papiers
    p_paste = _parse_control_locally('colle le texte')
    assert p_paste['action'] == 'paste'

    # Luminosité
    p_br = _parse_control_locally('règle la luminosité à 80%')
    assert p_br['action'] == 'brightness_set'
    assert p_br['params']['value'] == 80


def test_modern_status_and_private_clipboard(monkeypatch):
    monkeypatch.setattr(cc, "_active_window_title", lambda: "Terminal projet")
    monkeypatch.setattr(cc.os, "getloadavg", lambda: (0.42, 0.3, 0.2))
    monkeypatch.setattr(cc, "_clipboard_current_content", lambda: "secret-ne-pas-afficher")
    status = computer_control({"action": "system_status"})
    clipboard = computer_control({"action": "clipboard_status"})
    assert "charge" in status and "Terminal projet" in status
    assert "22 caractères" in clipboard
    assert "secret-ne-pas-afficher" not in clipboard


def test_pipewire_volume_is_bounded(monkeypatch):
    commands = []
    monkeypatch.setattr(cc, "_have", lambda cmd: cmd == "wpctl")
    monkeypatch.setattr(cc, "_run", lambda cmd, **kwargs: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    result = computer_control({"action": "volume_set", "value": 180})
    assert "100%" in result
    assert commands[-1][-1] == "100%"


def test_parse_modern_computer_commands():
    assert _parse_control_locally("quel est le volume") == {"action": "volume_get", "params": {}}
    assert _parse_control_locally("règle le volume à 35%") == {"action": "volume_set", "params": {"value": 35}}
    assert _parse_control_locally("état du système") == {"action": "system_status", "params": {}}
