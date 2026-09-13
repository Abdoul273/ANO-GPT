"""core/face_memory.py — ANO-GPT reconnaît les visages qu'on lui a présentés.

Deux réseaux OpenCV, entièrement locaux (aucun visage ne part sur le réseau
pour être identifié) :

* **YuNet** détecte les visages et leurs cinq repères (232 Ko, ~15 ms en 320p) ;
* **SFace** transforme un visage aligné en empreinte de 128 nombres (37 Mo,
  ~20 ms par visage). Deux empreintes de la même personne ont un cosinus
  élevé, celles de deux inconnus un cosinus proche de zéro.

Les empreintes vivent dans `memory/faces.db` : plusieurs par personne, parce
qu'un visage change avec la lumière, l'angle et la barbe. Chaque nouvelle
rencontre confirmée en ajoute une, donc la reconnaissance s'affine avec le
temps.

Un visage inconnu n'est pas jeté : il reste *en attente* quelques minutes,
le temps que l'utilisateur réponde « c'est Karim, mon frère ». L'inscription
réutilise alors l'empreinte déjà capturée — pas besoin de reprendre la photo.

Tout tourne en un seul fil ONNX/OpenCV : la voix a besoin de l'autre cœur.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np

from core import action_kit as kit

_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = _ROOT / "models"
DETECTOR_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
DB_PATH = _ROOT / "memory" / "faces.db"

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODEL_URLS = {
    DETECTOR_PATH: f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    RECOGNIZER_PATH: f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
# Un fichier tronqué par un téléchargement interrompu ferait planter OpenCV
# à chaque démarrage : on refuse tout ce qui est manifestement trop petit.
_MIN_MODEL_BYTES = {DETECTOR_PATH: 200_000, RECOGNIZER_PATH: 30_000_000}

# Seuils cosinus SFace. OpenCV recommande 0,363 ; on garde une marge des deux
# côtés : au-dessus de SURE on affirme, entre les deux on propose un prénom
# avec réserve, en dessous c'est un inconnu.
SURE_THRESHOLD = 0.42
MAYBE_THRESHOLD = 0.34
# Deux apparitions du même inconnu à quelques secondes d'écart : on les fusionne
# dans la même attente plutôt que d'ouvrir un second dossier.
PENDING_MERGE_THRESHOLD = 0.45
PENDING_TTL_S = 15 * 60
MAX_VECTORS_PER_PERSON = 40
MAX_PENDING_VECTORS = 8

# Qualité minimale pour inscrire un visage : trop petit ou trop flou, et
# l'empreinte apprise ne ressemblera à rien de ce qu'on reverra.
MIN_FACE_PX = 48
MIN_ENROLL_FACE_PX = 64
MIN_DETECT_SCORE = 0.75
MIN_SHARPNESS = 25.0
DETECT_MAX_DIM = 640

OWNER_WORDS = ("moi", "me", "c'est moi", "cest moi", "moi-meme", "moi même", "myself")


def _fold(value: str) -> str:
    lowered = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in lowered if not unicodedata.combining(c)).split())


def slug(value: str) -> str:
    return "_".join(_fold(value).replace("'", " ").split())[:60]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _cv2():
    import cv2
    return cv2


# ── Modèles ──────────────────────────────────────────────────────────────────

def models_present() -> bool:
    return all(p.is_file() and p.stat().st_size >= _MIN_MODEL_BYTES[p] for p in MODEL_URLS)


def ensure_models(progress: Callable[[str], None] | None = None) -> bool:
    """Télécharge les deux réseaux s'ils manquent. Vrai s'ils sont prêts."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for path, url in MODEL_URLS.items():
        if path.is_file() and path.stat().st_size >= _MIN_MODEL_BYTES[path]:
            continue
        if progress:
            progress(f"téléchargement de {path.name}")
        tmp = path.with_suffix(".part")
        try:
            resp = kit.http().get(url, timeout=(10, 120), stream=True)
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    if chunk:
                        fh.write(chunk)
            if tmp.stat().st_size < _MIN_MODEL_BYTES[path]:
                raise RuntimeError(f"{path.name} incomplet ({tmp.stat().st_size} octets)")
            tmp.replace(path)
        except Exception as exc:
            print(f"[Visages] modèle {path.name} indisponible : {exc}")
            tmp.unlink(missing_ok=True)
            return False
    return True


