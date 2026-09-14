"""Mémoire des visages : connu, inconnu en attente, inscription, veille, outil vocal."""

from __future__ import annotations

import numpy as np
import pytest

from actions import visual_recognition as vr
from core import face_memory as fm
from core import memory_store


def _vec(seed: int, noise: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=128).astype(np.float32)
    if noise:
        v = v + np.random.default_rng(seed * 7 + 1).normal(scale=noise, size=128).astype(np.float32)
    return v / np.linalg.norm(v)


def _face(seed: int, *, noise: float = 0.0, x: int = 100, size: int = 120, sharp: float = 80.0) -> fm.DetectedFace:
    return fm.DetectedFace(box=(x, 50, size, size), score=0.95, embedding=_vec(seed, noise),
                           sharpness=sharp, thumb_jpeg=b"\xff\xd8thumb")


class FakeEngine:
    """Rend les visages programmés pour la prochaine image, sans OpenCV."""

    def __init__(self):
        self.queue: list[list[fm.DetectedFace]] = []
        self.available = True
        self.error = ""

    def detect(self, image_bytes: bytes, *, with_thumbs: bool = True):
        if not self.queue:
            return []
        return self.queue.pop(0)


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_keep_photo", lambda *args, **kwargs: "")
    saved: list[tuple] = []
    monkeypatch.setattr(memory_store, "save", lambda value, **kw: saved.append((value, kw)) or "ok")
    monkeypatch.setattr(memory_store, "forget", lambda key: "ok")
    engine = FakeEngine()
    memory = fm.FaceMemory(store=fm.FaceStore(tmp_path / "faces.db"), engine=engine)
    memory.saved_long_term = saved  # type: ignore[attr-defined]
    monkeypatch.setattr(fm, "_memory", memory)
    monkeypatch.setattr(fm, "models_present", lambda: True)
    monkeypatch.setattr(fm, "ensure_models", lambda progress=None: True)
    return memory


# ── base ────────────────────────────────────────────────────────────────────

def test_store_finds_by_name_prefix_alias_and_owner(tmp_path):
    store = fm.FaceStore(tmp_path / "f.db")
    karim = store.upsert_person("Karim Diallo", relation="frère")
    store.add_vectors(karim.id, [(_vec(1), 0.9)])
    assert store.find("karim").id == karim.id
    assert store.find("KARIM DIALLO").relation == "frère"
    store.update_person(karim.id, alias="Kaka")
    assert store.find("kaka").id == karim.id
    assert store.find("Fatou") is None
    me = store.upsert_person("Abdoul", is_owner=True)
    assert store.find("moi").id == me.id and store.owner().is_owner
    pid, sim = store.best_match(_vec(1, noise=0.05))
    assert pid == karim.id and sim > fm.SURE_THRESHOLD
    assert store.delete(karim.id) and store.find("karim") is None
    assert store.best_match(_vec(1)) == (None, 0.0)


def test_vectors_are_capped_keeping_best_quality(tmp_path):
    store = fm.FaceStore(tmp_path / "f.db")
    p = store.upsert_person("Ali")
    store.add_vectors(p.id, [(_vec(i), 0.1 if i < 5 else 0.9) for i in range(fm.MAX_VECTORS_PER_PERSON + 5)])
    assert store.get(p.id).vectors == fm.MAX_VECTORS_PER_PERSON
    # les cinq de faible qualité sont parties
    assert store.best_match(_vec(0))[1] < fm.MAYBE_THRESHOLD


# ── identification ──────────────────────────────────────────────────────────

def test_unknown_face_waits_for_a_name_then_is_enrolled(mem):
    mem.engine.queue.append([_face(3)])
    matches = mem.identify(b"img")
    assert len(matches) == 1 and matches[0].status == "unknown"
    pending_id = matches[0].pending_id
    assert pending_id.startswith("V") and len(mem.pending()) == 1
    text = mem.describe(matches)
    assert "INCONNU" in text and pending_id in text and "remember_person" in text

    # même inconnu revu : même dossier, pas un second
    mem.engine.queue.append([_face(3, noise=0.05)])
    again = mem.identify(b"img")
    assert again[0].pending_id == pending_id and len(mem.pending()) == 1

    person, added, new = mem.enroll("Karim", relation="frère", pending_id=pending_id)
    assert new and added == 2 and person.relation == "frère"
    assert mem.pending() == []
    assert mem.saved_long_term and "Karim" in mem.saved_long_term[0][0]
    assert mem.saved_long_term[0][1]["key"] == "visage_karim"

    mem.engine.queue.append([_face(3, noise=0.05)])
    known = mem.identify(b"img")
    assert known[0].status == "known" and known[0].person.name == "Karim"
    assert "CONNU" in mem.describe(known) and "Karim (frère)" in mem.describe(known)
    assert mem.store.get(person.id).seen_count == 1


