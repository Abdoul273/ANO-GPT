from dashboard import server


def test_loopback_is_never_a_remote_address():
    assert not server._valid_lan_ipv4("127.0.0.1")
    assert server._choose_lan_ip([("127.0.0.1", "lo", 100)]) == ""


def test_physical_wifi_wins_and_virtual_interfaces_are_rejected():
    chosen = server._choose_lan_ip([
        ("172.17.0.1", "route", 100),
        ("172.17.0.1", "docker0", 100),
        ("192.168.1.44", "wlan0", 70),
        ("10.0.0.15", "veth12", 100),
    ])
    assert chosen == "192.168.1.44"


def test_dashboard_refreshes_ip_after_wifi_connects(monkeypatch):
    addresses = iter(["", "192.168.43.22"])
    monkeypatch.setattr(server, "_local_ip", lambda: next(addresses))

    dashboard = server.DashboardServer()
    assert dashboard._ip == ""
    assert dashboard.refresh_network_address() == "192.168.43.22"
    assert "127.0.0.1" not in dashboard.get_url()
    assert "192.168.43.22" in dashboard.get_url()


def test_dashboard_refuses_to_generate_loopback_url(monkeypatch):
    monkeypatch.setattr(server, "_local_ip", lambda: "")
    dashboard = server.DashboardServer()
    try:
        dashboard.get_url()
    except RuntimeError as exc:
        assert "Wi-Fi/LAN" in str(exc)
    else:
        raise AssertionError("une URL distante loopback ne doit jamais être générée")


def test_native_pairing_is_one_time_and_persistent(monkeypatch):
    monkeypatch.setattr(server, "_local_ip", lambda: "192.168.1.44")
    dashboard = server.DashboardServer()
    key = dashboard.new_key()

    pairing = dashboard._pair_device(key.lower())

    assert pairing["ok"] is True
    assert pairing["token"] in dashboard._tokens
    assert pairing["device_token"] in dashboard._device_sessions
    assert dashboard._pair_device(key) is None


def test_certificate_is_generated_and_covers_the_current_lan_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CERTS_DIR", tmp_path)
    monkeypatch.setattr(server, "CERT_FILE", tmp_path / "jarvis.crt")
    monkeypatch.setattr(server, "KEY_FILE", tmp_path / "jarvis.key")

    assert server._ensure_certificates("192.168.1.44") is True
    assert server._cert_covers("192.168.1.44")
    # Changer de Wi-Fi change l'IP : le certificat doit être renouvelé, sinon
    # le navigateur et l'application rejettent la connexion.
    assert not server._cert_covers("192.168.5.9")


def test_health_describes_the_reachable_endpoint(monkeypatch):
    monkeypatch.setattr(server, "_local_ip", lambda: "192.168.1.44")
    monkeypatch.setattr(server.DashboardServer, "_ssl_enabled", staticmethod(lambda: True))

    described = server.DashboardServer().describe()

    assert described["service"] == "ano-gpt"
    assert described["scheme"] == "https"
    assert described["host"] == "192.168.1.44"
    assert described["port"] == server.PORT
    assert described["alt_port"] == server.PORT + 1


def test_health_reports_plain_http_when_no_certificate(monkeypatch):
    monkeypatch.setattr(server, "_local_ip", lambda: "192.168.1.44")
    monkeypatch.setattr(server.DashboardServer, "_ssl_enabled", staticmethod(lambda: False))

    described = server.DashboardServer().describe()

    # L'application mobile se fie à ce champ pour choisir son schéma.
    assert described["scheme"] == "http"
    assert described["alt_port"] == server.PORT


def test_firewalld_is_detected_even_without_authorization(monkeypatch):
    """`firewall-cmd --state` répond « Authorization failed » sans droits.

    Chercher uniquement « running » faisait sauter la branche firewalld : le
    port restait fermé et rien n'était signalé.
    """
    import subprocess as sp

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "ufw":
            raise FileNotFoundError
        if cmd[0] == "systemctl":
            return _Result(0, "active\n")
        if cmd[0] == "firewall-cmd":
            return _Result(252, "", "Authorization failed.\n")
        if cmd[:1] == ["pkexec"] or cmd[:2] == ["sudo", "-n"]:
            return _Result(1)  # aucun agent polkit : l'élévation échoue
        return _Result(1)

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(server, "_firewalld_port_open", lambda *_: False)
    fix = server._ensure_network_access(8000, "TCP")

    assert "firewall-cmd" in fix and "8000/tcp" in fix
    assert any(c[:1] == ["pkexec"] for c in calls), "l'ouverture doit être tentée"


def test_firewall_warning_lists_every_closed_port():
    dashboard = server.DashboardServer.__new__(server.DashboardServer)
    dashboard.firewall_fixes = []
    assert dashboard.firewall_warning() == ""

    dashboard.firewall_fixes = ["sudo firewall-cmd --add-port=8000/tcp"]
    warning = dashboard.firewall_warning()
    assert "8000/tcp" in warning
    assert "téléphone" in warning


def test_firewalld_timeout_is_not_propagated(monkeypatch):
    import subprocess as sp

    class _Result:
        returncode, stdout, stderr = 3, "inactive\n", ""

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "ufw":
            raise FileNotFoundError
        if cmd[:2] == ["firewall-cmd", "--state"]:
            raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 5))
        if cmd[0] == "systemctl":
            return _Result()
        if cmd[0] in {"iptables", "nft"}:
            raise FileNotFoundError
        return _Result()

    monkeypatch.setattr(sp, "run", fake_run)
    assert server._ensure_network_access(8000, "TCP") == ""
