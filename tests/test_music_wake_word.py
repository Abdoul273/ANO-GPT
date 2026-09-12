import numpy as np

from core.wake_word import MusicWakeWordDetector


class _FakePorcupine:
    frame_length = 4
    sample_rate = 16000

    def __init__(self):
        self.frames = []

    def process(self, frame):
        self.frames.append(frame)
        return 0 if frame == [5, 6, 7, 8] else -1

    def delete(self):
        pass


def test_music_detector_accumulates_portaudio_chunks_without_transcribing():
    detector = MusicWakeWordDetector.__new__(MusicWakeWordDetector)
    detector.available = True
    detector.frame_length = 4
    detector.sample_rate = 16000
    detector._engine = _FakePorcupine()
    detector._pending = np.empty(0, dtype=np.int16)

    assert detector.process(np.array([1, 2, 3], dtype=np.int16)) is False
    assert detector.process(np.array([4, 5, 6], dtype=np.int16)) is False
    assert detector.process(np.array([7, 8], dtype=np.int16)) is True
    assert detector._engine.frames == [[1, 2, 3, 4], [5, 6, 7, 8]]
