"""Tests unitaires pour l'analyseur prosodique et l'adaptation Jarvis (core/prosody_analyzer.py)."""

from unittest.mock import AsyncMock, MagicMock
import pytest
import numpy as np

from core.prosody_analyzer import (
    ProsodyAnalyzer,
    TemporalMoodFilter,
    AcousticFeatures,
    MoodClassification,
    QUALIFIED_MOODS,
    compute_yin_pitch,
    get_prosody_analyzer,
)
from core.session_manager import SessionManager


# ─────────────────────────────────────────────────────────────────────────────
# Générateurs de signaux synthétiques
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_tone(
    freq_hz: float = 180.0,
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    amplitude: float = 0.3,
) -> np.ndarray:
    """Génère une onde pure avec harmoniques pour tester le pitch F0 Yin."""
    t = np.arange(int(sample_rate * duration_s), dtype=np.float32) / sample_rate
    wave = (
        np.sin(2 * np.pi * freq_hz * t)
        + 0.5 * np.sin(4 * np.pi * freq_hz * t)
        + 0.25 * np.sin(6 * np.pi * freq_hz * t)
    )
    return (amplitude * wave).astype(np.float32)


def generate_synthetic_speech(
    state: str,
    duration_s: float = 2.5,
    sample_rate: int = 16000,
) -> np.ndarray:
    """Génère un signal synthétique modulé représentatif d'un état qualifié."""
    fs = sample_rate
    t = np.arange(int(fs * duration_s), dtype=np.float32) / fs

    if state == "calme":
        # Voix calme : débit posé (~2.6 syl/s), F0 stable ~140Hz, énergie modérée (-23 dBFS)
        pitch = 140.0
        wave = np.sin(2 * np.pi * pitch * t) + 0.4 * np.sin(4 * np.pi * pitch * t)
        env = 0.08
        for c in np.arange(0.2, duration_s - 0.2, 1.0 / 2.6):
            env += np.exp(-((t - c) / 0.08) ** 2)
        return (0.12 * np.clip(env, 0, 1) * wave).astype(np.float32)

    elif state == "agacé/pressé":
        # Voix agacée/pressée : débit très rapide (~5.5 syl/s), F0 élevée et variable, forte énergie (-14 dBFS)
        pitch = 190.0 + 35.0 * np.sin(2 * np.pi * 1.5 * t)
        phase = 2 * np.pi * np.cumsum(pitch) / fs
        wave = np.sin(phase) + 0.6 * np.sin(2 * phase)
        env = 0.05
        for c in np.arange(0.1, duration_s - 0.1, 1.0 / 5.5):
            env += np.exp(-((t - c) / 0.045) ** 2)
        return (0.45 * np.clip(env, 0, 1) * wave).astype(np.float32)

    elif state == "chuchoté/nuit":
        # Chuchotement : bruit blanc filtré passe-haut, aucun voisement périodique, ZCR élevé, volume très faible (-38 dBFS)
        np.random.seed(42)
        noise = np.random.randn(len(t)).astype(np.float32)
        noise_filt = np.diff(noise, prepend=noise[0])
        env = 0.04
        for c in np.arange(0.2, duration_s - 0.2, 1.0 / 2.5):
            env += np.exp(-((t - c) / 0.08) ** 2)
        norm_noise = noise_filt / (np.std(noise_filt) + 1e-6)
        return (0.02 * np.clip(env, 0, 1) * norm_noise).astype(np.float32)

    elif state == "fatigué":
        # Voix fatiguée : débit ralenti (~1.3 syl/s), F0 grave et descendante, faible volume (-36 dBFS)
        pitch = 105.0 - 5.0 * (t / duration_s)
        phase = 2 * np.pi * np.cumsum(pitch) / fs
        wave = np.sin(phase) + 0.3 * np.sin(2 * phase)
        env = 0.02
        for c in np.arange(0.4, duration_s - 0.4, 1.0 / 1.3):
            env += np.exp(-((t - c) / 0.12) ** 2)
        return (0.035 * np.clip(env, 0, 1) * wave).astype(np.float32)

    else:  # "neutre"
        # Voix standard : débit moyen (~3.4 syl/s), F0 standard ~150Hz, volume moyen (-21 dBFS)
        pitch = 150.0 + 8.0 * np.sin(2 * np.pi * 0.4 * t)
        phase = 2 * np.pi * np.cumsum(pitch) / fs
        wave = np.sin(phase) + 0.4 * np.sin(2 * phase)
        env = 0.08
        for c in np.arange(0.25, duration_s - 0.2, 1.0 / 3.4):
            env += np.exp(-((t - c) / 0.07) ** 2)
        return (0.20 * np.clip(env, 0, 1) * wave).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Tests unitaires de l'analyse acoustique (Yin, RMS, Syllables, Whisper)
