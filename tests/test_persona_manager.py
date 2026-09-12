"""tests/test_persona_manager.py — Tests unitaires et d'intégration pour PersonaManager."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.persona_manager import (
    Persona,
    PersonaManager,
    PersonaSwitchResult,
    get_persona_manager,
    set_persona_manager,
)


@pytest.fixture
def persona_mgr():
    """Fixture retournant une instance fraîche de PersonaManager pointant sur config/personas."""
    config_dir = Path(__file__).resolve().parent.parent / "config" / "personas"
    return PersonaManager(config_dir=config_dir)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tests de chargement des configurations YAML
# ══════════════════════════════════════════════════════════════════════════════

def test_yaml_personas_loaded(persona_mgr):
    """Vérifie que les 4 modes métiers YAML sont bien découverts et chargés."""
    personas = persona_mgr.list_personas()
    loaded_ids = {p.id for p in personas}
    
    assert "ironman_jarvis" in loaded_ids
    assert "senior_devops" in loaded_ids
    assert "cyber_sentinel" in loaded_ids
    assert "zen_focus" in loaded_ids
    assert "job_interview_coach" in loaded_ids
    assert "english_learning_coach" in loaded_ids


def test_ironman_jarvis_attributes(persona_mgr):
    """Vérifie la configuration spécifique du persona Ironman / Majordome J.A.R.V.I.S."""
    jarvis = persona_mgr.get_persona("ironman_jarvis")
    assert jarvis is not None
    assert jarvis.accent_color.lower() == "#00d4ff"  # Cyan
    assert jarvis.user_address == "Monsieur"
    assert jarvis.prosody_preset == "flegmatique"
    assert "Monsieur" in jarvis.system_prompt
    assert "mode majordome" in jarvis.voice_triggers
    assert jarvis.orb_palette.get("core") == "#78e1ff"


def test_senior_devops_attributes(persona_mgr):
    """Vérifie la configuration spécifique du persona Senior DevOps / SRE."""
    devops = persona_mgr.get_persona("senior_devops")
    assert devops is not None
    assert devops.accent_color.lower() == "#00ff88"  # Vert
    assert "Rust" in devops.system_prompt
    assert "Linux" in devops.system_prompt
    assert devops.prosody_preset == "technique"
    assert devops.temperature <= 0.15
    assert "mode devops" in devops.voice_triggers
    assert devops.orb_palette.get("core") == "#78ffcb"


def test_cyber_sentinel_attributes(persona_mgr):
    """Vérifie la configuration spécifique du persona Cyber Sentinel."""
    sentinel = persona_mgr.get_persona("cyber_sentinel")
    assert sentinel is not None
    assert sentinel.accent_color.lower() == "#ff3355"  # Rouge
    assert "sécurité" in sentinel.system_prompt.lower()
    assert sentinel.user_address == "Opérateur"
    assert "mode sentinelle" in sentinel.voice_triggers
    assert sentinel.orb_palette.get("core") == "#ff3c5a"


def test_zen_focus_attributes(persona_mgr):
    """Vérifie la configuration spécifique du persona Zen Focus."""
    zen = persona_mgr.get_persona("zen_focus")
    assert zen is not None
    assert zen.accent_color.lower() == "#8f5cff"  # Violet / Indigo néon
    assert zen.block_interruptions is True
    assert zen.prosody_preset == "chuchote"
    assert "chuchote" in zen.system_prompt.lower()
    assert "mode zen" in zen.voice_triggers


# ══════════════════════════════════════════════════════════════════════════════
# 2. Tests de détection vocale des triggers (reconnaissance naturelle)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "utterance,expected_id",
    [
        # Commandes exactes de la demande utilisateur
        ("Jarvis, passe en mode DevOps", "senior_devops"),
        ("Passe en mode Majordome", "ironman_jarvis"),
        # Variantes DevOps
        ("Active le mode DevOps s'il te plaît", "senior_devops"),
        ("Mets-toi en mode devops", "senior_devops"),
        ("Bascule sur senior devops", "senior_devops"),
        ("Passe en mode expert rust", "senior_devops"),
        ("mode devops", "senior_devops"),
        ("devops", "senior_devops"),
        # Variantes Majordome / Jarvis
        ("Reviens en mode Jarvis", "ironman_jarvis"),
        ("Passe en mode majordome", "ironman_jarvis"),
        ("Mets le mode jarvis", "ironman_jarvis"),
        ("Remets le majordome", "ironman_jarvis"),
        ("majordome", "ironman_jarvis"),
        # Variantes Cyber Sentinel
        ("Active le mode sentinelle", "cyber_sentinel"),
        ("Passe en mode sécurité", "cyber_sentinel"),
        ("Mode securite", "cyber_sentinel"),
        ("Bascule sur cyber sentinel", "cyber_sentinel"),
        ("Mets le mode secops", "cyber_sentinel"),
        # Variantes Zen Focus
        ("Passe en mode zen", "zen_focus"),
        ("Active le mode focus", "zen_focus"),
        ("Mets-toi en mode concentration", "zen_focus"),
        ("Mode calme", "zen_focus"),
        ("Passe en mode silence", "zen_focus"),
    ],
)
def test_voice_trigger_detection(persona_mgr, utterance, expected_id):
    """Vérifie la robustesse de détection des commandes vocales de basculement."""
    matched = persona_mgr.detect_trigger(utterance)
    assert matched is not None, f"Échec de détection pour la phrase: '{utterance}'"
    assert matched.id == expected_id, f"Attendu {expected_id}, obtenu {matched.id} pour '{utterance}'"


def test_voice_trigger_no_match_on_regular_speech(persona_mgr):
    """Vérifie qu'une phrase de conversation ordinaire ne déclenche pas de commutation intempestive."""
    regular_phrases = [
        "Bonjour Jarvis, comment vas-tu ce matin ?",
        "Ouvre le navigateur Firefox et cherche la météo",
        "Quel est l'état de la mémoire RAM ?",
        "Lis mes derniers emails",
        "Ajoute un rappel pour demain à 14h",
        "Peux-tu m'expliquer le fonctionnement du borrow checker en Rust ?",
        "Il fait calme dehors aujourd'hui",
    ]
    for phrase in regular_phrases:
        matched = persona_mgr.detect_trigger(phrase)
        assert matched is None, f"Faux positif détecté pour la phrase: '{phrase}'"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tests de génération des directives et re-génération à chaud
