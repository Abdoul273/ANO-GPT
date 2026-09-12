import time

import pytest

from core.camera_studio import CameraStudio, CameraUnavailable, SOURCE_PHONE


def _jpeg(width: int = 160, height: int = 120, shade: int = 90) -> bytes:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    frame = np.full((height, width, 3), shade, dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


@pytest.fixture
def studio(tmp_path):
    frames: list[bytes] = []
    started: list[bool] = []
    cam = CameraStudio(
        on_frame=frames.append,
        phone_start=lambda: started.append(True),
        uploads_dir=tmp_path,
    )
    cam.frames_seen = frames        # exposé pour les assertions
    cam.phone_starts = started
    yield cam
    cam.close()


def test_phone_source_streams_and_photographs(studio, tmp_path):
    studio.push_phone_frame(_jpeg())
    studio.open(SOURCE_PHONE)

    assert studio.active
    assert studio.phone_starts, "le téléphone doit recevoir l'ordre de filmer"

    deadline = time.monotonic() + 3
    while not studio.frames_seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert studio.frames_seen, "aucune image n'est parvenue à l'interface"

    photo = studio.photo()
    assert photo.exists() and photo.suffix == ".jpg"
    assert photo.parent == tmp_path


def test_video_recording_produces_a_readable_file(studio, tmp_path):
    studio.push_phone_frame(_jpeg())
    studio.open(SOURCE_PHONE)

    path = studio.start_video()
    assert studio.recording
    assert studio.state()["recording"] is True

    # Alimenter la source pendant l'enregistrement, comme le ferait le
    # téléphone : sans nouvelle image le fichier resterait vide.
    for shade in (60, 120, 180, 200):
        studio.push_phone_frame(_jpeg(shade=shade))
        time.sleep(0.15)

    closed = studio.stop_video()
    assert closed == path
    assert not studio.recording
    assert path.exists() and path.stat().st_size > 0

    cv2 = pytest.importorskip("cv2")
    capture = cv2.VideoCapture(str(path))
    try:
        ok, frame = capture.read()
        assert ok and frame is not None, "la vidéo enregistrée est illisible"
    finally:
        capture.release()


def test_second_recording_is_refused_while_one_runs(studio):
    studio.push_phone_frame(_jpeg())
    studio.open(SOURCE_PHONE)
    studio.start_video()
    with pytest.raises(CameraUnavailable):
        studio.start_video()


def test_photo_without_any_frame_is_reported(tmp_path):
    cam = CameraStudio(uploads_dir=tmp_path)
    with pytest.raises(CameraUnavailable):
        cam.photo()


def test_stale_phone_frames_are_not_treated_as_live(studio):
    studio.push_phone_frame(_jpeg())
    assert studio.phone_online()
    # Une image vieille de plusieurs secondes signifie que le téléphone a
    # décroché : continuer à l'afficher ferait passer un flux mort pour vivant.
    assert not studio.phone_online(max_age=0.0)


def test_closing_releases_the_worker(studio):
    studio.push_phone_frame(_jpeg())
    studio.open(SOURCE_PHONE)
    studio.close()
    assert not studio.active
    assert studio.state()["active"] is False
