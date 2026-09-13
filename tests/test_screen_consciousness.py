"""tests/test_screen_consciousness.py — Veille visuelle ROI / pHash / SSIM / Live."""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from PIL import Image, ImageDraw

from core.event_bus import ScreenChangeEvent
from core.screen_capture import WindowInfo
from core.screen_consciousness import (
    DEFAULT_INTERVAL_S,
    PROBE_SCALE,
    SCREEN_TICK_STALL_S,
    WEBP_MAX_BYTES,
    WEBP_MAX_SIDE,
    WEBP_METHOD,
    WEBP_QUALITY,
    ScreenConsciousness,
    ScreenSnapshot,
    TickStats,
    benchmark_cpu,
    compress_webp,
    diff_snapshots,
    extract_signals,
    hamming_distance,
    perceptual_hash,
    snapshot_capture_scale,
    ssim_score,
    wants_screen_context,
)


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (320, 180)) -> Image.Image:
    return Image.new("RGB", size, color)


def _jpeg(image: Image.Image, quality: int = 80) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _terminal(text: str = "error: failed to compile main.py", color: str = "red") -> Image.Image:
    img = Image.new("RGB", (640, 360), (16, 16, 20))
    draw = ImageDraw.Draw(img)
    fill = (220, 60, 60) if color == "red" else (180, 220, 180)
    draw.text((16, 40), text, fill=fill)
    return img


def _window(
    cls: str = "kitty",
    title: str = "zsh",
    address: str = "0x1",
    size: tuple[int, int] = (800, 600),
) -> WindowInfo:
    return WindowInfo(
        address=address,
        window_class=cls,
        title=title,
        at=(10, 20),
        size=size,
    )


def test_phash_identical_images_have_distance_zero():
    img = _solid((40, 80, 120))
    a = perceptual_hash(img)
    b = perceptual_hash(img.copy())
    assert a == b
    assert hamming_distance(a, b) == 0


