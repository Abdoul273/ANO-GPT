"""Reconnaissance locale isolée : un crash natif ne peut pas tuer ANO-GPT."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import struct
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("anogpt.barge_in")


class IsolatedInterruptDetector:
    def __init__(self, model_path, sample_rate=16000):
        self.available = False
        self.last_text = self.last_kind = ""
        self.fail_reason = ""
        self._generation = 0
        self._idle = True
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._audio = queue.Queue(maxsize=50)
        self._results = queue.Queue(maxsize=4)
        self._process = None
        self._stderr_tail = []
        path = Path(model_path)
        if not path.is_dir() or not ((path / "am").is_dir() or (path / "conf").is_dir()):
            self.fail_reason = f"modèle Vosk absent ou invalide ({model_path})"
            return
        if importlib.util.find_spec("vosk") is None:
            self.fail_reason = "paquet vosk absent"
            return
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "core.interrupt_worker", str(path), str(sample_rate)],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
                env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            )
        except OSError as exc:
            self.fail_reason = f"sous-processus impossible : {exc}"
            return
        self.available = True
        threading.Thread(target=self._write, name="interrupt-pcm", daemon=True).start()
        threading.Thread(target=self._read, name="interrupt-words", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="interrupt-err", daemon=True).start()

    def wait_ready(self, timeout=45.0) -> bool:
        if self._ready.wait(timeout):
            return True
        if self._process is not None and self._process.poll() is not None:
            self.available = False
            err = " ".join(self._stderr_tail).strip()
            self.fail_reason = err or f"worker arrêté (code {self._process.returncode})"
            logger.warning("Interruption locale : %s", self.fail_reason)
            return False
        return self.available and self._ready.is_set()

    def _write(self):
        try:
            while not self._closed.is_set():
                try:
                    generation, pcm = self._audio.get(timeout=0.2)
                except queue.Empty:
                    continue
                data = memoryview(struct.pack("<II", generation, len(pcm)) + pcm)
                while data and not self._closed.is_set():
                    written = self._process.stdin.write(data)
                    if not written:
                        return
                    data = data[written:]
        except (OSError, ValueError):
            self.available = False

    def _read_stderr(self):
        try:
            for raw in self._process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail = (self._stderr_tail + [line])[-8:]
                    logger.debug("interrupt_worker: %s", line)
        except (OSError, ValueError):
            pass

    def _read(self):
        try:
            for line in self._process.stdout:
                result = json.loads(line)
                if result.get("ready"):
                    self._ready.set()
                elif result.get("generation") == self._generation:
                    try:
                        self._results.put_nowait(result)
                    except queue.Full:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            if not self._closed.is_set():
                self.available = False
                if not self.fail_reason:
                    self.fail_reason = "worker d'interruption fermé"

    def process(self, pcm):
        if not self.available or not self._ready.is_set():
            return False
        self._idle = False
        result = None
        while True:
            try:
                candidate = self._results.get_nowait()
                if candidate and candidate.get("generation") == self._generation:
                    result = candidate
            except queue.Empty:
                break
        if result:
            self.last_text = result["text"]
            self.last_kind = result["kind"]
            return True
        data = pcm if isinstance(pcm, bytes) else pcm.tobytes()
        try:
            self._audio.put_nowait((self._generation, data))
        except queue.Full:
            self.reset()  # Jamais reconnaître une commande à partir d'audio troué.
        return False

    def reset(self):
        if self._idle:
            return
        self._idle = True
        self._generation += 1
        for q in (self._audio, self._results):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        try:
            self._audio.put_nowait((self._generation, b""))
        except queue.Full:
            pass

    def close(self):
        self.available = False
        self._closed.set()
        proc = self._process
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.5)
            for pipe in (proc.stdin, proc.stdout):
                if pipe:
                    pipe.close()
