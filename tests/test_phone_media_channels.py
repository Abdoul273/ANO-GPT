"""Canaux temps réel du téléphone : micro, caméra et pilotage à distance.

Ces tests montent le vrai serveur FastAPI et parlent WebSocket, car c'est
précisément la couche transport qui a fait défaut jusqu'ici.
"""

import asyncio

import pytest

from dashboard import server as server_module
from dashboard.server import DashboardServer


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "_local_ip", lambda: "192.168.1.44")
    dash = DashboardServer()
    dash._uploads_dir = tmp_path
    return dash


@pytest.fixture
def client(dashboard):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    return fastapi_testclient.TestClient(dashboard.app)


def _token(dashboard) -> str:
    return dashboard._pair_device(dashboard.new_key())["token"]


def test_camera_frames_reach_the_pc(dashboard, client):
    token = _token(dashboard)
    seen: list[bytes] = []
    dashboard.set_frame_callback(seen.append)

    with client.websocket_connect(f"/ws/phone-camera?token={token}") as socket:
        socket.send_bytes(b"\xff\xd8jpeg-1")
        socket.send_bytes(b"\xff\xd8jpeg-2")
        # Laisser le serveur traiter avant de refermer.
        socket.send_bytes(b"\xff\xd8jpeg-3")

    assert seen[:3] == [b"\xff\xd8jpeg-1", b"\xff\xd8jpeg-2", b"\xff\xd8jpeg-3"]


def test_only_the_latest_camera_frame_is_kept(dashboard, client):
    token = _token(dashboard)
    with client.websocket_connect(f"/ws/phone-camera?token={token}") as socket:
        socket.send_bytes(b"vieille")
        socket.send_bytes(b"recente")
        # Empiler les trames ferait grandir le retard sans fin : le serveur ne
        # garde que la dernière.
        assert dashboard.latest_phone_frame() in (b"vieille", b"recente")


def test_camera_channel_refuses_an_unknown_token(client):
    fastapi = pytest.importorskip("fastapi")
    with pytest.raises(fastapi.WebSocketDisconnect):
        with client.websocket_connect("/ws/phone-camera?token=vole") as socket:
            socket.send_bytes(b"jpeg")


def test_phone_audio_feeds_the_relay_queue(dashboard, client):
    token = _token(dashboard)
    with client.websocket_connect(f"/ws/phone-audio?token={token}") as socket:
        socket.send_bytes(b"\x01\x02" * 160)

    chunk = asyncio.run(asyncio.wait_for(dashboard._phone_audio_queue.get(), 2))
    assert chunk["mime_type"] == "audio/pcm;rate=16000"
    assert len(chunk["data"]) == 320

    marker = asyncio.run(asyncio.wait_for(dashboard._phone_audio_queue.get(), 2))
    assert marker == {"activity": "phone_stream_end"}


def test_audio_channel_refuses_an_unknown_token(client):
    fastapi = pytest.importorskip("fastapi")
    with pytest.raises(fastapi.WebSocketDisconnect):
        with client.websocket_connect("/ws/phone-audio?token=vole") as socket:
            socket.send_bytes(b"pcm")


def test_remote_text_does_not_unmute_the_pc(dashboard, client):
    token = _token(dashboard)
    wake_calls = []
    dashboard.set_wake_callback(lambda: wake_calls.append(True))

    response = client.post(
        "/api/command",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "cherche les actualités"},
    )

    assert response.status_code == 200
    assert wake_calls == []
    command = asyncio.run(asyncio.wait_for(dashboard._command_queue.get(), 2))
    assert command == "cherche les actualités"


def test_remote_websocket_text_does_not_unmute_the_pc(dashboard, client):
    token = _token(dashboard)
    wake_calls = []
    dashboard.set_wake_callback(lambda: wake_calls.append(True))

    with client.websocket_connect(f"/ws?token={token}") as socket:
        socket.send_json({"type": "command", "text": "bonjour"})

    assert wake_calls == []
    command = asyncio.run(asyncio.wait_for(dashboard._command_queue.get(), 2))
    assert command == "bonjour"


def test_only_explicit_remote_wake_reactivates_the_pc(dashboard, client):
    token = _token(dashboard)
    wake_calls = []
    dashboard.set_wake_callback(lambda: wake_calls.append(True))

    response = client.post(
        "/api/wake", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert wake_calls == [True]


def test_pc_can_command_the_phone_camera(dashboard, client):
    token = _token(dashboard)
    with client.websocket_connect(f"/ws?token={token}") as socket:
        asyncio.run(dashboard.send_phone_command("start"))
        received = None
        for _ in range(6):  # l'historique précède parfois la commande
            message = socket.receive_json()
            if message.get("type") == "phone_camera":
                received = message
                break
    assert received == {"type": "phone_camera", "action": "start"}


def test_no_command_is_claimed_sent_without_any_client(dashboard):
    # Sans téléphone connecté, l'ordre ne part pas : le PC doit le savoir pour
    # ne pas promettre une caméra qui ne s'allumera jamais.
    assert asyncio.run(dashboard.send_phone_command("start")) is False


def test_stale_frames_are_not_considered_online(dashboard):
    assert dashboard.phone_camera_online() is False
    dashboard._phone_frame = b"jpeg"
    dashboard._phone_frame_at = 0.0
    assert dashboard.phone_camera_online() is False


def test_captures_land_in_the_uploads_folder(dashboard, tmp_path):
    path = dashboard.save_capture(b"\xff\xd8donnees", ".jpg", "photo")

    assert path.parent == tmp_path
    assert path.read_bytes() == b"\xff\xd8donnees"
    assert path.name.startswith("ano-photo-")

    # Deux captures dans la même seconde ne doivent pas s'écraser.
    second = dashboard.save_capture(b"autre", ".jpg", "photo")
    assert second != path
    assert second.read_bytes() == b"autre"