# ─────────────────────────────────────────────────────────────────────────────

def test_yin_pitch_extraction_accuracy():
    """Vérifie la précision de l'algorithme Yin sur des tonalités connues."""
    sample_rate = 16000
    for target_f0 in [120.0, 180.0, 240.0, 320.0]:
        sig = generate_synthetic_tone(freq_hz=target_f0, duration_s=0.5, sample_rate=sample_rate)
        frame_len = 512
        tau_max = int(sample_rate / 60.0)
        search_region = sig[: frame_len + tau_max + 1]
        frame = search_region[:frame_len]
        estimated_f0 = compute_yin_pitch(frame, search_region, sample_rate=sample_rate)
        assert abs(estimated_f0 - target_f0) / target_f0 < 0.02, (
            f"Erreur Yin trop importante : {estimated_f0} vs {target_f0}"
        )


def test_rms_and_dynamic_range_features():
    """Vérifie l'extraction de l'énergie RMS en dBFS et de la dynamique."""
    analyzer = ProsodyAnalyzer(sample_rate=16000)
    quiet_sig = generate_synthetic_tone(amplitude=0.01)
    loud_sig = generate_synthetic_tone(amplitude=0.50)

    quiet_feat = analyzer.extract_features(quiet_sig)
    loud_feat = analyzer.extract_features(loud_sig)

    assert quiet_feat.rms_dbfs < loud_feat.rms_dbfs
    assert quiet_feat.rms_dbfs < -30.0
    assert loud_feat.rms_dbfs > -15.0


def test_syllables_per_second_estimation():
    """Vérifie l'estimation du débit de parole (syllabes/s) via les pics d'énergie."""
    analyzer = ProsodyAnalyzer(sample_rate=16000)
    slow_speech = generate_synthetic_speech("fatigué", duration_s=3.0)
    fast_speech = generate_synthetic_speech("agacé/pressé", duration_s=2.0)

    slow_feat = analyzer.extract_features(slow_speech)
    fast_feat = analyzer.extract_features(fast_speech)

    assert slow_feat.syllables_per_second < 2.5
    assert fast_feat.syllables_per_second >= 4.5
    assert fast_feat.syllables_per_second > slow_feat.syllables_per_second


def test_whisper_and_voicing_ratio():
    """Vérifie la distinction nette entre parole voisée et chuchotement."""
    analyzer = ProsodyAnalyzer(sample_rate=16000)
    voiced_speech = generate_synthetic_speech("calme")
    whisper_speech = generate_synthetic_speech("chuchoté/nuit")

    voiced_feat = analyzer.extract_features(voiced_speech)
    whisper_feat = analyzer.extract_features(whisper_speech)

    # Voicing ratio élevé pour le voisé, quasi nul pour le chuchoté
    assert voiced_feat.voicing_ratio >= 0.70
    assert whisper_feat.voicing_ratio <= 0.20

    # ZCR élevé pour le chuchoté
    assert whisper_feat.mean_zcr > 0.15
    assert voiced_feat.mean_zcr < 0.10

    # Whisper score élevé pour le chuchoté
    assert whisper_feat.whisper_score >= 0.45
    assert voiced_feat.whisper_score < 0.20


