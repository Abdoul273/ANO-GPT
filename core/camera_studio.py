"""core/camera_studio.py — Studio caméra unifié d'ANO-GPT.

Un seul flux vidéo, deux sources possibles : la webcam du PC ou la caméra du
téléphone relayée par le tableau de bord. Les images partent vers le conteneur
plein cadre de l'interface, et les captures atterrissent dans JARVIS Uploads.

Le point important est qu'aucune fenêtre d'application externe n'est ouverte :
« ouvre l'appareil photo » allume ce conteneur, jamais l'appareil photo du
système.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

# Cadence d'affichage. 30 images/s suffisent à la fluidité perçue et laissent
# du souffle au reste du système, qui fait tourner un modèle vocal en parallèle.
TARGET_FPS = 30.0
_FRAME_INTERVAL = 1.0 / TARGET_FPS

# Cadence d'enregistrement vidéo : volontairement plus basse que l'affichage,
# car la source téléphone livre rarement mieux et un fichier annoncé à 30 i/s
# mais nourri à 20 serait lu en accéléré.
VIDEO_FPS = 20.0

SOURCE_PC = "pc"
SOURCE_PHONE = "phone"

# Objectifs du téléphone. « front » est celui qui regarde l'utilisateur : c'est
# lui qu'on veut pour un selfie ou pour montrer son visage à l'assistant.
LENS_FRONT = "front"
LENS_BACK = "back"

_FRONT_WORDS = (
    "front", "selfie", "avant", "frontal", "frontale", "face", "moi", "me",
)


def normalize_lens(value: str | None) -> str | None:
    """Traduit un mot d'utilisateur en objectif. `None` si rien de reconnu.

    Le modèle transmet ce que la personne a dit — « selfie », « caméra avant »,
    « front camera » — sans vocabulaire imposé.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in _FRONT_WORDS):
        return LENS_FRONT
    if any(word in text for word in ("back", "rear", "arriere", "arrière", "dos", "principal")):
        return LENS_BACK
    return None


class CameraUnavailable(RuntimeError):
    """Aucune source vidéo exploitable."""


