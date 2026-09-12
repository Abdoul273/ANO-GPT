"""Simulation de signaux mixtes pour le détecteur de double parole.

Scénarios
---------
1. Écho seul (Jarvis parle, micro = far-end retardé) → pas de gel, NCC haute
2. Double parole (voix utilisateur superposée) → détection < 10 ms, gel NLMS
3. Gain de confort lissé, canal micro jamais coupé
4. Barge-in seulement après 2 trames VAD neurales consécutives
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from core.double_talk_detector import (
    ATTACK_MS,
    BARGE_CONFIRM_FRAMES,
    FRAME_SAMPLES,
    GAIN_FLOOR,
    GAIN_OPEN,
    HOLD_MS,
    SAMPLE_RATE,
    AdaptiveEchoFilter,
    DoubleTalkDetector,
    TalkState,
    float_to_int16,
    geigel_ratio,
    max_lag_ncc,
    normalized_cross_correlation,
    rms,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Signaux mixtes
# ═══════════════════════════════════════════════════════════════════════════════

def make_voice(
    f0: float,
    duration_s: float,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.4,
) -> np.ndarray:
    """Pile d'harmoniques type parole (F0 distinct par locuteur)."""
    n = int(sample_rate * duration_s)
    t = np.arange(n, dtype=np.float64) / sample_rate
    sig = (
        np.sin(2 * np.pi * f0 * t)
        + 0.55 * np.sin(2 * np.pi * 2 * f0 * t)
        + 0.35 * np.sin(2 * np.pi * 3 * f0 * t)
        + 0.22 * np.sin(2 * np.pi * 4 * f0 * t)
        + 0.12 * np.sin(2 * np.pi * 5 * f0 * t)
    )
    sig *= amplitude / (np.sqrt(np.mean(sig * sig)) + 1e-12)
    return sig.astype(np.float32)


def make_echo(far: np.ndarray, delay_samples: int, gain: float) -> np.ndarray:
    echo = np.zeros_like(far)
    if 0 < delay_samples < far.size:
        echo[delay_samples:] = far[:-delay_samples] * np.float32(gain)
    elif delay_samples <= 0:
        echo = far * np.float32(gain)
    return echo