# ─────────────────────────────────────────────────────────────────────────────
# Tests unitaires de la classification d'humeur
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expected_state", QUALIFIED_MOODS)
def test_classification_instantanee_etats(expected_state):
    """Vérifie que chacun des 5 états qualifiés est correctement classifié."""
    analyzer = ProsodyAnalyzer(sample_rate=16000)
    is_night = (expected_state == "chuchoté/nuit")
    sig = generate_synthetic_speech(expected_state, duration_s=2.5)

    feat = analyzer.extract_features(sig)
    result = analyzer.classify_instant(feat, is_night=is_night)

    assert result.state == expected_state
    assert result.confidence >= 0.40
    assert result.probabilities[expected_state] > 0.35


def test_signal_trop_court_ou_silence():
    """Vérifie le repli sécurisé sur signal quasi vide ou silence."""
    analyzer = ProsodyAnalyzer(sample_rate=16000)
    short_audio = np.zeros(100, dtype=np.float32)  # < 150ms
    feat = analyzer.extract_features(short_audio)
    res = analyzer.classify_instant(feat)
    assert res.state == "neutre"
    assert feat.rms_dbfs <= -100.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests unitaires du filtrage temporel glissant (Hystérésis)
# ─────────────────────────────────────────────────────────────────────────────

def test_temporal_filter_prevents_isolated_word_flip():
    """Un mot isolé agacé ne doit pas faire basculer l'assistant s'il était calme."""
    filt = TemporalMoodFilter(history_size=5, alpha_ema=0.45, hysteresis_margin=0.10, min_consecutive_switches=2)
    filt.reset("calme")
    dummy_feat = AcousticFeatures(1.0, -20.0, 10.0, 3.0, 140.0, 140.0, 130.0, 150.0, 0.05, 0.05, 0.8, 0.1, 0.2)

    # 1. État stable calme
    calm_inst = MoodClassification(
        state="calme",
        confidence=0.75,
        probabilities={"calme": 0.75, "agacé/pressé": 0.05, "chuchoté/nuit": 0.05, "fatigué": 0.05, "neutre": 0.10},
        features=dummy_feat,
    )
    for _ in range(3):
        filt.update(calm_inst)
    assert filt.current_state == "calme"

    # 2. Un seul mot isolé agacé ("Non !")
    rush_inst = MoodClassification(
        state="agacé/pressé",
        confidence=0.70,
        probabilities={"calme": 0.10, "agacé/pressé": 0.70, "chuchoté/nuit": 0.05, "fatigué": 0.05, "neutre": 0.10},
        features=dummy_feat,
    )
    res_isolated = filt.update(rush_inst)
    assert res_isolated.state == "calme", "L'état ne doit pas basculer sur un seul mot isolé"


def test_temporal_filter_transitions_on_sustained_evidence():
    """L'état bascule bien dès que la tendance est confirmée consécutivement."""
    filt = TemporalMoodFilter(history_size=5, alpha_ema=0.45, hysteresis_margin=0.10, min_consecutive_switches=2)
    filt.reset("calme")
    dummy_feat = AcousticFeatures(1.0, -20.0, 10.0, 3.0, 140.0, 140.0, 130.0, 150.0, 0.05, 0.05, 0.8, 0.1, 0.2)

    calm_inst = MoodClassification(
        state="calme",
        confidence=0.75,
        probabilities={"calme": 0.75, "agacé/pressé": 0.05, "chuchoté/nuit": 0.05, "fatigué": 0.05, "neutre": 0.10},
        features=dummy_feat,
    )
    for _ in range(3):
        filt.update(calm_inst)

    rush_inst = MoodClassification(
        state="agacé/pressé",
        confidence=0.75,
        probabilities={"calme": 0.05, "agacé/pressé": 0.75, "chuchoté/nuit": 0.05, "fatigué": 0.05, "neutre": 0.10},
        features=dummy_feat,
    )
    filt.update(rush_inst)  # Tour 1
    filt.update(rush_inst)  # Tour 2
    res3 = filt.update(rush_inst)  # Tour 3
    assert res3.state == "agacé/pressé", "L'état doit basculer après confirmation consécutive"