def test_phash_detects_a_real_visual_change():
    # Deux aplatis ne diffèrent que par la DC, que le pHash ignore.
    # Un damier vs un bloc de texte ont des structures DCT distinctes.
    yy, xx = np.indices((180, 320))
    tiles = ((xx // 16) + (yy // 16)) % 2
    checker = Image.fromarray(np.where(tiles[..., None], 220, 20).astype(np.uint8).repeat(3, axis=2))
    text = _terminal("error: failed to compile main.py")
    assert hamming_distance(perceptual_hash(checker), perceptual_hash(text)) >= 16


def test_ssim_identical_is_one():
    arr = np.full((64, 64), 80.0)
    assert ssim_score(arr, arr) == pytest.approx(1.0, abs=1e-9)


def test_ssim_drops_on_different_frames():
    a = np.zeros((64, 64))
    b = np.full((64, 64), 255.0)
    assert ssim_score(a, b) < 0.3


def test_webp_stays_under_forty_kilobytes():
    img = Image.new("RGB", (1920, 1080), (30, 40, 50))
    payload, mime = compress_webp(img)
    assert mime in {"image/webp", "image/jpeg"}
    assert 0 < len(payload) <= WEBP_MAX_BYTES


def test_webp_resizes_before_encode_and_uses_fast_method(monkeypatch):
    saves: list[tuple[str | None, dict, tuple[int, int]]] = []
    original_save = Image.Image.save

    def _spy(self, fp, format=None, **kwargs):
        saves.append((format, kwargs, self.size))
        return original_save(self, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", _spy)
    img = Image.new("RGB", (1920, 1080), (30, 40, 50))
    payload, mime = compress_webp(img)
    assert mime == "image/webp"
    assert payload.startswith(b"RIFF")
    assert len(saves) == 1
    fmt, kwargs, size = saves[0]
    assert fmt == "WEBP"
    assert kwargs.get("method") == WEBP_METHOD == 0
    assert kwargs.get("quality") == WEBP_QUALITY == 70
    assert max(size) <= WEBP_MAX_SIDE == 1024


def test_webp_single_pass_even_on_noisy_fullscreen(monkeypatch):
    saves: list[str | None] = []
    original_save = Image.Image.save

    def _spy(self, fp, format=None, **kwargs):
        saves.append(format)
        return original_save(self, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", _spy)
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    started = time.perf_counter()
    payload, mime = compress_webp(Image.fromarray(noise, "RGB"))
    elapsed_s = time.perf_counter() - started
    assert mime in {"image/webp", "image/jpeg"}
    assert len(payload) > 0
    assert saves == ["WEBP"]
    assert elapsed_s < 0.30


def test_webp_from_jpeg_bytes_does_not_keep_full_frame():
    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), (12, 24, 48)).save(buf, format="JPEG", quality=85)
    payload, mime = compress_webp(buf.getvalue())
    assert mime == "image/webp"
    assert 0 < len(payload) <= WEBP_MAX_BYTES


def test_snapshot_scale_caps_4k_to_1024px():
    assert snapshot_capture_scale((1920, 1080)) == pytest.approx(0.5)
    assert snapshot_capture_scale((3840, 2160)) == pytest.approx(1024 / 3840)
    assert snapshot_capture_scale((800, 600)) == pytest.approx(0.5)


def test_change_tick_asks_grim_for_1024px_on_4k_window():
    scales: list[float] = []

    def capture(geometry: str, **kwargs):
        scales.append(float(kwargs.get("scale", 1.0)))
        return _jpeg(_terminal())

    mind = ScreenConsciousness(
        capture_fn=capture,
        window_fn=lambda: _window("kitty", "zsh", "0x4k", size=(3840, 2160)),
        ocr_fn=lambda _b: "error: failed to compile main.py",
    )
    stats = mind.tick(force=True)
    assert stats.action in {"first", "change"}
    assert scales[0] == pytest.approx(PROBE_SCALE)
    assert scales[1] == pytest.approx(1024 / 3840)


def test_run_tick_uses_eight_second_stall(monkeypatch):
    captured: dict[str, object] = {}

    class _Fut:
        def __await__(self):
            async def _ok():
                return TickStats(action="skip")

            return _ok().__await__()

    class _Pool:
        def submit(self, pool, fn, *args, **kwargs):
            captured["pool"] = pool
            captured["task_name"] = kwargs.get("task_name")
            captured["stall_timeout"] = kwargs.get("stall_timeout")
            return _Fut()

    monkeypatch.setattr("core.thread_pool.get_thread_pool", lambda: _Pool())
    mind = ScreenConsciousness(
        capture_fn=lambda geometry, **k: _jpeg(_solid((0, 0, 0))),
        window_fn=lambda: _window(),
        ocr_fn=lambda _b: "",
    )
    stats = asyncio.run(mind._run_tick(force=False))
    assert stats.action == "skip"
    assert captured["pool"] == "compute-light"
    assert captured["task_name"] == "scrn"
    assert captured["stall_timeout"] == SCREEN_TICK_STALL_S == 8.0


def test_extract_signals_finds_errors_and_filenames():
    text = (
        "error[E0308]: mismatched types\n"
        "  --> src/main.rs:42:5\n"
        "FAILED tests/test_math.py::test_division\n"
    )
    signals = extract_signals(text)
    assert signals.has_error
    assert any("error" in k for k in signals.keywords)
    assert any(name.endswith(".rs") or name.endswith(".py") for name in signals.filenames)


def test_wants_screen_context_for_classic_question():
    assert wants_screen_context("Qu'est-ce qui cloche ici ?")
    assert wants_screen_context("Regarde l'écran, c'est quoi cette erreur")
    assert not wants_screen_context("Quelle heure est-il ?")
    assert not wants_screen_context("")


def test_wants_screen_context_deictic_on_error_snapshot():
    snap = ScreenSnapshot(
        window=_window(),
        phash=1,
        webp_bytes=b"x",
        ocr_text="error: failed",
        signals=extract_signals("error: failed to compile main.py"),
    )
    assert wants_screen_context("c'est quoi ça", snap)
    assert not wants_screen_context("bonjour", snap)


def test_diff_app_switch_always_triggers():
    prev = ScreenSnapshot(window=_window("firefox", "Web", "0xa"), phash=1, webp_bytes=b"x")
    nxt = _window("kitty", "cargo build", "0xb")
    result = diff_snapshots(prev, nxt, new_hash=1)
    assert result.changed
    assert result.reason == "app_switch"


def test_diff_static_screen_below_phash_soft():
    prev = ScreenSnapshot(window=_window(), phash=0, webp_bytes=b"x")
    # Hamming 0 → statique, zéro OCR, zéro réseau.
    result = diff_snapshots(prev, _window(), new_hash=0)
    assert result.changed is False
    assert result.reason == "static"


def _mind_with_frames(frames: list[bytes], windows: list[WindowInfo], ocr_calls: list[str]) -> ScreenConsciousness:
    state = {"i": 0}

    def capture(geometry: str, **kwargs):
        idx = min(state["i"], len(frames) - 1)
        return frames[idx]

    def window():
        idx = min(state["i"], len(windows) - 1)
        return windows[idx]

    def ocr(data: bytes) -> str:
        ocr_calls.append("ocr")
        return "error: failed to compile main.py"

    mind = ScreenConsciousness(
        interval_s=3.0,
        capture_fn=capture,
        window_fn=window,
        ocr_fn=ocr,
    )

    def tick_and_advance(*, force: bool = False):
        stats = ScreenConsciousness.tick(mind, force=force)
        state["i"] += 1
        return stats

    mind.advance = tick_and_advance  # type: ignore[attr-defined]
    return mind


def test_static_tick_does_not_ocr_or_grow_snapshot():
    frame = _jpeg(_terminal("ok", "green"))
    ocr_calls: list[str] = []
    mind = _mind_with_frames(
        [frame, frame, frame],
        [_window(), _window(), _window()],
        ocr_calls,
    )
    first = mind.advance(force=True)
    assert first.action in {"first", "change"}
    assert ocr_calls  # premier cliché d'un terminal : OCR une fois
    ocr_calls.clear()
    second = mind.advance(force=False)
    assert second.action == "static"
    assert ocr_calls == []
    assert mind.snapshot() is not None
    assert mind.cached_capture(max_age_s=8.0) is not None


def test_app_switch_triggers_a_new_capture():
    ocr_calls: list[str] = []
    mind = _mind_with_frames(
        [_jpeg(_solid((10, 20, 200))), _jpeg(_terminal())],
        [_window("firefox", "Web", "0xa"), _window("kitty", "cargo", "0xb")],
        ocr_calls,
    )
    mind.advance(force=True)
    ocr_calls.clear()
    stats = mind.advance(force=False)
    assert stats.action == "change"
    assert stats.reason == "app_switch"
    assert ocr_calls == ["ocr"]
    snap = mind.snapshot()
    assert snap is not None
    assert snap.window.window_class == "kitty"
    assert snap.signals.has_error
    assert len(snap.webp_bytes) <= WEBP_MAX_BYTES


def test_inject_is_blocked_while_speaking():
    async def scenario():
        session = SimpleNamespace(send_realtime_input=AsyncMock())
        host = SimpleNamespace(
            session=session,
            _is_speaking=True,
            _model_turn_active=False,
            _interrupted=False,
            _activity_open=False,
            ui=None,
        )
        mind = ScreenConsciousness(
            capture_fn=lambda geometry, **k: _jpeg(_terminal()),
            window_fn=lambda: _window(),
            ocr_fn=lambda _b: "error: boom main.py",
        )
        mind.tick(force=True)
        sent = await mind.inject_into_live(host, "Qu'est-ce qui cloche ici ?")
        return sent, session, mind

    sent, session, mind = asyncio.run(scenario())
    assert sent is False
    session.send_realtime_input.assert_not_called()
    assert mind._pending_query.startswith("Qu'est-ce qui cloche")


def test_inject_sends_webp_and_context_text():
    async def scenario():
        session = SimpleNamespace(send_realtime_input=AsyncMock())
        host = SimpleNamespace(
            session=session,
            _is_speaking=False,
            _model_turn_active=False,
            _interrupted=False,
            _activity_open=True,
            ui=None,
        )
        mind = ScreenConsciousness(
            capture_fn=lambda geometry, **k: _jpeg(_terminal()),
            window_fn=lambda: _window(),
            ocr_fn=lambda _b: "error: failed to compile main.py",
        )
        mind.tick(force=True)
        ok = await mind.inject_into_live(host, "Qu'est-ce qui cloche ici ?")
        again = await mind.inject_into_live(host, "Qu'est-ce qui cloche ici ?")
        return ok, again, session, mind

    ok, again, session, mind = asyncio.run(scenario())
    assert ok is True
    assert again is True
    # Chaque question explicite doit emporter son cliché frais, même si le
    # bureau semble identique : elle peut désigner un détail différent.
    assert session.send_realtime_input.await_count == 4
    video_call, text_call = session.send_realtime_input.await_args_list[:2]
    assert "video" in video_call.kwargs
    assert video_call.kwargs["video"]["mime_type"] in {"image/webp", "image/jpeg"}
    assert len(video_call.kwargs["video"]["data"]) <= WEBP_MAX_BYTES
    assert "CONTEXTE ÉCRAN" in text_call.kwargs["text"]
    assert mind._network_sends == 2


def test_daemon_cancels_cleanly_without_ticking_while_speaking():
    async def scenario():
        ticks: list[bool] = []

        async def fake_tick(*, force: bool = False):
            ticks.append(force)
            return SimpleNamespace(action="static", total_ms=1.0)

        mind = ScreenConsciousness(
            capture_fn=lambda geometry, **k: _jpeg(_solid((0, 0, 0))),
            window_fn=lambda: _window(),
            ocr_fn=lambda _b: "",
        )
        mind._run_tick = fake_tick  # type: ignore[method-assign]
        host = SimpleNamespace(
            session=None,
            _is_speaking=True,
            _model_turn_active=False,
            _interrupted=False,
            _activity_open=False,
            ui=None,
        )
        task = asyncio.create_task(mind.run(host))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ticks

    assert asyncio.run(scenario()) == []


def test_cpu_budget_static_path_stays_tiny():
    report = benchmark_cpu(rounds=12, include_live=False)
    assert report["webp_bytes"] <= WEBP_MAX_BYTES
    # Chemin statique : pHash (+ SSIM éventuellement), zéro OCR. Le test peut
    # cohabiter avec PortAudio/Qt en CI; avec l'intervalle sobre de 8 s, 80 ms
    # reste largement sous 2 % d'un cœur.
    assert report["tick_static_p50_ms"] < 80.0
    assert report["cpu_pct_static_one_core"] < 2.0
    assert report["phash_ms"] < 25.0
    assert report["ssim_ms"] < 10.0


def test_screen_change_event_is_frozen():
    event = ScreenChangeEvent(
        window_class="kitty",
        window_title="cargo",
        reason="app_switch",
        phash_distance=32,
        ssim=0.2,
        keywords=("error",),
        has_error=True,
    )
    assert event.event_name == "ScreenChangeEvent"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        event.reason = "visual"  # type: ignore[misc]
    assert DEFAULT_INTERVAL_S == 8.0


def test_quiet_browser_skips_grim_when_title_is_stable():
    captures: list[str] = []

    def capture(geometry: str, **kwargs):
        captures.append(geometry)
        return _jpeg(_solid((30, 40, 80)))

    mind = ScreenConsciousness(
        capture_fn=capture,
        window_fn=lambda: _window("firefox", "Mozilla Firefox", "0xf"),
        ocr_fn=lambda _b: "",
    )
    first = mind.tick(force=True)
    assert first.action == "first"
    assert captures
    captures.clear()
    skipped = mind.tick(force=False)
    assert skipped.action == "skip"
    assert skipped.reason == "quiet-app"
    assert captures == []


def test_cached_capture_expires():
    mind = ScreenConsciousness(
        capture_fn=lambda geometry, **k: _jpeg(_terminal()),
        window_fn=lambda: _window(),
        ocr_fn=lambda _b: "error: x",
    )
    mind.tick(force=True)
    snap = mind.snapshot()
    assert snap is not None
    snap.captured_at = time.monotonic() - 40.0
    assert mind.cached_capture(max_age_s=8.0) is None
    assert mind.cached_capture(max_age_s=60.0) is not None