def test_enrolling_an_existing_name_adds_vectors(mem):
    mem.enroll("Karim", faces=[_face(3)])
    person, added, new = mem.enroll("karim", faces=[_face(3, noise=0.2)])
    assert not new and added == 1 and person.vectors == 2


def test_enroll_rejects_blurry_or_tiny_faces(mem):
    with pytest.raises(ValueError):
        mem.enroll("Flou", faces=[_face(4, sharp=2.0)])
    with pytest.raises(ValueError):
        mem.enroll("Petit", faces=[_face(4, size=30)])


def test_owner_is_enrolled_with_moi(mem):
    person, _, _ = mem.enroll("moi", faces=[_face(9)])
    assert person.is_owner
    mem.engine.queue.append([_face(9, noise=0.05)])
    assert mem.describe(mem.identify(b"img")).count("toi") >= 1


def test_positions_left_right_in_description(mem):
    mem.enroll("Karim", faces=[_face(3)])
    mem.engine.queue.append([_face(3, noise=0.05, x=500), _face(8, x=20)])
    text = mem.describe(mem.identify(b"img"))
    assert "Karim (à droite)" in text.replace("CONNU (à droite) : Karim", "Karim (à droite)") or "à droite" in text
    assert "à gauche" in text and "INCONNU" in text


def test_forget_removes_person_and_long_term_key(mem):
    mem.enroll("Karim", faces=[_face(3)])
    assert mem.forget("karim").name == "Karim"
    assert mem.forget("karim") is None


# ── veille ──────────────────────────────────────────────────────────────────

def test_watcher_announces_each_person_once_per_cooldown(mem):
    events: list[list[fm.Match]] = []
    watcher = fm.FaceWatcher(mem, lambda: b"img", lambda kind, m: events.append(m), cooldown=300)
    mem.enroll("Karim", faces=[_face(3)])
    known = fm.Match(face=_face(3), person=mem.store.find("Karim"), similarity=0.9)
    watcher._announce([known])
    watcher._announce([known])
    assert len(events) == 1
    unknown = fm.Match(face=_face(5), person=None, similarity=0.1, pending_id="V9")
    watcher._announce([known, unknown])
    assert len(events) == 1  # Une seule apparition ne suffit pas.
    watcher._announce([known, unknown])
    assert len(events) == 2 and events[1][0].pending_id == "V9"


# ── outil ───────────────────────────────────────────────────────────────────

class Player:
    def __init__(self):
        self.cards = []
        self.logs = []

    def show_card(self, *a, **k):
        self.cards.append(a)

    def write_log(self, msg):
        self.logs.append(msg)


def test_tool_identify_then_remember_from_pending(mem):
    player, session = Player(), {}
    mem.engine.queue.append([_face(3)])
    out = vr.visual_recognition({"action": "identify"}, player=player, session_memory=session,
                                grab_frame=lambda: (b"img", "image/jpeg"))
    assert "INCONNU" in out and session[vr.LAST_FACES_KEY][0]["status"] == "unknown"
    assert player.cards and "Inconnu" in player.cards[0][2]
    pending_id = session[vr.LAST_FACES_KEY][0]["pending_id"]

    out = vr.visual_recognition({"action": "remember_person", "name": "Karim", "relation": "frère",
                                 "pending_id": pending_id}, player=player, session_memory=session)
    assert "retenu" in out and "Karim (frère)" in out
    assert "Karim" in vr.visual_recognition({"action": "list_people"})

    mem.engine.queue.append([_face(3, noise=0.05)])
    out = vr.visual_recognition({"action": "identify"}, player=player, session_memory=session,
                                grab_frame=lambda: (b"img", "image/jpeg"))
    assert "CONNU" in out and "Karim" in out