# ─────────────────────────────────────────────────────────────────────────────
# Tests d'adaptation Jarvis (Directives Gemini Live & TTS)
# ─────────────────────────────────────────────────────────────────────────────

def test_gemini_system_instruction_injection():
    """Vérifie les instructions contextuelles formulées pour chaque état."""
    analyzer = get_prosody_analyzer()

    # Format spécifique demandé dans l'énoncé
    night_prompt = analyzer.get_gemini_instruction("chuchoté/nuit")
    assert "[Contexte : Utilisateur chuchote la nuit. Réponds doucement, de manière très courte et apaisante.]" in night_prompt

    rush_prompt = analyzer.get_gemini_instruction("agacé/pressé")
    assert "Utilisateur agacé ou pressé" in rush_prompt
    assert "directe et efficace" in rush_prompt

    calm_prompt = analyzer.get_gemini_instruction("calme")
    assert "calme et posé" in calm_prompt

    fatigue_prompt = analyzer.get_gemini_instruction("fatigué")
    assert "fatigué" in fatigue_prompt


def test_tts_modulation_parameters():
    """Vérifie la modulation de vitesse et de volume TTS pour chaque état."""
    analyzer = get_prosody_analyzer()

    # 1. Chuchoté / nuit : plus lent, volume réduit
    mod_whisp = analyzer.get_tts_modulation("chuchoté/nuit")
    assert mod_whisp.speed_factor < 1.0
    assert mod_whisp.volume_factor < 0.8
    assert mod_whisp.tts_rate == "-15%"
    assert mod_whisp.tts_volume == "-30%"

    # 2. Agacé / pressé : plus rapide (+20%), volume tonique (+5%)
    mod_rush = analyzer.get_tts_modulation("agacé/pressé")
    assert mod_rush.speed_factor > 1.15
    assert mod_rush.volume_factor > 1.0
    assert mod_rush.tts_rate == "+20%"
    assert mod_rush.tts_volume == "+5%"

    # 3. Fatigué : doux et ralenti
    mod_tired = analyzer.get_tts_modulation("fatigué")
    assert mod_tired.speed_factor < 1.0
    assert mod_tired.volume_factor < 0.9

    # 4. Neutre : nominal
    mod_neut = analyzer.get_tts_modulation("neutre")
    assert mod_neut.speed_factor == 1.0
    assert mod_neut.volume_factor == 1.0


def test_apply_tts_modulation_config():
    """Vérifie la mise à jour d'un dictionnaire de configuration TTS."""
    analyzer = get_prosody_analyzer()
    base_cfg = {"tts_engine": "kokoro", "tts_speed": 1.0, "tts_voice": "af_heart"}

    updated_rush = analyzer.apply_tts_modulation(base_cfg, "agacé/pressé")
    assert updated_rush["tts_speed"] == 1.20
    assert updated_rush["tts_rate"] == "+20%"
    assert updated_rush["tts_volume"] == "+5%"

    updated_whisp = analyzer.apply_tts_modulation(base_cfg, "chuchoté/nuit")
    assert updated_whisp["tts_speed"] == 0.85
    assert updated_whisp["tts_volume"] == "-30%"


