import asyncio
from types import SimpleNamespace

from core import elevenlabs_voice as voice


def test_playback_uses_existing_queue_and_closes_stream_on_interrupt(monkeypatch):
    async def scenario():
        closed = []
        async def fake(text, settings):
            try:
                yield b'\x00' * 960
                host._interrupted = True
                yield b'\x00' * 960
            finally:
                closed.append(True)
        states = []
        host = SimpleNamespace(_interrupted=False, _turn_done_event=asyncio.Event(),
            set_speaking=states.append, audio_in_queue=asyncio.Queue(),
            _audio_enqueued_sec=0, ui=SimpleNamespace(write_log=lambda _: None))
        monkeypatch.setattr(voice, 'stream_pcm', fake)
        await voice.speak_live_turn(host, 'Bonjour.', {})
        assert states == [True]
        assert host.audio_in_queue.qsize() == 1
        assert host._audio_enqueued_sec == .02
        assert closed and host._turn_done_event.is_set()
    asyncio.run(scenario())


def test_failure_releases_playback_without_exposing_network_details(monkeypatch):
    async def scenario():
        async def fake(*args):
            raise OSError('secret should never be logged')
            yield
        logs = []
        host = SimpleNamespace(_interrupted=False, _turn_done_event=asyncio.Event(),
            set_speaking=lambda _: None, ui=SimpleNamespace(write_log=logs.append))
        monkeypatch.setattr(voice, 'stream_pcm', fake)
        await voice.speak_live_turn(host, 'Bonjour.', {})
        assert host._turn_done_event.is_set()
        assert len(logs) == 1 and 'secret' not in logs[0]
    asyncio.run(scenario())


def test_provider_save_preserves_secrets(tmp_path, monkeypatch):
    from memory import config_manager as cm
    manager = cm.ConfigManager(tmp_path / 'settings.json', use_env=False)
    manager._file.update({'gemini_api_key': 'kept', 'custom': 42})
    monkeypatch.setattr(cm, '_default_manager', manager)
    cm.save_voice_provider('elevenlabs')
    assert manager._file.read() == {'gemini_api_key': 'kept', 'custom': 42, 'voice_provider': 'elevenlabs'}


def test_pcm_alignment_and_request_settings(monkeypatch):
    async def scenario():
        calls = []
        class Response:
            status = 200
            content = None
            def __init__(self): self.content = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def iter_chunked(self, size):
                for part in (b'a', b'b' * 960, b'c'):
                    yield part
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def post(self, url, **kwargs):
                calls.append({'url': url, **kwargs})
                return Response()
        monkeypatch.setattr(voice.aiohttp, 'ClientSession', Client)
        chunks = [chunk async for chunk in voice.stream_pcm('Bonjour', {'elevenlabs_api_key': 'test', 'elevenlabs_voice_id': 'FrenchVoice1', 'elevenlabs_model_id': 'eleven_turbo_v2_5'})]
        assert list(map(len, chunks)) == [960, 2]
        assert b''.join(chunks) == b'a' + b'b' * 960 + b'c'
        assert calls[0]['params']['output_format'] == 'pcm_24000'
        assert '/FrenchVoice1/stream' in calls[0]['url']
        assert calls[0]['json']['model_id'] == 'eleven_turbo_v2_5'
        assert len(calls) == 1
    asyncio.run(scenario())


def test_catalog_pages_are_loaded_and_secrets_are_not_returned(monkeypatch):
    calls = []
    class Response:
        status = 200
        def __init__(self, data): self.data = data
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self.data
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return Response({"voices": [{"voice_id": "Voice2", "name": "Zoé", "labels": {"language": "fr"}}],
                                 "has_more": True, "next_page_token": "page2"})
            return Response({"voices": [{"voice_id": "Voice1", "name": "Alice"}], "has_more": False})
    monkeypatch.setattr(voice.aiohttp, "ClientSession", Client)
    result = asyncio.run(voice.list_voices({"elevenlabs_api_key": "secret"}))
    assert [v["voice_id"] for v in result] == ["Voice1", "Voice2"]
    assert calls[1]["params"]["next_page_token"] == "page2"
    assert "secret" not in str(result)


def test_voice_and_model_change_are_saved_and_reconnect_live(tmp_path, monkeypatch):
    from memory import config_manager as cm
    from core.session_manager import SessionManager

    manager = cm.ConfigManager(tmp_path / "settings.json", use_env=False)
    manager._file.update({"elevenlabs_api_key": "kept", "custom": 42, "voice_provider": "elevenlabs"})
    monkeypatch.setattr(cm, "_default_manager", manager)
    async def scenario():
        host = SimpleNamespace(_voice_change_event=asyncio.Event(),
                               _loop=asyncio.get_running_loop(),
                               _voice_reconnect_requested=False)
        SessionManager._on_elevenlabs_voice_change(host, "FrenchVoice1", "eleven_turbo_v2_5")
        await asyncio.sleep(0)
        assert host._voice_change_event.is_set()
        assert host._voice_reconnect_requested
        saved = manager._file.read()
        assert saved["elevenlabs_voice_id"] == "FrenchVoice1"
        assert saved["elevenlabs_model_id"] == "eleven_turbo_v2_5"
        assert saved["elevenlabs_api_key"] == "kept" and saved["custom"] == 42
        assert saved["voice_provider"] == "elevenlabs"
    asyncio.run(scenario())


def test_catalog_permission_error_is_actionable_and_redacted(monkeypatch):
    import pytest
    class Response:
        status = 401
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self):
            return {"detail": {"status": "missing_permissions", "message": "secret-api-key"}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(voice.aiohttp, "ClientSession", Client)
    with pytest.raises(RuntimeError, match="voices_read") as error:
        asyncio.run(voice.list_voices({"elevenlabs_api_key": "secret-api-key"}))
    assert "secret-api-key" not in str(error.value)