class CameraStudio:
    """Pilote le flux vidéo, les photos et les enregistrements.

    Toutes les méthodes publiques sont sûres à appeler depuis n'importe quel
    fil : l'interface Qt, la boucle asyncio et l'outil vocal y touchent.
    """

    def __init__(
        self,
        *,
        on_frame: Callable[[bytes], None] | None = None,
        on_state: Callable[[dict], None] | None = None,
        save_capture: Callable[[bytes, str, str], Path] | None = None,
        phone_start: Callable[[], None] | None = None,
        phone_stop: Callable[[], None] | None = None,
        phone_lens: Callable[[str], None] | None = None,
        uploads_dir: Path | None = None,
        camera_index: int = 0,
    ) -> None:
        self._on_frame = on_frame
        self._on_state = on_state
        self._save_capture = save_capture
        self._phone_start = phone_start
        self._phone_stop = phone_stop
        self._phone_lens_cb = phone_lens
        self._uploads_dir = uploads_dir or Path.home() / "Downloads" / "JARVIS Uploads"
        self._camera_index = camera_index

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._source = SOURCE_PC
        self._active = False
        # Le téléphone démarre sur son objectif principal, comme son
        # application photo : c'est ce que l'utilisateur attend par défaut.
        self._lens = LENS_BACK

        self._last_jpeg: bytes | None = None
        # Compteur d'images : il permet de distinguer « une image existe » de
        # « une image vient d'arriver », indispensable après un changement
        # d'objectif où l'image en mémoire montre encore l'autre caméra.
        self._frame_count = 0
        self._phone_jpeg: bytes | None = None
        self._phone_at = 0.0

        self._writer = None
        self._video_path: Path | None = None
        self._video_started = 0.0
        self._video_size: tuple[int, int] | None = None

    # ── état ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def source(self) -> str:
        return self._source

    @property
    def recording(self) -> bool:
        return self._writer is not None

    @property
    def lens(self) -> str:
        """Objectif utilisé côté téléphone (`front` ou `back`)."""
        return self._lens

    def state(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "source": self._source,
                "lens": self._lens,
                "recording": self.recording,
                "elapsed": (time.monotonic() - self._video_started) if self.recording else 0.0,
                "has_frame": self._last_jpeg is not None,
            }

    def latest_frame(self, timeout: float = 0.0) -> bytes | None:
        """Dernière image JPEG, en patientant si le flux vient d'ouvrir."""
        if timeout > 0:
            return self._wait_for_frame(timeout)
        return self._last_jpeg

    def _publish_state(self) -> None:
        if self._on_state:
            try:
                self._on_state(self.state())
            except Exception:
                pass  # l'affichage ne doit jamais interrompre la capture

    # ── source téléphone ─────────────────────────────────────────────────

    def push_phone_frame(self, jpeg: bytes) -> None:
        """Appelé par le tableau de bord à chaque image reçue du téléphone."""
        self._phone_jpeg = jpeg
        self._phone_at = time.monotonic()

    def phone_online(self, max_age: float = 3.0) -> bool:
        return bool(self._phone_jpeg) and (time.monotonic() - self._phone_at) < max_age

    # ── ouverture / fermeture ────────────────────────────────────────────

    def open(self, source: str = SOURCE_PC, *, lens: str | None = None) -> str:
        """Allume le flux. Renvoie la phrase à dire à l'utilisateur.

        `lens` ne concerne que le téléphone : demander l'objectif frontal
        bascule aussi la source, puisque le PC n'a qu'une webcam.
        """
        wanted_lens = normalize_lens(lens)
        if wanted_lens and str(source or "").strip() == "":
            source = SOURCE_PHONE
        source = SOURCE_PHONE if str(source).lower().startswith("ph") else SOURCE_PC

        with self._lock:
            already = self._active
            self._source = source
            if wanted_lens:
                self._lens = wanted_lens

        if source == SOURCE_PHONE and self._phone_start:
            # L'objectif part avant l'ordre d'allumage : le téléphone ouvre
            # ainsi la bonne caméra du premier coup, sans redémarrer son flux.
            self._send_lens()
            self._phone_start()

        if already:
            self._publish_state()
            return (f"Flux basculé sur la caméra "
                    f"{self._phone_lens_label() if source == SOURCE_PHONE else 'du PC'}.")

        if source == SOURCE_PC and not self._pc_camera_available():
            if self.phone_online() or self._phone_start:
                # Pas de webcam : le téléphone est le repli naturel plutôt
                # qu'un échec sec.
                with self._lock:
                    self._source = SOURCE_PHONE
                if self._phone_start:
                    self._phone_start()
            else:
                raise CameraUnavailable(
                    "Aucune webcam détectée sur le PC et aucun téléphone connecté."
                )

        self._stop.clear()
        self._active = True
        self._thread = threading.Thread(
            target=self._run, name="camera-studio", daemon=True
        )
        self._thread.start()
        self._publish_state()
        return "Caméra ouverte."

    def close(self) -> str:
        self.stop_video()
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self._active = False
        self._last_jpeg = None
        if self._phone_stop:
            self._phone_stop()
        self._publish_state()
        return "Caméra fermée."

    def switch_source(self) -> str:
        target = SOURCE_PC if self._source == SOURCE_PHONE else SOURCE_PHONE
        if not self._active:
            return self.open(target)
        with self._lock:
            self._source = target
        if target == SOURCE_PHONE and self._phone_start:
            self._phone_start()
        elif target == SOURCE_PC and self._phone_stop:
            self._phone_stop()
        self._publish_state()
        return f"Caméra {self._phone_lens_label() if target == SOURCE_PHONE else 'du PC'}."

    # ── objectif du téléphone ────────────────────────────────────────────

    def set_lens(self, lens: str) -> str:
        """Choisit l'objectif frontal ou arrière du téléphone.

        Demander la caméra frontale implique la caméra du téléphone : c'est la
        seule qui en possède deux. On bascule donc la source sans le demander,
        plutôt que de renvoyer une erreur pour une intention parfaitement
        claire.
        """
        wanted = normalize_lens(lens)
        if wanted is None:
            return ("Précisez « caméra frontale » ou « caméra arrière » "
                    "pour choisir l'objectif du téléphone.")

        if not self._active:
            self.open(SOURCE_PHONE, lens=wanted)
            return f"Caméra {self._phone_lens_label()} ouverte."

        with self._lock:
            self._lens = wanted
            was_phone = self._source == SOURCE_PHONE
            self._source = SOURCE_PHONE

        self._send_lens()
        if not was_phone and self._phone_start:
            self._phone_start()
        self._publish_state()
        return f"Caméra {self._phone_lens_label()}."

    def switch_lens(self) -> str:
        """Bascule d'un objectif du téléphone à l'autre."""
        return self.set_lens(
            LENS_BACK if self._lens == LENS_FRONT else LENS_FRONT
        )

    def _send_lens(self) -> None:
        if self._phone_lens_cb:
            try:
                self._phone_lens_cb(self._lens)
            except Exception:
                # Le téléphone est peut-être hors ligne : l'affichage local ne
                # doit pas tomber pour autant.
                pass

    def _phone_lens_label(self) -> str:
        return ("frontale du téléphone" if self._lens == LENS_FRONT
                else "du téléphone")

    # ── captures ─────────────────────────────────────────────────────────

    def photo(self) -> Path:
        """Enregistre l'image courante. Le flux continue pendant ce temps."""
        frame = self._last_jpeg
        if frame is None and self._active:
            # Le flux vient d'être ouvert : laisser la première image arriver
            # plutôt que rendre un échec pour quelques dizaines de ms.
            deadline = time.monotonic() + 3.0
            while frame is None and time.monotonic() < deadline:
                time.sleep(0.05)
                frame = self._last_jpeg
        if frame is None:
            raise CameraUnavailable("Aucune image disponible pour la photo.")
        if self._save_capture:
            return self._save_capture(frame, ".jpg", "photo")
        dest = self._uploads_dir / f"ano-photo-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        dest.write_bytes(frame)
        return dest

    def start_video(self) -> Path:
        with self._lock:
            if self._writer is not None:
                raise CameraUnavailable("Un enregistrement est déjà en cours.")
        if not self._active:
            self.open(self._source)
        frame = self._wait_for_frame(timeout=3.0)
        if frame is None:
            raise CameraUnavailable("Aucune image à enregistrer.")

        cv2, np = _cv2_numpy()
        decoded = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise CameraUnavailable("Image illisible, enregistrement annulé.")
        height, width = decoded.shape[:2]

        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        path = self._uploads_dir / f"ano-video-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, (width, height)
        )
        if not writer.isOpened():
            raise CameraUnavailable("Encodeur vidéo indisponible (codec mp4v).")

        with self._lock:
            self._writer = writer
            self._video_path = path
            self._video_size = (width, height)
            self._video_started = time.monotonic()
        self._publish_state()
        return path

    def stop_video(self) -> Path | None:
        with self._lock:
            writer, path = self._writer, self._video_path
            self._writer = None
            self._video_path = None
            self._video_size = None
        if writer is None:
            return None
        try:
            writer.release()
        except Exception:
            pass
        self._publish_state()
        return path

    # ── boucle de capture ────────────────────────────────────────────────

    def wait_fresh_frame(self, timeout: float = 4.0,
                         settle: float = 1.0) -> bytes | None:
        """Attend une image postérieure à l'appel, pas celle déjà en mémoire.

        `settle` laisse passer les dernières images de l'objectif précédent,
        qui sont encore en vol quand le téléphone rouvre sa caméra.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        if settle > 0:
            self._stop.wait(min(settle, max(0.0, timeout)))
        mark = self._frame_count
        while time.monotonic() < deadline:
            if self._frame_count != mark:
                return self._last_jpeg
            time.sleep(0.05)
        return self._last_jpeg

    def _wait_for_frame(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._last_jpeg is not None:
                return self._last_jpeg
            time.sleep(0.05)
        return self._last_jpeg

    def _pc_camera_available(self) -> bool:
        try:
            cv2, _ = _cv2_numpy()
        except Exception:
            return False
        try:
            cap = cv2.VideoCapture(self._camera_index)
            ok = cap.isOpened()
            cap.release()
            return ok
        except Exception:
            return False

    def _run(self) -> None:
        capture = None
        opened_for = None
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                source = self._source

                if source == SOURCE_PC:
                    if opened_for != SOURCE_PC:
                        capture = self._open_pc_capture()
                        opened_for = SOURCE_PC
                    jpeg = self._grab_pc(capture)
                else:
                    if capture is not None:
                        # Libérer la webcam dès qu'on passe au téléphone : la
                        # garder ouverte allume la diode pour rien.
                        self._release(capture)
                        capture, opened_for = None, SOURCE_PHONE
                    opened_for = SOURCE_PHONE
                    jpeg = self._phone_jpeg if self.phone_online() else None

                if jpeg:
                    self._last_jpeg = jpeg
                    self._frame_count += 1
                    if self._on_frame:
                        try:
                            self._on_frame(jpeg)
                        except Exception:
                            pass
                    self._record(jpeg)

                delay = _FRAME_INTERVAL - (time.monotonic() - started)
                if delay > 0:
                    self._stop.wait(delay)
        finally:
            if capture is not None:
                self._release(capture)
            self._active = False

    def _open_pc_capture(self):
        cv2, _ = _cv2_numpy()
        capture = cv2.VideoCapture(self._camera_index)
        if not capture.isOpened():
            capture = cv2.VideoCapture(0)
        # Les premières images d'une webcam sont noires le temps de l'exposition.
        for _ in range(3):
            capture.read()
        return capture

    def _grab_pc(self, capture) -> bytes | None:
        if capture is None or not capture.isOpened():
            return None
        cv2, _ = _cv2_numpy()
        ok, frame = capture.read()
        if not ok or frame is None:
            return None
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
        return buffer.tobytes() if ok else None

    @staticmethod
    def _release(capture) -> None:
        try:
            capture.release()
        except Exception:
            pass

    def _record(self, jpeg: bytes) -> None:
        with self._lock:
            writer, size = self._writer, self._video_size
        if writer is None or size is None:
            return
        try:
            cv2, np = _cv2_numpy()
            frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return
            if (frame.shape[1], frame.shape[0]) != size:
                # La source a changé de définition en cours de route (bascule
                # PC ↔ téléphone) : recadrer, sinon l'encodeur rejette l'image.
                frame = cv2.resize(frame, size)
            # ``VideoWriter.release()`` et ``write()`` entrent dans FFmpeg en
            # code natif. Les exécuter simultanément depuis stop_video() et le
            # worker provoque un segfault (pas une exception Python). Revalider
            # le writer puis conserver le verrou pendant l'appel natif.
            with self._lock:
                if self._writer is writer and self._video_size == size:
                    writer.write(frame)
        except Exception:
            pass


def _cv2_numpy():
    """Import paresseux : OpenCV coûte cher et ne sert qu'à la caméra."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise CameraUnavailable(
            "OpenCV manquant. Installez-le avec : pip install opencv-python"
        ) from exc
    return cv2, np