# ── Structures ───────────────────────────────────────────────────────────────

@dataclass
class DetectedFace:
    """Un visage trouvé dans une image, avec son empreinte."""
    box: tuple[int, int, int, int]          # x, y, w, h en pixels de l'image d'origine
    score: float
    embedding: np.ndarray                    # 128 floats, norme 1
    sharpness: float = 0.0
    thumb_jpeg: bytes = b""

    @property
    def size(self) -> int:
        return min(self.box[2], self.box[3])

    @property
    def center_x(self) -> int:
        return self.box[0] + self.box[2] // 2

    def enroll_quality(self) -> float:
        """0 = inutilisable, 1 = excellent. Sert à trier les empreintes gardées."""
        size_q = min(1.0, self.size / 160.0)
        sharp_q = min(1.0, self.sharpness / 120.0)
        return max(0.0, 0.5 * size_q + 0.3 * sharp_q + 0.2 * self.score)


@dataclass
class Person:
    id: int
    name: str
    relation: str = ""
    notes: str = ""
    aliases: list[str] = field(default_factory=list)
    created_at: str = ""
    last_seen: str = ""
    seen_count: int = 0
    vectors: int = 0
    is_owner: bool = False

    def label(self) -> str:
        if self.is_owner:
            return f"toi ({self.name})" if self.name else "toi"
        return f"{self.name} ({self.relation})" if self.relation else self.name


@dataclass
class Match:
    face: DetectedFace
    person: Person | None
    similarity: float
    pending_id: str = ""     # rempli pour un inconnu ou un « peut-être »

    @property
    def status(self) -> str:
        if self.person is not None and self.similarity >= SURE_THRESHOLD:
            return "known"
        if self.person is not None and self.similarity >= MAYBE_THRESHOLD:
            return "maybe"
        return "unknown"


@dataclass
class PendingFace:
    id: str
    vectors: list[np.ndarray]
    thumb_jpeg: bytes
    first_seen: float
    last_seen: float
    best_quality: float
    guess: Person | None = None
    guess_similarity: float = 0.0

    def matches(self, vec: np.ndarray) -> float:
        return max(float(np.dot(v, vec)) for v in self.vectors)


# ── Moteur ───────────────────────────────────────────────────────────────────

class FaceEngine:
    """Détection + empreinte. Chargement paresseux, appels sérialisés."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._det = None
        self._rec = None
        self._det_size = (0, 0)
        self.available = False
        self.error = ""

    def _load(self) -> bool:
        if self._det is not None:
            return True
        if self.error:
            return False
        if not ensure_models():
            self.error = "modèles de visage absents (scripts/install_face_memory.sh)"
            return False
        try:
            cv2 = _cv2()
            try:
                cv2.setNumThreads(1)
            except Exception:
                pass
            self._det = cv2.FaceDetectorYN.create(
                str(DETECTOR_PATH), "", (320, 320), MIN_DETECT_SCORE, 0.3, 50,
            )
            self._rec = cv2.FaceRecognizerSF.create(str(RECOGNIZER_PATH), "")
            self.available = True
            return True
        except Exception as exc:
            self.error = f"OpenCV ne charge pas les modèles de visage : {exc}"
            print(f"[Visages] {self.error}")
            return False

    @staticmethod
    def decode(image_bytes: bytes):
        cv2 = _cv2()
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("image illisible")
        return img

    @staticmethod
    def _sharpness(gray) -> float:
        cv2 = _cv2()
        if gray.size == 0:
            return 0.0
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def _thumb(img, box: tuple[int, int, int, int]) -> bytes:
        cv2 = _cv2()
        x, y, w, h = box
        pad = int(0.35 * max(w, h))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return b""
        scale = 160 / max(crop.shape[0], crop.shape[1])
        if scale < 1:
            crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))
        ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else b""

    def detect(self, image_bytes: bytes, *, with_thumbs: bool = True) -> list[DetectedFace]:
        """Visages de l'image, du plus grand au plus petit."""
        if not self._load():
            return []
        cv2 = _cv2()
        img = self.decode(image_bytes)
        h, w = img.shape[:2]
        scale = min(1.0, DETECT_MAX_DIM / max(h, w))
        small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
        faces: list[DetectedFace] = []
        with self._lock:
            size = (small.shape[1], small.shape[0])
            if size != self._det_size:
                self._det.setInputSize(size)
                self._det_size = size
            _, rows = self._det.detect(small)
            if rows is None or len(rows) == 0:
                return []
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            for row in rows:
                row = row.astype(np.float32)
                # Coordonnées ramenées à l'image d'origine pour l'alignement :
                # l'empreinte se calcule sur les vrais pixels, pas la réduction.
                full = row.copy()
                full[:14] = row[:14] / scale
                x, y, bw, bh = (int(full[0]), int(full[1]), int(full[2]), int(full[3]))
                if min(bw, bh) < MIN_FACE_PX or bw <= 0 or bh <= 0:
                    continue
                try:
                    aligned = self._rec.alignCrop(img, full)
                    feat = self._rec.feature(aligned).astype(np.float32).ravel()
                except Exception as exc:
                    print(f"[Visages] empreinte impossible : {exc}")
                    continue
                norm = float(np.linalg.norm(feat)) or 1.0
                feat = feat / norm
                box = (max(0, x), max(0, y), bw, bh)
                sharp = self._sharpness(gray[box[1]:box[1] + bh, box[0]:box[0] + bw])
                faces.append(DetectedFace(
                    box=box, score=float(full[14]), embedding=feat, sharpness=sharp,
                    thumb_jpeg=self._thumb(img, box) if with_thumbs else b"",
                ))
        faces.sort(key=lambda f: f.box[2] * f.box[3], reverse=True)
        return faces