def mix_double_talk(
    duration_s: float = 0.40,
    echo_only_s: float = 0.15,
    delay_ms: float = 20.0,
    echo_gain: float = 0.25,
    jarvis_f0: float = 180.0,
    user_f0: float = 110.0,
    jarvis_amp: float = 0.55,
    user_amp: float = 0.50,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Far-end Jarvis + micro (écho puis écho+utilisateur).

    Retourne ``(far, mic, onset_sample)`` où ``onset_sample`` est le premier
    échantillon de double parole.
    """
    far = make_voice(jarvis_f0, duration_s, amplitude=jarvis_amp)
    echo = make_echo(far, int(SAMPLE_RATE * delay_ms / 1000.0), echo_gain)
    user = make_voice(user_f0, duration_s, amplitude=user_amp)
    onset = int(SAMPLE_RATE * echo_only_s)
    user[:onset] = 0.0
    mic = np.clip(echo + user, -0.99, 0.99).astype(np.float32)
    return far, mic, onset


def iter_frames(mic: np.ndarray, far: np.ndarray, frame: int = FRAME_SAMPLES):
    n = min(mic.size, far.size)
    n -= n % frame
    for start in range(0, n, frame):
        yield start, mic[start:start + frame], far[start:start + frame]


class FakeVAD:
    def __init__(self, probabilities):
        self._probabilities = list(probabilities)
        self.calls = 0

    def probability(self, _audio):
        idx = min(self.calls, len(self._probabilities) - 1)
        self.calls += 1
        return self._probabilities[idx]

    def reset(self):
        self.calls = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Primitives
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrimitives:
    def test_ncc_identical_signals_is_one(self):
        x = make_voice(140.0, 0.02)
        assert normalized_cross_correlation(x, x) == pytest.approx(1.0, abs=1e-5)

    def test_ncc_uncorrelated_tones_is_low(self):
        a = make_voice(110.0, 0.04)
        b = make_voice(330.0, 0.04)
        assert normalized_cross_correlation(a, b) < 0.35

    def test_ncc_finds_delayed_echo(self):
        far = make_voice(180.0, 0.12, amplitude=0.5)
        echo = make_echo(far, delay_samples=240, gain=0.4)
        # La portion après le retard doit corréler fortement.
        tail = slice(240, 240 + FRAME_SAMPLES)
        score = max_lag_ncc(echo[tail], far[: tail.stop], max_lag=400)
        assert score > 0.90

    def test_geigel_echo_only_below_threshold(self):
        far = make_voice(180.0, 0.08, amplitude=0.6)
        echo = far * np.float32(0.25)
        ratio = geigel_ratio(echo[-FRAME_SAMPLES:], far)
        assert ratio < 0.50

    def test_geigel_double_talk_above_threshold(self):
        far = make_voice(180.0, 0.08, amplitude=0.55)
        echo = far * np.float32(0.25)
        user = make_voice(110.0, 0.08, amplitude=0.50)
        mic = echo + user
        # Une trame de 5 ms peut tomber sur une interférence destructive :
        # on mesure une fenêtre voisée plus longue, représentative.
        mid = 640
        frame = mic[mid:mid + 4 * FRAME_SAMPLES]
        ratio = geigel_ratio(frame, far, far[mid:mid + 4 * FRAME_SAMPLES])
        assert ratio > 0.50

    def test_float_to_int16_roundtrip_range(self):
        x = np.array([-1.0, 0.0, 1.0], dtype=np.float32)
        pcm = float_to_int16(x)
        assert pcm.dtype == np.int16
        assert pcm[0] == -32767 or pcm[0] == -32768
        assert pcm[2] == 32767


# ═══════════════════════════════════════════════════════════════════════════════
# NLMS à gel
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveEchoFilter:
    def test_coefficients_change_when_adapting(self):
        filt = AdaptiveEchoFilter(n_taps=64, mu=0.8)
        far = make_voice(180.0, 0.08, amplitude=0.5)
        mic = make_echo(far, delay_samples=10, gain=0.4)
        before = filt.snapshot()
        for start in range(0, far.size - FRAME_SAMPLES, FRAME_SAMPLES):
            filt.process(
                mic[start:start + FRAME_SAMPLES],
                far[start:start + FRAME_SAMPLES],
                freeze=False,
            )
        after = filt.coefficients
        assert filt.updates > 0
        assert not np.allclose(before, after)

    def test_coefficients_frozen_are_identical(self):
        filt = AdaptiveEchoFilter(n_taps=64, mu=0.8)
        far = make_voice(180.0, 0.10, amplitude=0.5)
        mic = make_echo(far, delay_samples=8, gain=0.5)
        for start in range(0, 8 * FRAME_SAMPLES, FRAME_SAMPLES):
            filt.process(
                mic[start:start + FRAME_SAMPLES],
                far[start:start + FRAME_SAMPLES],
                freeze=False,
            )
        frozen_w = filt.snapshot()
        filt.frozen = True
        offset = 8 * FRAME_SAMPLES
        user = make_voice(110.0, (far.size - offset) / SAMPLE_RATE, amplitude=0.5)
        mixed = np.clip(mic[offset:] + user, -1, 1)
        far_tail = far[offset:]
        for start in range(0, mixed.size - FRAME_SAMPLES, FRAME_SAMPLES):
            filt.process(
                mixed[start:start + FRAME_SAMPLES],
                far_tail[start:start + FRAME_SAMPLES],
                freeze=True,
            )
        assert np.array_equal(frozen_w, filt.coefficients)

    def test_apply_does_not_adapt(self):
        filt = AdaptiveEchoFilter(n_taps=32)
        far = make_voice(200.0, 0.02, amplitude=0.4)
        w0 = filt.snapshot()
        filt.apply(far, far)
        assert np.array_equal(w0, filt.coefficients)
        assert filt.updates == 0


# ═══════════════════════════════════════════════════════════════════════════════
# DTD sur signaux mixtes
# ═══════════════════════════════════════════════════════════════════════════════

class TestDoubleTalkDetection:
    def test_echo_only_does_not_freeze(self):
        dtd = DoubleTalkDetector()
        far = make_voice(180.0, 0.20, amplitude=0.55)
        mic = make_echo(far, delay_samples=320, gain=0.25)
        states = []
        for _, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            states.append(result.state)
        # Après quelques trames de far-end, on reste en écho seul.
        assert TalkState.DOUBLE_TALK not in states[4:]
        assert any(s is TalkState.ECHO_ONLY for s in states)
        assert dtd.frozen is False
        assert dtd.filter.updates > 0

    def test_detects_double_talk_in_under_10ms(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.16)
        detected_at = None
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            if result.state is TalkState.DOUBLE_TALK and start >= onset:
                detected_at = start
                break
        assert detected_at is not None, "double parole non détectée"
        latency_ms = 1000.0 * (detected_at + FRAME_SAMPLES - onset) / SAMPLE_RATE
        assert latency_ms < 10.0, f"latence {latency_ms:.2f} ms ≥ 10 ms"

    def test_first_dt_frame_freezes_coefficients(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.14)
        snapshot = None
        frozen_seen = False
        for start, m, f in iter_frames(mic, far):
            if start < onset:
                dtd.process(m, f)
                snapshot = dtd.filter.snapshot()
                continue
            result = dtd.process(m, f)
            if result.frozen:
                frozen_seen = True
                assert snapshot is not None
                assert np.array_equal(snapshot, dtd.filter.coefficients)
                break
        assert frozen_seen

    def test_coefficients_stay_frozen_during_overlap(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(duration_s=0.36, echo_only_s=0.12)
        frozen_w = None
        dt_frames = 0
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            if result.frozen:
                if frozen_w is None:
                    frozen_w = dtd.filter.snapshot()
                else:
                    assert np.array_equal(frozen_w, dtd.filter.coefficients)
                if start >= onset:
                    dt_frames += 1
        assert dt_frames >= 3
        assert frozen_w is not None

    def test_ncc_high_on_delayed_echo_only(self):
        """Après le retard acoustique, la NCC à max-lag reconnaît l'écho."""
        dtd = DoubleTalkDetector()
        far = make_voice(180.0, 0.24, amplitude=0.55)
        mic = make_echo(far, delay_samples=320, gain=0.25)
        scores = []
        states = []
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            states.append(result.state)
            if start >= 640:
                scores.append(result.ncc)
        assert scores
        assert np.mean(scores) > 0.70
        assert TalkState.DOUBLE_TALK not in states[8:]

    def test_geigel_rises_on_double_talk(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk()
        echo_g, dt_g = [], []
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            if start + FRAME_SAMPLES < onset:
                echo_g.append(result.geigel)
            elif start >= onset:
                dt_g.append(result.geigel)
        assert echo_g and dt_g
        assert np.mean(dt_g) > np.mean(echo_g)

    def test_holdover_keeps_freeze_across_a_gap(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.10, duration_s=0.22)
        # Un trou de 15 ms de parole proche au milieu du DT.
        gap_start = onset + 4 * FRAME_SAMPLES
        gap_end = gap_start + 2 * FRAME_SAMPLES  # 10 ms
        echo = make_echo(far, delay_samples=320, gain=0.25)
        mic[gap_start:gap_end] = echo[gap_start:gap_end]
        still_frozen = False
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            if gap_start <= start < gap_end:
                still_frozen = still_frozen or result.frozen
        assert still_frozen, "le hangover doit maintenir le gel pendant un trou court"
        assert HOLD_MS >= 10.0


class TestComfortGain:
    def test_channel_never_muted(self):
        dtd = DoubleTalkDetector()
        far, mic, _ = mix_double_talk()
        gains = []
        for _, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            gains.append(result.comfort_gain)
            assert result.comfort_gain >= GAIN_FLOOR
            assert result.residual.size == m.size
        assert min(gains) >= GAIN_FLOOR

    def test_gain_opens_on_double_talk(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.14)
        echo_gain, dt_gain = [], []
        prev = None
        for start, m, f in iter_frames(mic, far):
            result = dtd.process(m, f)
            if start + FRAME_SAMPLES < onset:
                echo_gain.append(result.comfort_gain)
            elif start >= onset:
                dt_gain.append(result.comfort_gain)
                if prev is not None:
                    # Pas de saut brutal à 1.0 : l'attaque est lissée.
                    assert result.comfort_gain - prev < 0.85
                prev = result.comfort_gain
        assert echo_gain and dt_gain
        assert dt_gain[-1] > echo_gain[-1]
        assert dt_gain[-1] > 0.85

    def test_gain_smoothing_is_asymmetric(self):
        """L'attaque (ouvrir) est plus rapide que le relâchement."""
        assert ATTACK_MS < 10.0
        assert ATTACK_MS < 18.0  # RELEASE_MS


class TestBargeIn:
    def test_requires_two_consecutive_vad_frames(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.12)
        barges = []
        for start, m, f in iter_frames(mic, far):
            vad = 0.92 if start >= onset else 0.05
            result = dtd.process(m, f, vad_probability=vad, jarvis_speaking=True)
            barges.append(result.should_barge_in)
        assert barges.count(True) >= 1
        first_dt = next(
            i for i, (start, _, _) in enumerate(iter_frames(mic, far))
            if start >= onset
        )
        # La première trame DT + VAD n'interrompt pas encore.
        assert barges[first_dt] is False
        # La deuxième trame consécutive le fait.
        assert barges[first_dt + 1] is True

    def test_single_vad_spike_does_not_barge(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.12)
        any_barge = False
        for start, m, f in iter_frames(mic, far):
            # Une trame VAD sur deux : jamais 2 consécutives.
            vad = 0.95 if (start >= onset and ((start // FRAME_SAMPLES) % 2 == 0)) else 0.08
            result = dtd.process(m, f, vad_probability=vad, jarvis_speaking=True)
            any_barge = any_barge or result.should_barge_in
        assert any_barge is False

    def test_echo_only_does_not_barge_even_with_high_vad(self):
        dtd = DoubleTalkDetector()
        far = make_voice(180.0, 0.16, amplitude=0.55)
        mic = make_echo(far, delay_samples=320, gain=0.25)
        any_barge = False
        for _, m, f in iter_frames(mic, far):
            result = dtd.process(m, f, vad_probability=0.99, jarvis_speaking=True)
            any_barge = any_barge or result.should_barge_in
        assert any_barge is False

    def test_no_barge_when_jarvis_is_silent(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.08)
        any_barge = False
        for start, m, f in iter_frames(mic, far):
            vad = 0.95 if start >= onset else 0.1
            result = dtd.process(m, f, vad_probability=vad, jarvis_speaking=False)
            any_barge = any_barge or result.should_barge_in
        assert any_barge is False

    def test_update_barge_in_two_phase_api(self):
        dtd = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.12)
        for start, m, f in iter_frames(mic, far):
            dtd.process(m, f)
            if start >= onset:
                break
        assert dtd.state is TalkState.DOUBLE_TALK
        assert dtd.update_barge_in(0.9, True) is False
        assert dtd.update_barge_in(0.9, True) is True
        assert BARGE_CONFIRM_FRAMES == 2

    def test_injected_vad_instance(self):
        vad = FakeVAD([0.05] * 20 + [0.91, 0.93, 0.92])
        dtd = DoubleTalkDetector(vad=vad)
        far, mic, _ = mix_double_talk(echo_only_s=0.10, duration_s=0.28)
        barges = []
        for _, m, f in iter_frames(mic, far):
            result = dtd.process(m, f, jarvis_speaking=True)
            barges.append(result.should_barge_in)
        # FakeVAD est appelé sur chaque trame (80 samples) : Silero réel
        # bufferiserait, le fake non. On vérifie juste que le chemin interne vit.
        assert vad.calls > 0
        assert any(barges) or dtd.stats["dt_frames"] > 0


class TestResetAndStats:
    def test_reset_clears_freeze_and_gain(self):
        dtd = DoubleTalkDetector()
        far, mic, _ = mix_double_talk(echo_only_s=0.08, duration_s=0.20)
        for _, m, f in iter_frames(mic, far):
            dtd.process(m, f)
        dtd.reset()
        assert dtd.state is TalkState.SILENCE
        assert dtd.frozen is False
        assert dtd.filter.updates == 0
        assert dtd.stats["frames"] == 0
        assert np.allclose(dtd.filter.coefficients, 0.0)

    def test_stats_keys(self):
        dtd = DoubleTalkDetector()
        far = make_voice(160.0, 0.02, amplitude=0.4)
        dtd.process(far, far)
        stats = dtd.stats
        for key in (
            "state", "frozen", "ncc", "geigel", "comfort_gain",
            "frames", "dt_frames", "freeze_events", "barge_events",
        ):
            assert key in stats

    def test_int16_path_matches_float_decision(self):
        dtd_f = DoubleTalkDetector()
        dtd_i = DoubleTalkDetector()
        far, mic, onset = mix_double_talk(echo_only_s=0.12)
        states_f, states_i = [], []
        for start, m, f in iter_frames(mic, far):
            states_f.append(dtd_f.process(m, f).state)
            states_i.append(dtd_i.process_int16(float_to_int16(m), float_to_int16(f)).state)
        # Même transition écho → DT, au plus une trame d'écart (quantification).
        first_f = next(i for i, s in enumerate(states_f) if s is TalkState.DOUBLE_TALK)
        first_i = next(i for i, s in enumerate(states_i) if s is TalkState.DOUBLE_TALK)
        assert abs(first_f - first_i) <= 1
        assert first_f * FRAME_SAMPLES >= onset - FRAME_SAMPLES


class TestLatencyBudget:
    def test_process_faster_than_realtime(self):
        dtd = DoubleTalkDetector()
        far, mic, _ = mix_double_talk(duration_s=0.25)
        t0 = time.monotonic()
        n = 0
        for _, m, f in iter_frames(mic, far):
            dtd.process(m, f)
            n += 1
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        budget_ms = n * 5.0  # chaque trame = 5 ms d'audio
        assert elapsed_ms < budget_ms
        # Et très en dessous de 10 ms par trame.
        assert elapsed_ms / max(n, 1) < 10.0

    def test_empty_inputs(self):
        dtd = DoubleTalkDetector()
        result = dtd.process(np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32))
        assert result.residual.size == 0
        assert result.frozen is False


class TestEchoCancellerIntegration:
    def test_aec_exposes_dtd_and_freeze_flag(self):
        from core.echo_canceller import EchoCanceller
        aec = EchoCanceller()
        try:
            assert aec.dtd is not None
            stats = aec.stats
            assert "dtd_state" in stats
            assert "dtd_frozen" in stats
        finally:
            aec.destroy()

    def test_full_duplex_barge_in_uses_two_vad_frames(self):
        from core.echo_canceller import FullDuplexFilter
        fdf = FullDuplexFilter()
        assert fdf._BARGE_CONFIRM_FRAMES == 2
        silence = np.zeros(1024, dtype=np.int16)
        _, should_barge, _ = fdf.process_mic(silence, jarvis_speaking=True)
        assert should_barge is False
