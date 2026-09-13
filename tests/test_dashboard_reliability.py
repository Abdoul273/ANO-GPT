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


def test_open_port_uses_running_loop_not_get_event_loop(monkeypatch):
    import asyncio

    called = {}

    def fake_ensure(port, proto="TCP"):
        called["port"] = port
        called["proto"] = proto
        return None

    monkeypatch.setattr(server, "_ensure_network_access", fake_ensure)

    async def scenario():
        dash = server.DashboardServer()
        dash._loop = asyncio.get_running_loop()
        dash._open_port(8000, "TCP")
        await asyncio.sleep(0.05)
        assert called == {"port": 8000, "proto": "TCP"}

    asyncio.run(scenario())


def test_notify_capture_broadcasts_from_worker_thread(tmp_path, monkeypatch):
    import asyncio
    import threading

    monkeypatch.setattr(server, "_local_ip", lambda: "192.168.1.44")
    received: list[dict] = []

    async def scenario():
        dash = server.DashboardServer()
        dash._loop = asyncio.get_running_loop()
        dash._uploads_dir = tmp_path

        async def capture_broadcast(msg):
            received.append(dict(msg))

        dash.broadcast = capture_broadcast  # type: ignore[method-assign]
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"jpg")
        done = threading.Event()

        def worker():
            dash.notify_capture(path)
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        for _ in range(80):
            await asyncio.sleep(0.02)
            if received:
                break
        assert done.wait(1.0)
        assert received
        assert received[0]["type"] == "file_received"
        assert received[0]["name"] == "photo.jpg"

    asyncio.run(scenario())