# ── Base des visages ─────────────────────────────────────────────────────────

class FaceStore:
    """SQLite : personnes, empreintes, apparitions. Empreintes en cache RAM."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None       # (n, 128)
        self._owners: list[int] = []                 # person_id par ligne
        self._init()

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    folded TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    aliases TEXT NOT NULL DEFAULT '[]',
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    thumb BLOB,
                    created_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL DEFAULT '',
                    seen_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS people_folded ON people(folded);
                CREATE TABLE IF NOT EXISTS face_vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                    vec BLOB NOT NULL,
                    quality REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS face_vectors_person ON face_vectors(person_id);
                CREATE TABLE IF NOT EXISTS sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                    seen_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    similarity REAL NOT NULL DEFAULT 0
                );
            """)
        self._reload_cache()

    def _reload_cache(self) -> None:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT person_id, vec FROM face_vectors").fetchall()
            if not rows:
                self._matrix, self._owners = None, []
                return
            self._owners = [int(r["person_id"]) for r in rows]
            self._matrix = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])

    @staticmethod
    def _row_to_person(row: sqlite3.Row, vectors: int = 0) -> Person:
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except Exception:
            aliases = []
        return Person(
            id=int(row["id"]), name=str(row["name"]), relation=str(row["relation"] or ""),
            notes=str(row["notes"] or ""), aliases=[str(a) for a in aliases],
            created_at=str(row["created_at"] or ""), last_seen=str(row["last_seen"] or ""),
            seen_count=int(row["seen_count"] or 0), vectors=vectors,
            is_owner=bool(row["is_owner"]),
        )

    def get(self, person_id: int) -> Person | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if row is None:
                return None
            n = conn.execute("SELECT COUNT(*) FROM face_vectors WHERE person_id=?", (person_id,)).fetchone()[0]
            return self._row_to_person(row, int(n))

    def find(self, query: str) -> Person | None:
        """Par nom, alias ou préfixe de nom (« Karim » retrouve « Karim Diallo »)."""
        folded = _fold(query)
        if not folded:
            return None
        if folded in OWNER_WORDS or folded in ("toi", "utilisateur"):
            return self.owner()
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE folded=?", (folded,)).fetchone()
            if row is None:
                for cand in conn.execute("SELECT * FROM people").fetchall():
                    try:
                        aliases = [_fold(a) for a in json.loads(cand["aliases"] or "[]")]
                    except Exception:
                        aliases = []
                    if folded in aliases or cand["folded"].startswith(folded + " ") \
                            or folded.split(" ")[0] == cand["folded"].split(" ")[0]:
                        row = cand
                        break
            if row is None:
                return None
            n = conn.execute("SELECT COUNT(*) FROM face_vectors WHERE person_id=?", (row["id"],)).fetchone()[0]
            return self._row_to_person(row, int(n))

    def owner(self) -> Person | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE is_owner=1").fetchone()
            return self._row_to_person(row) if row is not None else None

    def upsert_person(self, name: str, *, relation: str = "", notes: str = "",
                      is_owner: bool = False, thumb: bytes = b"") -> Person:
        """Crée ou complète une personne. Les champs vides n'écrasent rien."""
        name = " ".join(str(name or "").split())[:80]
        folded = _fold(name)
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE folded=?", (folded,)).fetchone()
            if row is None and is_owner:
                row = conn.execute("SELECT * FROM people WHERE is_owner=1").fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO people(name, folded, relation, notes, is_owner, thumb, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, folded, relation[:80], notes[:400], int(is_owner), thumb or None, _now()),
                )
                pid = int(cur.lastrowid)
            else:
                pid = int(row["id"])
                conn.execute(
                    "UPDATE people SET relation=COALESCE(NULLIF(?, ''), relation), "
                    "notes=CASE WHEN ?='' THEN notes WHEN notes='' THEN ? ELSE notes || ' ; ' || ? END, "
                    "is_owner=MAX(is_owner, ?), thumb=COALESCE(?, thumb) WHERE id=?",
                    (relation[:80], notes[:400], notes[:400], notes[:400], int(is_owner), thumb or None, pid),
                )
            conn.commit()
        return self.get(pid)  # type: ignore[return-value]

    def add_vectors(self, person_id: int, vectors: list[tuple[np.ndarray, float]]) -> int:
        """Ajoute des empreintes ; au-delà du plafond, les moins bonnes partent."""
        if not vectors:
            return 0
        with self._lock, self._conn() as conn:
            now = _now()
            conn.executemany(
                "INSERT INTO face_vectors(person_id, vec, quality, created_at) VALUES (?,?,?,?)",
                [(person_id, np.asarray(v, dtype=np.float32).tobytes(), float(q), now) for v, q in vectors],
            )
            conn.execute(
                "DELETE FROM face_vectors WHERE person_id=? AND id NOT IN ("
                "SELECT id FROM face_vectors WHERE person_id=? ORDER BY quality DESC, id DESC LIMIT ?)",
                (person_id, person_id, MAX_VECTORS_PER_PERSON),
            )
            conn.commit()
        self._reload_cache()
        return len(vectors)

    def record_sighting(self, person_id: int, similarity: float, source: str = "") -> None:
        with self._lock, self._conn() as conn:
            now = _now()
            conn.execute("INSERT INTO sightings(person_id, seen_at, source, similarity) VALUES (?,?,?,?)",
                         (person_id, now, source, float(similarity)))
            conn.execute("UPDATE people SET last_seen=?, seen_count=seen_count+1 WHERE id=?", (now, person_id))
            conn.commit()

    def update_person(self, person_id: int, *, name: str = "", relation: str = "",
                      notes: str = "", alias: str = "") -> Person | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if row is None:
                return None
            try:
                aliases = list(json.loads(row["aliases"] or "[]"))
            except Exception:
                aliases = []
            if name and _fold(name) != row["folded"]:
                aliases.append(row["name"])
            if alias and _fold(alias) not in {_fold(a) for a in aliases}:
                aliases.append(alias)
            conn.execute(
                "UPDATE people SET name=COALESCE(NULLIF(?, ''), name), folded=COALESCE(NULLIF(?, ''), folded), "
                "relation=COALESCE(NULLIF(?, ''), relation), notes=COALESCE(NULLIF(?, ''), notes), aliases=? WHERE id=?",
                (name[:80], _fold(name) if name else "", relation[:80], notes[:400],
                 json.dumps(aliases[-10:], ensure_ascii=False), person_id),
            )
            conn.commit()
        return self.get(person_id)

    def delete(self, person_id: int) -> bool:
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.execute("DELETE FROM people WHERE id=?", (person_id,))
            conn.execute("DELETE FROM face_vectors WHERE person_id=?", (person_id,))
            conn.execute("DELETE FROM sightings WHERE person_id=?", (person_id,))
            conn.commit()
            deleted = cur.rowcount > 0
        self._reload_cache()
        return deleted

    def list_people(self) -> list[Person]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM face_vectors v WHERE v.person_id=p.id) AS n "
                "FROM people p ORDER BY last_seen DESC, name"
            ).fetchall()
            return [self._row_to_person(r, int(r["n"])) for r in rows]

    def thumb(self, person_id: int) -> bytes:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT thumb FROM people WHERE id=?", (person_id,)).fetchone()
            return bytes(row["thumb"]) if row is not None and row["thumb"] else b""

    def best_match(self, vec: np.ndarray) -> tuple[int | None, float]:
        """(person_id, cosinus max) sur toutes les empreintes connues."""
        with self._lock:
            if self._matrix is None or len(self._owners) == 0:
                return None, 0.0
            sims = self._matrix @ np.asarray(vec, dtype=np.float32)
            idx = int(np.argmax(sims))
            return self._owners[idx], float(sims[idx])

    def count(self) -> int:
        with self._lock, self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0])