def test_tool_remember_without_pending_captures_now(mem):
    mem.engine.queue.extend([[_face(6)], [_face(6, noise=0.05)]] * 3)
    out = vr.visual_recognition({"action": "remember_person", "name": "Fatou", "relation": "collègue"},
                                grab_frame=lambda: (b"img", "image/jpeg"))
    assert "Fatou" in out and mem.store.find("Fatou").vectors >= 1


def test_tool_object_path_uses_gemini_search_and_personal_memory(mem, monkeypatch):
    monkeypatch.setattr(vr, "identify_object", lambda img, mime, q="": {
        "name": "Arduino Uno R3", "brand": "Arduino", "model": "Uno R3", "confidence": 0.9,
        "visible_text": "ARDUINO UNO", "description": "Carte microcontrôleur.", "search_query": "Arduino Uno R3",
        "model_used": "gemini-test",
    })
    monkeypatch.setattr(vr, "_search_object", lambda q, budget_s=10.0: f"résultats pour {q} : ~25 €")
    monkeypatch.setattr(memory_store, "search", lambda q, limit=5, **kw: [{"id": 1, "value": "Mon Arduino sert au projet serre."}])
    session, player = {}, Player()
    out = vr.visual_recognition({"action": "identify", "question": "c'est quoi ça et quel est son prix ?"}, player=player,
                                session_memory=session, grab_frame=lambda: (b"img", "image/jpeg"))
    assert "[OBJET IDENTIFIÉ" in out and "Arduino Uno R3" in out and "25 €" in out
    assert "projet serre" in out and session[vr.LAST_OBJECT_KEY]["brand"] == "Arduino"
    assert any("Objet identifié" in c[1] for c in player.cards)

    saved = []
    monkeypatch.setattr(memory_store, "save", lambda value, **kw: saved.append((value, kw)) or "ok")
    out = vr.visual_recognition({"action": "remember_object", "notes": "celui du projet serre"}, session_memory=session)
    assert "noté" in out and saved[0][1]["key"] == "objet_arduino_uno_r3" and "projet serre" in saved[0][0]


def test_tool_expect_person_without_face_asks_to_come_closer(mem):
    out = vr.visual_recognition({"action": "identify", "expect": "person"}, grab_frame=lambda: (b"img", "image/jpeg"))
    assert "Aucun visage" in out


def test_tool_forget_update_status(mem):
    mem.enroll("Karim", faces=[_face(3)])
    assert "Karim Diallo" in vr.visual_recognition({"action": "update_person", "name": "Karim", "new_name": "Karim Diallo", "notes": "habite à Lyon"})
    assert mem.store.find("Karim").name == "Karim Diallo"
    assert "visage(s) connu(s)" in vr.visual_recognition({"action": "status"})
    assert "oublié" in vr.visual_recognition({"action": "forget_person", "name": "Karim"})
    assert "aucun visage" in vr.visual_recognition({"action": "forget_person", "name": "Karim"})


def test_tool_watch_needs_camera_and_stops(mem, monkeypatch):
    assert "flux caméra" in vr.visual_recognition({"action": "watch"})
    out = vr.visual_recognition({"action": "watch"}, grab_frame=lambda: b"img", speak=lambda t: None)
    assert "activée" in out and vr.watcher_running()
    assert "arrêtée" in vr.visual_recognition({"action": "stop_watch"})
    assert not vr.watcher_running()


def test_vision_block_is_empty_without_models(monkeypatch):
    monkeypatch.setattr(fm, "models_present", lambda: False)
    assert fm.faces_block_for_vision(b"img") == ""


def test_pack_and_declaration():
    from core import tool_packs as tp
    from core.tool_dispatcher import TOOL_DECLARATIONS, _TOOL_LABELS
    # Dans le noyau : disponible dès la première phrase, sans reconnexion.
    assert "visual_recognition" in tp.CORE
    assert "visual_recognition" in {d["name"] for d in tp.select_declarations(TOOL_DECLARATIONS, frozenset())}
    assert any(d["name"] == "visual_recognition" for d in TOOL_DECLARATIONS)
    assert "visual_recognition" in _TOOL_LABELS