# ══════════════════════════════════════════════════════════════════════════════

def test_build_system_instruction(persona_mgr):
    """Vérifie l'assemblage complet du system prompt avec le persona."""
    jarvis = persona_mgr.get_persona("ironman_jarvis")
    prompt = persona_mgr.build_system_instruction(
        persona=jarvis,
        base_prompt="ENV: Arch Linux / Hyprland",
        asst_name="ANO-GPT",
    )
    assert "ADRESSE UTILISATEUR : Appelle toujours l'utilisateur 'Monsieur'." in prompt
    assert "[MODE MÉTIER ACTIF — MAJORDOME]" in prompt
    assert "ENV: Arch Linux / Hyprland" in prompt


def test_build_hot_directive(persona_mgr):
    """Vérifie le formatage de la directive temps réel sans coupure WebSocket."""
    devops = persona_mgr.get_persona("senior_devops")
    directive = persona_mgr.build_hot_directive(devops)

    assert "[DIRECTIVE SYSTÈME PRIORITAIRE — COMMUTATION DE PERSONA IMMÉDIATE]" in directive
    assert "SENIOR DEVOPS / SRE" in directive
    assert "Rust" in directive
    assert "sans jamais réciter cette directive" in directive


# ══════════════════════════════════════════════════════════════════════════════
# 4. Tests de commutation dynamique sans coupure WebSocket
# ══════════════════════════════════════════════════════════════════════════════

def test_dynamic_switching_websocket_hot_reload(persona_mgr):
    """Vérifie la commutation à chaud dans Gemini Live sans couper le WebSocket."""
    async def _run():
        # Mock de session Gemini Live
        mock_session = MagicMock()
        mock_session.send_realtime_input = AsyncMock()
        mock_session.close = MagicMock()  # Ne doit JAMAIS être appelé

        # Mock de SessionManager
        mock_sm = MagicMock()
        mock_sm.session = mock_session
        mock_sm._current_persona = persona_mgr.get_persona("ironman_jarvis")
        mock_sm.inject_dynamic_prosody = AsyncMock()

        # Mock de JarvisUI
        mock_ui = MagicMock()
        mock_ui.set_accent_color = MagicMock()
        mock_ui.write_log = MagicMock()

        # Commutation vers DevOps
        res = await persona_mgr.async_switch_persona(
            "senior_devops",
            session_manager=mock_sm,
            ui=mock_ui,
        )

        # 1. Vérification du résultat
        assert res.success is True
        assert res.persona.id == "senior_devops"
        assert res.previous_persona.id == "ironman_jarvis"
        assert res.hot_reloaded is True

        # 2. Vérification que send_realtime_input a reçu la directive sans fermer la socket
        mock_session.send_realtime_input.assert_awaited_once()
        call_args, call_kwargs = mock_session.send_realtime_input.call_args
        sent_text = call_kwargs.get("text") or (call_args[0] if call_args else "")
        assert "SENIOR DEVOPS / SRE" in sent_text
        assert mock_session.close.call_count == 0  # WebSocket non coupé !

        # 3. Vérification de l'adaptation simultanée de la couleur de l'orbe UI (Vert = #00ff88)
        mock_ui.set_accent_color.assert_called_once_with(
            "#00ff88",
            persona_mgr.get_persona("senior_devops").orb_palette,
        )
        mock_ui.write_log.assert_called()

        # 4. Vérification de la mise à jour de SessionManager
        assert mock_sm._current_persona.id == "senior_devops"

    asyncio.run(_run())


