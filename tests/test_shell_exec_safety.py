"""Tests des garde-fous de sécurité de actions/shell_exec.py (§G)."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.shell_exec import (
    _is_blocked,
    _is_risky,
    _needs_interactive_terminal,
    run_shell,
    adapt_command_for_arch,
)


def test_harmless_commands_pass_through():
    for cmd in ["ls -la", "pwd", "echo hello", "cat file.txt"]:
        assert not _is_blocked(cmd)
        assert not _is_risky(cmd)


def test_catastrophic_commands_are_hard_blocked():
    for cmd in ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs.ext4 /dev/sda1",
                "dd if=/dev/zero of=/dev/sda"]:
        assert _is_blocked(cmd), cmd


def test_risky_but_not_blocked_commands_need_confirmation():
    for cmd in ["sudo pacman -Syu", "systemctl restart NetworkManager",
                "git reset --hard HEAD~3", "pkill firefox",
                "rm -rf ~/tmp/test", "pacman -R firefox",
                "systemctl poweroff", "shutdown -h now",
                "nmcli radio wifi off"]:
        assert not _is_blocked(cmd), cmd
        assert _is_risky(cmd), cmd


def test_run_shell_requires_interface_confirmation():
    from core import human_confirmation

    shown = []
    human_confirmation.clear()
    human_confirmation.bind(show=shown.append)
    result = run_shell({"command": "sudo shutdown -h now"})
    assert result.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert shown and "shutdown" in shown[0].detail


def test_model_confirm_parameter_cannot_bypass_interface():
    from core import human_confirmation

    shown = []
    human_confirmation.clear()
    human_confirmation.bind(show=shown.append)
    result = run_shell({"command": "sudo shutdown -h now", "confirm": True})
    assert result.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert shown


def test_run_shell_refuses_blocked_command_even_with_confirm():
    result = run_shell({"command": "rm -rf /", "confirm": True})
    assert "refusée" in result.lower()


def test_run_shell_executes_harmless_command_directly():
    result = run_shell({"command": "echo test123"})
    assert "test123" in result
    assert not result.startswith("[NEEDS_CONFIRM]")


def test_package_installs_need_a_visible_interactive_terminal():
    for cmd in [
        "sudo pacman -S nmap",
        "pacman -S --needed nmap",
        "sudo apt install nmap",
        "dnf install nmap",
        "flatpak install flathub org.example.App",
    ]:
        assert _needs_interactive_terminal(cmd), cmd
    assert not _needs_interactive_terminal("pacman -Q nmap")
    assert not _needs_interactive_terminal("python -m pip list")


def test_confirmed_install_is_launched_in_terminal(monkeypatch):
    import actions.shell_exec as shell_module
    from core import human_confirmation

    launched = {}
    launched_event = threading.Event()

    class FakeProcess:
        returncode = None

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        launched["argv"] = argv
        launched["kwargs"] = kwargs
        launched_event.set()
        return FakeProcess()

    monkeypatch.setattr(shell_module.shutil, "which", lambda name: "/usr/bin/kitty" if name == "kitty" else None)
    monkeypatch.setattr(shell_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shell_module.time, "sleep", lambda _seconds: None)

    shown = []
    human_confirmation.clear()
    human_confirmation.bind(show=shown.append)
    result = run_shell({"command": "sudo pacman -S nmap", "confirm": True})
    assert result.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert human_confirmation.resolve(shown[0].token, True, source="test")
    assert launched_event.wait(1.0)

    assert launched["argv"][0] == "kitty"
    assert "sudo pacman -S nmap" in launched["argv"][-1]
    assert launched["kwargs"]["start_new_session"] is True


def test_adapt_command_for_arch():
    # Élimination des fallbacks multi-distro inutiles
    assert adapt_command_for_arch("sudo pacman -S nmap || sudo apt install -y nmap") == "sudo pacman -S nmap"
    assert adapt_command_for_arch("yay -S package || sudo apt-get install package") == "yay -S package"

    # Traduction directe des commandes Debian/Ubuntu
    assert adapt_command_for_arch("sudo apt install -y nmap") == "sudo pacman -S --needed nmap"
    assert adapt_command_for_arch("sudo apt-get install -y wireshark-qt") == "sudo pacman -S --needed wireshark-qt"
    assert adapt_command_for_arch("apt install git curl") == "pacman -S --needed git curl"
    assert adapt_command_for_arch("sudo apt-get update") == "sudo pacman -Sy"
    assert adapt_command_for_arch("sudo apt upgrade -y") == "sudo pacman -Syu"
    assert adapt_command_for_arch("sudo apt remove --purge nmap") == "sudo pacman -Rns nmap"
    assert adapt_command_for_arch("apt search ripgrep") == "pacman -Ss ripgrep"

    # Conservation des commandes normales non-apt
    for cmd in ["echo hello", "git status", "pacman -Syu"]:
        assert adapt_command_for_arch(cmd) == cmd


def test_run_shell_adapts_debian_command_before_confirmation():
    from core import human_confirmation

    shown = []
    human_confirmation.clear()
    human_confirmation.bind(show=shown.append)
    result = run_shell({"command": "sudo apt install -y nmap"})
    assert result.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert shown
    assert "sudo pacman -S --needed nmap" in shown[0].detail
    assert "sudo apt install -y nmap" not in shown[0].detail