def test_simple_object_identification_does_not_search(mem, monkeypatch):
    monkeypatch.setattr(vr, "identify_object", lambda *args: {"name": "objet", "confidence": 0.9})
    def unexpected_search(*args, **kwargs):
        pytest.fail("Une identification simple ne doit pas attendre le web")
    monkeypatch.setattr(vr, "_search_object", unexpected_search)
    assert "objet" in vr.visual_recognition({"action": "identify", "question": "c'est quoi ça ?"},
                                           grab_frame=lambda: b"img")


def test_identical_question_rechecks_changed_scene(mem, monkeypatch):
    seen = []
    def identify(image, *args):
        seen.append(image)
        return {"name": image.decode(), "confidence": 0.9}
    monkeypatch.setattr(vr, "identify_object", identify)
    for image in (b"premier", b"second"):
        report = vr.visual_recognition({"action": "identify", "expect": "object"},
                                       grab_frame=lambda image=image: image)
        assert image.decode() in report
    assert seen == [b"premier", b"second"]


def test_two_similar_people_require_confirmation(mem):
    mem.enroll("Karim", faces=[_face(3)])
    mem.enroll("Ali", faces=[_face(3, noise=.08)])
    result = mem.identify(b"img", faces=[_face(3)])[0]
    assert result.similarity > fm.SURE_THRESHOLD
    assert result.margin < fm.IDENTITY_MARGIN
    assert result.status == "maybe"
    assert all(p.seen_count == 0 for p in mem.store.list_people())


def test_runner_up_excludes_other_photos_of_same_person(mem):
    mem.enroll("Karim", faces=[_face(3), _face(3, noise=.1)])
    result = mem.identify(b"img", faces=[_face(3, noise=.05)])[0]
    assert result.status == "known"


def test_identifying_never_silently_trains_a_profile(mem):
    person, _, _ = mem.enroll("Karim", faces=[_face(3)])
    # Empreinte ressemblante mais assez différente pour déclencher l'ancien
    # apprentissage automatique (similarité entre 0.42 et 0.80).
    candidate = _face(3, noise=1.0)
    for _ in range(4):
        assert mem.identify(b"img", faces=[candidate])[0].status == "known"
    assert mem.store.get(person.id).vectors == 1


@pytest.mark.parametrize("kwargs", [{"sharp": 2}, {"size": 30}])
def test_bad_capture_cannot_be_enrolled_through_pending(mem, kwargs):
    result = mem.identify(b"img", faces=[_face(3, **kwargs)])[0]
    assert result.status == "unknown" and not result.pending_id
    assert "NON IDENTIFIABLE" in mem.describe([result])
    assert not mem.pending()


@pytest.mark.parametrize("vector", [np.zeros(128), np.full(128, np.nan), np.ones(127), np.full(128, np.inf)])
def test_invalid_embeddings_rejected_without_creating_person(mem, vector):
    face = _face(3)
    face.embedding = vector
    with pytest.raises(ValueError, match="empreinte"):
        mem.enroll("Karim", faces=[face])
    assert mem.store.count() == 0
    result = mem.identify(b"img", faces=[face])[0]
    assert result.status == "unknown" and not result.pending_id


def test_normalization_and_duplicate_suppression(mem):
    face = _face(3)
    face.embedding *= 20
    person, added, _ = mem.enroll("Karim", faces=[face, face])
    assert added == 1
    assert mem.store.best_match(_vec(3))[1] == pytest.approx(1)
    assert mem.store.add_vectors(person.id, [(_vec(3) * 5, .8)]) == 0
    assert mem.store.get(person.id).vectors == 1


def test_corrupt_stored_vector_does_not_break_valid_profiles(mem):
    person, _, _ = mem.enroll("Karim", faces=[_face(3)])
    with mem.store._conn() as conn:
        conn.execute("INSERT INTO face_vectors(person_id, vec, quality, created_at) VALUES (?, ?, ?, ?)",
                     (person.id, b"broken", .9, "test"))
    mem.store._reload_cache()
    assert mem.store.best_match(_vec(3))[0] == person.id


