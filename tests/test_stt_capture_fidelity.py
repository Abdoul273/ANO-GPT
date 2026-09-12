"""Le chemin de capture réel doit préserver même les consonnes non voisées."""
import ast
import collections
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from core.stt_audio import pcm16_bytes, signal_metrics


def test_all_pcm16_values_roundtrip_exactly():
    raw = np.arange(-32768, 32768, dtype=np.int16)
    normalized = raw.astype(np.float32) / 32768.0
    assert pcm16_bytes(normalized) == raw.astype('<i2').tobytes()


def test_metrics_distinguish_quiet_signal_and_clipping():
    assert signal_metrics(np.zeros(320, dtype=np.int16).tobytes())['rms_dbfs'] == -100
    metrics = signal_metrics(np.full(16000, -32768, dtype=np.int16).tobytes())
    assert metrics['clipped_percent'] == 100
    assert metrics['duration_s'] == 1


def capture_callback(host, preroll, detector):
    # Exécuter la vraie closure sans ouvrir le matériel, Qt ou un socket.
    from core import audio_engine as engine
    tree = ast.parse(inspect.getsource(engine))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == 'callback')
    scope = dict(vars(engine))
    scope.update(self=host, preroll=preroll, preprocessor=detector,
                 loop=SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
                 _aec_mode=False,
                 _cb_state={'last_voice': time.monotonic(), 'barge_stop_quiet': 0,
                            'level': 0, 'last_err_log': 0, 'err_count': 0})
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<capture callback>', 'exec'), scope)
    return scope['callback'], scope


@pytest.mark.parametrize('speaking', [False, True])
def test_real_capture_retains_preroll_and_high_frequency_consonants(speaking):
    # Paquets de souffle/fricatives HF très faibles : aucun masque spectral
    # ni gain ne doit les faire disparaître à l'intérieur d'une phrase admise.
    rng = np.random.default_rng(20)
    raw = rng.integers(-30, 31, 1024, dtype=np.int16)
    lead = rng.integers(-50, 51, 1024, dtype=np.int16)
    sent = []
    h = SimpleNamespace(_phone_active=False, _is_speaking=speaking,
                        _speaking_lock=threading.Lock(), _activity_open=True,
                        _activity_since=time.monotonic(), _voice_evidence_ms=0,
                        _voice_chunks=[], _enqueue_out=sent.append,
                        _activity_cancel=Mock(), _activity_end=Mock(),
                        ui=SimpleNamespace(muted=False, set_volume=Mock(), feed_audio_spectrum=Mock()))
    detector = SimpleNamespace(has_speech=lambda *a, **kw: True,
                               is_voice_like=lambda *a, **kw: True)
    callback, scope = capture_callback(h, collections.deque([lead.astype(np.float32)/32768]), detector)
    callback(raw.reshape(-1, 1), 1024, None, None)
    assert scope['_cb_state']['err_count'] == 0
    if speaking:
        assert sent == []
        h._activity_cancel.assert_called_once()
    else:
        assert b''.join(m['data'] for m in sent) == lead.tobytes() + raw.tobytes()


def test_portaudio_overflow_marks_the_utterance_as_incomplete():
    h = SimpleNamespace(_phone_active=False, _audio_drop_count=0, _activity_open=False,
                        _wake_enabled=False, ui=SimpleNamespace(muted=True))
    callback, scope = capture_callback(h, collections.deque(), None)
    callback(np.zeros((1024, 1), dtype=np.int16), 1024, None,
             SimpleNamespace(input_overflow=True))
    assert h._audio_drop_count == 1
    assert scope['_cb_state']['err_count'] == 0