# ── Façade ───────────────────────────────────────────────────────────────────

class FaceMemory:
    """Ce que les actions et le répartiteur utilisent : identifier, retenir, oublier."""

    def __init__(self, store: FaceStore | None = None, engine: FaceEngine | None = None) -> None:
        self.store = store or FaceStore()
        self.engine = engine or FaceEngine()
        self._pending: dict[str, PendingFace] = {}
        self._pending_seq = 0
        self._lock = threading.Lock()

    # ── attente des inconnus ────────────────────────────────────────────

    def _prune_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_S
        for key in [k for k, p in self._pending.items() if p.last_seen < cutoff]:
            self._pending.pop(key, None)

    def _remember_pending(self, face: DetectedFace, guess: Person | None, sim: float) -> str:
        with self._lock:
            self._prune_pending()
            now = time.monotonic()
            for pend in self._pending.values():
                if pend.matches(face.embedding) >= PENDING_MERGE_THRESHOLD:
                    if len(pend.vectors) < MAX_PENDING_VECTORS:
                        pend.vectors.append(face.embedding)
                    pend.last_seen = now
                    if face.enroll_quality() > pend.best_quality and face.thumb_jpeg:
                        pend.best_quality = face.enroll_quality()
                        pend.thumb_jpeg = face.thumb_jpeg
                    if guess is not None and sim > pend.guess_similarity:
                        pend.guess, pend.guess_similarity = guess, sim
                    return pend.id
            self._pending_seq += 1
            pid = f"V{self._pending_seq}"
            self._pending[pid] = PendingFace(
                id=pid, vectors=[face.embedding], thumb_jpeg=face.thumb_jpeg,
                first_seen=now, last_seen=now, best_quality=face.enroll_quality(),
                guess=guess, guess_similarity=sim,
            )
            return pid

    def pending(self) -> list[PendingFace]:
        with self._lock:
            self._prune_pending()
            return sorted(self._pending.values(), key=lambda p: p.last_seen, reverse=True)

    def clear_pending(self) -> None:
        with self._lock:
            self._pending.clear()

    # ── identification ──────────────────────────────────────────────────

    def identify(self, image_bytes: bytes, *, source: str = "camera",
                 record: bool = True, faces: list[DetectedFace] | None = None) -> list[Match]:
        """Qui est sur l'image. Les inconnus passent en attente d'un nom.

        `faces` évite une seconde détection quand l'appelant l'a déjà faite.
        """
        if faces is None:
            faces = self.engine.detect(image_bytes)
        matches: list[Match] = []
        for face in faces:
            pid, sim = self.store.best_match(face.embedding)
            person = self.store.get(pid) if pid is not None and sim >= MAYBE_THRESHOLD else None
            match = Match(face=face, person=person, similarity=sim)
            if match.status == "known":
                if record:
                    self.store.record_sighting(person.id, sim, source)  # type: ignore[union-attr]
                    # Une rencontre nette enrichit le profil : la personne sera
                    # reconnue sous cet angle-là aussi la prochaine fois.
                    if face.enroll_quality() >= 0.45 and sim < 0.80:
                        self.store.add_vectors(person.id, [(face.embedding, face.enroll_quality())])  # type: ignore[union-attr]
            else:
                match.pending_id = self._remember_pending(face, person, sim)
            matches.append(match)
        return matches

    def status(self) -> dict[str, Any]:
        return {
            "models": models_present(),
            "engine": self.engine.available or not self.engine.error,
            "error": self.engine.error,
            "people": self.store.count(),
            "pending": len(self.pending()),
        }

    # ── inscription ─────────────────────────────────────────────────────

    def _write_long_term(self, person: Person, *, first: bool) -> None:
        """La mémoire générale doit aussi savoir que cette personne existe."""
        try:
            from core import memory_store
            rel = f", {person.relation}" if person.relation else ""
            who = "l'utilisateur lui-même" if person.is_owner else f"{person.name}{rel}"
            note = f" — {person.notes}" if person.notes else ""
            text = (f"Visage connu : {who}{note}. ANO-GPT reconnaît cette personne à la caméra "
                    f"(première rencontre le {person.created_at[:10] or time.strftime('%Y-%m-%d')}).")
            memory_store.save(text, kind=memory_store.KIND_PROFILE,
                              key=f"visage_{slug(person.name)}", category="relationships")
        except Exception as exc:
            print(f"[Visages] mémoire longue durée indisponible : {exc}")

    def enroll(self, name: str, *, relation: str = "", notes: str = "",
               faces: list[DetectedFace] | None = None, pending_id: str = "",
               is_owner: bool = False) -> tuple[Person, int, bool]:
        """Associe un nom à des empreintes. Rend (personne, nb empreintes, nouvelle ?).

        Les empreintes viennent d'un dossier en attente (réponse à « c'est
        qui ? ») ou de visages capturés à l'instant. Un nom déjà connu reçoit
        simplement des empreintes de plus.
        """
        if _fold(name) in OWNER_WORDS:
            is_owner = True
            owner = self.store.owner()
            name = owner.name if owner else self._owner_name_from_profile()
        vectors: list[tuple[np.ndarray, float]] = []
        thumb = b""
        if pending_id:
            with self._lock:
                pend = self._pending.pop(pending_id, None)
            if pend is not None:
                vectors.extend((v, max(0.3, pend.best_quality)) for v in pend.vectors)
                thumb = pend.thumb_jpeg
        for face in faces or []:
            if face.size >= MIN_ENROLL_FACE_PX and face.sharpness >= MIN_SHARPNESS:
                vectors.append((face.embedding, face.enroll_quality()))
                if not thumb or face.enroll_quality() > 0.6:
                    thumb = face.thumb_jpeg or thumb
        if not vectors:
            raise ValueError("aucune empreinte exploitable")
        existed = self.store.find(name) is not None
        person = self.store.upsert_person(name, relation=relation, notes=notes,
                                          is_owner=is_owner, thumb=thumb)
        added = self.store.add_vectors(person.id, vectors)
        # Les autres dossiers en attente qui étaient ce même visage n'ont plus
        # lieu d'être.
        with self._lock:
            for key, pend in list(self._pending.items()):
                if any(pend.matches(v) >= PENDING_MERGE_THRESHOLD for v, _ in vectors):
                    self._pending.pop(key, None)
        person = self.store.get(person.id) or person
        self._write_long_term(person, first=not existed)
        return person, added, not existed

    @staticmethod
    def _owner_name_from_profile() -> str:
        # L'empreinte vocale connaît déjà le prénom de l'utilisateur.
        try:
            from core import speaker_id
            if speaker_id.PROFILE_PATH.is_file():
                data = np.load(speaker_id.PROFILE_PATH, allow_pickle=False)
                name = str(data["name"]).strip()
                if name:
                    return name
        except Exception:
            pass
        return "Toi"

    def forget(self, query: str) -> Person | None:
        person = self.store.find(query)
        if person is None:
            return None
        self.store.delete(person.id)
        try:
            from core import memory_store
            memory_store.forget(f"visage_{slug(person.name)}")
        except Exception:
            pass
        return person

    # ── texte pour le modèle ────────────────────────────────────────────

    @staticmethod
    def _ago(stamp: str) -> str:
        if not stamp:
            return ""
        try:
            then = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return ""
        delta = max(0, time.time() - then)
        if delta < 90:
            return "à l'instant"
        if delta < 3600:
            return f"il y a {int(delta // 60)} min"
        if delta < 86400:
            return f"il y a {int(delta // 3600)} h"
        days = int(delta // 86400)
        return "hier" if days == 1 else f"il y a {days} jours"

    def describe(self, matches: list[Match], *, position: bool = True) -> str:
        """Bloc lisible par le modèle : qui est là, et quoi demander."""
        if not matches:
            return "Aucun visage détecté sur l'image."
        several = len(matches) > 1
        xs = sorted(m.face.center_x for m in matches)
        lines = [f"{len(matches)} visage(s) détecté(s) :"]
        for m in matches:
            where = ""
            if several and position:
                rank = xs.index(m.face.center_x)
                where = " (à gauche)" if rank == 0 else (" (à droite)" if rank == len(xs) - 1 else " (au centre)")
            if m.status == "known" and m.person is not None:
                p = m.person
                seen = f", vu {p.seen_count} fois" if p.seen_count > 1 else ""
                last = self._ago(p.last_seen)
                last = f", dernière fois {last}" if last and p.seen_count > 1 else ""
                notes = f". Notes : {p.notes}" if p.notes else ""
                lines.append(f"- CONNU{where} : {p.label()} (certitude {m.similarity:.2f}{seen}{last}){notes}")
            elif m.status == "maybe" and m.person is not None:
                lines.append(
                    f"- PEUT-ÊTRE{where} : ressemble à {m.person.label()} (similarité {m.similarity:.2f}, "
                    f"pas sûr). Dossier {m.pending_id}. Demande confirmation ; si oui ⇒ "
                    f"remember_person name=\"{m.person.name}\" pending_id={m.pending_id}."
                )
            else:
                lines.append(
                    f"- INCONNU{where} : personne jamais vue. Dossier {m.pending_id}. "
                    f"Demande à l'utilisateur qui c'est ; à sa réponse ⇒ remember_person "
                    f"name=… relation=… pending_id={m.pending_id}."
                )
        return "\n".join(lines)


# ── Veille caméra ────────────────────────────────────────────────────────────

class FaceWatcher:
    """Reconnaît en continu sur le flux caméra ouvert et annonce les arrivées.

    Une image toutes les `interval` secondes, un seul fil : sur cette machine
    c'est ~5 % d'un cœur, la voix n'en souffre pas. Chaque personne connue
    n'est annoncée qu'une fois par `cooldown`, un inconnu une fois par
    `unknown_cooldown`.
    """

    def __init__(self, memory: FaceMemory, grab_frame: Callable[[], bytes | None],
                 on_event: Callable[[str, list[Match]], None], *,
                 interval: float = 1.5, cooldown: float = 300.0,
                 unknown_cooldown: float = 120.0) -> None:
        self.memory = memory
        self.grab_frame = grab_frame
        self.on_event = on_event
        self.interval = interval
        self.cooldown = cooldown
        self.unknown_cooldown = unknown_cooldown
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_announced: dict[str, float] = {}
        self.started_at = 0.0
        self.frames = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="face-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                frame = self.grab_frame()
                if not frame:
                    continue
                self.frames += 1
                matches = self.memory.identify(frame, source="veille")
                self._announce(matches)
            except Exception as exc:
                print(f"[Visages] veille : {exc}")

    def _announce(self, matches: list[Match]) -> None:
        now = time.monotonic()
        fresh: list[Match] = []
        for m in matches:
            if m.status == "known" and m.person is not None:
                key, cool = f"p{m.person.id}", self.cooldown
            else:
                key, cool = f"u{m.pending_id}", self.unknown_cooldown
            if now - self._last_announced.get(key, -1e9) >= cool:
                self._last_announced[key] = now
                fresh.append(m)
        if fresh:
            self.on_event("faces", fresh)


# ── Singleton ────────────────────────────────────────────────────────────────

_memory: FaceMemory | None = None
_memory_lock = threading.Lock()


def get_face_memory() -> FaceMemory:
    global _memory
    with _memory_lock:
        if _memory is None:
            _memory = FaceMemory()
        return _memory


def faces_block_for_vision(image_bytes: bytes) -> str:
    """Bloc court ajouté aux analyses caméra : le modèle sait qui est devant lui.

    Ne déclenche jamais un téléchargement de modèle : une analyse d'écran ne
    doit pas attendre 37 Mo. Rend une chaîne vide si rien n'est prêt.
    """
    if not models_present():
        return ""
    try:
        mem = get_face_memory()
        matches = mem.identify(image_bytes, source="vision")
        print(f"[Visages] {len(matches)} visage(s) — "
              + ", ".join(m.person.name if m.person else f"inconnu {m.pending_id}" for m in matches))
        if not matches:
            return ""
        return "[PERSONNES RECONNUES — mémoire des visages]\n" + mem.describe(matches)
    except Exception as exc:
        print(f"[Visages] identification pendant la vision impossible : {exc}")
        return ""


def thumb_markdown(jpeg: bytes, alt: str = "visage") -> str:
    if not jpeg:
        return ""
    return f"![{alt}](data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')})"