def test_failed_enrollment_preserves_pending(mem, monkeypatch):
    result = mem.identify(b"img", faces=[_face(3)])[0]
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(mem.store, "upsert_person", fail)
    with pytest.raises(OSError):
        mem.enroll("Karim", pending_id=result.pending_id)
    assert mem.pending()[0].id == result.pending_id


def test_pending_expiry_checked_before_enrollment(mem, monkeypatch):
    result = mem.identify(b"img", faces=[_face(3)])[0]
    pending = mem.pending()[0]
    monkeypatch.setattr(fm.time, "monotonic", lambda: pending.last_seen + fm.PENDING_TTL_S + 1)
    with pytest.raises(ValueError):
        mem.enroll("Karim", pending_id=result.pending_id)
    assert mem.store.count() == 0


def test_pending_count_is_bounded(mem, monkeypatch):
    monkeypatch.setattr(fm, "MAX_PENDING_FACES", 3)
    for seed in range(8):
        mem.identify(b"img", faces=[_face(seed)])
    assert len(mem.pending()) == 3


def test_mixed_people_are_not_enrolled_together(mem):
    with pytest.raises(ValueError, match="captures incohérentes"):
        mem.enroll("Karim", faces=[_face(3), _face(8)])
    assert mem.store.count() == 0


def test_watcher_requires_consecutive_observations_and_limits_writes(mem):
    person, _, _ = mem.enroll("Karim", faces=[_face(3)])
    known = fm.Match(_face(3), person, .9)
    events = []
    watcher = fm.FaceWatcher(mem, lambda: b"", lambda *event: events.append(event))
    watcher._announce([known])
    watcher._announce([])
    watcher._announce([known])
    assert not events
    watcher._announce([known])
    watcher._announce([known])
    assert len(events) == 1
    assert mem.store.get(person.id).seen_count == 1


def test_watcher_stop_keeps_reference_to_unfinished_worker(mem):
    class Worker:
        def join(self, timeout):
            pass
        def is_alive(self):
            return True
    watcher = fm.FaceWatcher(mem, lambda: b"", lambda *args: None)
    worker = Worker()
    watcher._thread = worker
    watcher.stop()
    assert watcher._thread is worker and watcher.running


def test_tool_refuses_ambiguous_group_enrollment(mem, monkeypatch):
    monkeypatch.setattr(vr, "_grab_best", lambda *a, **k: (b"img", "image/jpeg", [_face(3), _face(8)]))
    answer = vr.visual_recognition({"action": "remember_person", "name": "Karim"}, grab_frame=lambda: b"img")
    assert "Plusieurs visages" in answer
    assert mem.store.count() == 0


def test_two_faces_in_one_frame_never_share_a_pending_identity(mem):
    matches = mem.identify(b"img", faces=[_face(3, x=10), _face(3, noise=.1, x=300)])
    assert len({match.pending_id for match in matches}) == 2
    mem.enroll("Karim", pending_id=matches[0].pending_id)
    assert [pending.id for pending in mem.pending()] == [matches[1].pending_id]


def test_unusable_face_card_asks_for_better_capture(mem):
    matches = mem.identify(b"img", faces=[_face(3, sharp=2)])
    player = Player()
    vr._show_faces_card(player, matches)
    assert "Image à reprendre" in player.cards[0][2]
    assert "dis-moi qui" not in player.cards[0][2]


def test_model_loading_is_serialized_and_published_together(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    started, release = threading.Event(), threading.Event()
    calls = []
    detector, recognizer = object(), object()
    def make_detector(*args):
        calls.append("detector")
        started.set()
        assert release.wait(2)
        return detector
    def make_recognizer(*args):
        calls.append("recognizer")
        return recognizer
    monkeypatch.setattr(fm, "ensure_models", lambda: True)
    monkeypatch.setattr(fm, "_cv2", lambda: SimpleNamespace(
        setNumThreads=lambda count: None,
        FaceDetectorYN=SimpleNamespace(create=make_detector),
        FaceRecognizerSF=SimpleNamespace(create=make_recognizer)))
    engine = fm.FaceEngine()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(engine._load)
        try:
            assert started.wait(1)
            assert engine._det is None and engine._rec is None
            second = pool.submit(engine._load)
        finally:
            release.set()
        assert first.result(timeout=2) and second.result(timeout=2)
    assert calls == ["detector", "recognizer"]