# ─────────────────────────────────────────────────────────────────────────────
# Tests d'intégration dans SessionManager (core/session_manager.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_session_manager_prosody_integration():
    """Vérifie que SessionManager intègre les méthodes de ProsodyAnalyzer."""
    sm = SessionManager()

    # Propriétés de base
    assert sm.prosody_analyzer is not None
    assert sm.get_current_mood() in QUALIFIED_MOODS

    # Analyse audio
    calm_audio = generate_synthetic_speech("calme")
    classification = sm.analyze_user_audio(calm_audio, sample_rate=16000)
    assert isinstance(classification, MoodClassification)

    # Récupération de directive Gemini
    instruction = sm.get_prosody_context_instruction("chuchoté/nuit")
    assert "Utilisateur chuchote la nuit" in instruction

    # Modulation TTS
    tts_cfg = {"tts_speed": 1.0}
    modulated = sm.apply_prosody_to_tts_config(tts_cfg, "agacé/pressé")
    assert modulated["tts_speed"] == 1.20
    assert modulated["tts_rate"] == "+20%"


def test_jarvis_live_exposes_session_manager_prosody_descriptor():
    """L'hôte composé doit reprendre tous les descripteurs de ses moteurs."""
    from main import JarvisLive
    from core.audio_engine import AudioEngine
    from core.phone_relay import PhoneRelay
    from core.proactive_engine import ProactiveEngine
    from core.tool_dispatcher import ToolDispatcher

    jarvis = JarvisLive.__new__(JarvisLive)

    assert all(
        issubclass(JarvisLive, engine)
        for engine in (
            AudioEngine,
            SessionManager,
            ToolDispatcher,
            ProactiveEngine,
            PhoneRelay,
        )
    )
    assert JarvisLive.prosody_analyzer is SessionManager.prosody_analyzer
    assert JarvisLive.continuous_vision is SessionManager.continuous_vision
    assert JarvisLive.thought_streamer is SessionManager.thought_streamer
    assert JarvisLive.camera is PhoneRelay.camera
    assert jarvis.prosody_analyzer is not None
    assert jarvis.get_current_mood() in QUALIFIED_MOODS


def test_session_manager_build_config_injects_prosody(monkeypatch):
    """Vérifie que _build_config() intègre la directive prosodique dans system_instruction."""
    sm = SessionManager()
    # Mock des dépendances nécessaires pour _build_config
    sm._asst_name = "JARVIS"
    sm._recalled_ids = set()
    sm._conn = MagicMock()
    sm._conn.resume_handle.return_value = None
    sm._context_compression_enabled = False
    sm._live_voice = "Puck"
    sm._live_models = MagicMock()
    sm._live_models.current = "gemini-test"
    sm._plugins = MagicMock()
    sm._plugins.declarations.return_value = []

    # Mock types
    mock_types = MagicMock()
    mock_types.LiveConnectConfig = lambda **kwargs: kwargs
    monkeypatch.setattr("core.session_manager.types", mock_types)

    config = sm._build_config()
    system_inst = config.get("system_instruction", "")

    assert "[POSTURE VOCALE & PROSODIE INITIALE]" in system_inst
    assert "[Contexte :" in system_inst
    # Le mode de ton arrive après le socle JARVIS, sinon Astro se fait écraser.
    jarvis_at = system_inst.find("style JARVIS")
    if jarvis_at < 0:
        jarvis_at = system_inst.find("Style JARVIS")
    mode_at = system_inst.rfind("[MODE DE TON")
    assert mode_at > jarvis_at >= 0


def test_session_manager_inject_dynamic_prosody():
    """Vérifie l'injection asynchrone dynamique dans le WebSocket Gemini Live."""
    import asyncio

    async def _test():
        sm = SessionManager()
        mock_session = AsyncMock()
        sm.session = mock_session

        success = await sm.inject_dynamic_prosody("chuchoté/nuit")
        assert success is True
        assert mock_session.send_realtime_input.called
        sent_text = mock_session.send_realtime_input.call_args.kwargs.get("text", "")
        assert "[Contexte : Utilisateur chuchote la nuit." in sent_text

    asyncio.run(_test())
