import pytest
from fastapi.testclient import TestClient

from dashboard import server


@pytest.fixture
def dashboard(monkeypatch):
    monkeypatch.setattr(server, "_local_ip", lambda: "192.168.1.44")
    monkeypatch.setattr(server, "_load_devices", lambda: {})
    return server.DashboardServer()


def test_static_token_no_longer_authenticates(dashboard):
    with TestClient(dashboard.app) as client:
        for endpoint in ("/api/command", "/api/diagnostics"):
            request = client.post if endpoint.endswith("command") else client.get
            assert request(endpoint, headers={"Authorization": "Bearer local_ui_token"}).status_code == 401


def test_command_validation_and_backpressure(dashboard):
    dashboard._tokens.add("test-session")
    with TestClient(dashboard.app) as client:
        headers = {"Authorization": "Bearer test-session"}
        for payload in ([], None, {"text": 42}, {"text": " "}, {"enc": [1]}):
            assert client.post("/api/command", json=payload, headers=headers).status_code == 400
        assert client.post("/api/command", content="{invalid", headers=headers).status_code == 400
        assert client.post("/api/command", json={"text": "x" * 16001}, headers=headers).status_code == 413
        for _ in range(64):
            assert client.post("/api/command", json={"text": "bonjour"}, headers=headers).status_code == 200
        assert client.post("/api/command", json={"text": "bonjour"}, headers=headers).status_code == 429


def test_websocket_rejects_invalid_message_without_losing_connection(dashboard):
    dashboard._tokens.add("test-session")
    with TestClient(dashboard.app) as client:
        with client.websocket_connect("/ws?token=test-session") as ws:
            ws.send_json([])
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "command", "text": 42})
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "command", "text": "x" * 16001})
            assert ws.receive_json()["type"] == "error"


def test_diagnostics_require_session(dashboard, monkeypatch):
    from core import diagnostics
    monkeypatch.setattr(diagnostics, "collect", lambda: {"status": "ok", "checks": []})
    dashboard._tokens.add("test-session")
    with TestClient(dashboard.app) as client:
        response = client.get("/api/diagnostics", headers={"Authorization": "Bearer test-session"})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