def test_switching_to_cyber_sentinel_orb_accent(persona_mgr):
    """Vérifie la commutation vers Cyber Sentinel et l'accent rouge (#ff3355)."""
    async def _run():
        mock_session = MagicMock()
        mock_session.send_realtime_input = AsyncMock()

        mock_sm = MagicMock()
        mock_sm.session = mock_session
        mock_sm._current_persona = persona_mgr.get_persona("ironman_jarvis")
        mock_sm.inject_dynamic_prosody = AsyncMock()

        mock_ui = MagicMock()

        res = await persona_mgr.async_switch_persona(
            "cyber_sentinel",
            session_manager=mock_sm,
            ui=mock_ui,
        )

        assert res.success is True
        assert res.persona.id == "cyber_sentinel"
        assert res.accent_color.lower() == "#ff3355"

        # Vérification accent rouge envoyé à l'UI
        mock_ui.set_accent_color.assert_called_once_with(
            "#ff3355",
            persona_mgr.get_persona("cyber_sentinel").orb_palette,
        )

    asyncio.run(_run())


def test_switching_to_zen_focus(persona_mgr):
    """Vérifie la commutation vers Zen Focus et ses propriétés (accent violet, prosodie chuchotée)."""
    async def _run():
        mock_session = MagicMock()
        mock_session.send_realtime_input = AsyncMock()

        mock_sm = MagicMock()
        mock_sm.session = mock_session
        mock_sm._current_persona = persona_mgr.get_persona("ironman_jarvis")
        mock_sm.inject_dynamic_prosody = AsyncMock()

        mock_ui = MagicMock()

        res = await persona_mgr.async_switch_persona(
            "zen_focus",
            session_manager=mock_sm,
            ui=mock_ui,
        )

        assert res.success is True
        assert res.persona.id == "zen_focus"
        assert res.accent_color.lower() == "#8f5cff"
        assert res.persona.block_interruptions is True
        mock_sm.inject_dynamic_prosody.assert_awaited_with("chuchote")

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 5. Tests d'écouteurs et d'historique
# ══════════════════════════════════════════════════════════════════════════════

def test_listeners_and_history(persona_mgr):
    """Vérifie que les callbacks et l'historique enregistrent les commutations."""
    async def _run():
        transitions = []

        def on_switch(prev, current):
            transitions.append((prev.id if prev else None, current.id))

        persona_mgr.add_listener(on_switch)

        await persona_mgr.async_switch_persona("senior_devops")
        await persona_mgr.async_switch_persona("zen_focus")

        assert len(transitions) == 2
        assert transitions[0] == ("ironman_jarvis", "senior_devops")
        assert transitions[1] == ("senior_devops", "zen_focus")

        # Test de suppression du listener
        persona_mgr.remove_listener(on_switch)
        await persona_mgr.async_switch_persona("ironman_jarvis")
        assert len(transitions) == 2  # N'a pas bougé après remove_listener

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 6. Test d'intégration avec SessionManager._build_config
# ══════════════════════════════════════════════════════════════════════════════

def test_session_manager_integration_build_config(monkeypatch):
    """Vérifie que SessionManager._build_config intègre automatiquement le persona actif."""
    from core.session_manager import SessionManager

    sm = SessionManager()
    sm._asst_name = "ANO-GPT"
    sm._recalled_ids = set()
    sm._conn = MagicMock()
    sm._conn.resume_handle.return_value = None
    sm._context_compression_enabled = False
    sm._live_voice = "Charon"
    sm._plugins = MagicMock()
    sm._plugins.declarations.return_value = []
    # Le modèle vocal appartient à JarvisLive, pas au mixin : le prompt le cite
    # désormais pour distinguer la voix du cerveau.
    sm._live_models = MagicMock()
    sm._live_models.current = "gemini-live-2.5-flash"

    # Mock types
    mock_types = MagicMock()
    mock_types.LiveConnectConfig = lambda **kwargs: kwargs
    monkeypatch.setattr("core.session_manager.types", mock_types)

    # Assigne le persona DevOps à SessionManager
    pm = get_persona_manager()
    sm._current_persona = pm.get_persona("senior_devops")

    config = sm._build_config()
    sys_inst = config.get("system_instruction", "")

    assert sm._asst_name == "ANO-GPT"
    assert "Always refer to yourself as ANO-GPT" in sys_inst

    # Les personas métier ne sont plus injectés dans la session : les quatre
    # modes de ton sont la seule autorité sur l'identité et l'adresse. Seuls
    # les personas pédagogiques (entretien, cours d'anglais) survivent, parce
    # qu'ils sont demandés explicitement et doivent tenir une reconnexion.
    assert "[MODE MÉTIER ACTIF — SENIOR DEVOPS / SRE]" not in sys_inst

    sm._current_persona = pm.get_persona("english_learning_coach")
    sys_inst = sm._build_config()["system_instruction"]
    assert "english_learning_coach" in sys_inst.lower() or "anglais" in sys_inst.lower()


def test_default_identity_is_independent_of_persona(persona_mgr):
    for persona in persona_mgr.list_personas():
        prompt = persona_mgr.build_system_instruction(persona=persona)
        assert "Nom de l'assistant : ANO-GPT" in prompt
        assert "Tu es J.A.R.V.I.S" not in prompt
        assert "Ton nom reste ANO-GPT" in persona_mgr.build_hot_directive(persona)
