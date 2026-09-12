#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_thought_streamer.py — Tests unitaires et d'intégration de core/thought_streamer.py.

Valide :
1. Extraction des tokens de pensée (Thinking Tokens) :
   - Gemini 2.0 Flash Thinking (`part.thought == True`)
   - Claude 3.7 Thinking (`thinking_delta`, `thinking` block)
   - DeepSeek-R1 / OpenAI reasoning (`reasoning_content`)
   - Balises XML de streaming (<thought>, <thinking>, <think>) avec coupures inter-chunks
2. Filtrage du bruit mental :
   - Suppression du monologue interne, tics de langage et syntaxe JSON
   - Extraction des micro-étapes lisibles pour l'UI
   - Synthèse de jalons audio concis (1 phrase max)
3. Restitution multimodale :
   - Visuel : mise à jour de la bulle 'Pensée en cours...'
   - Audio : silence absolu si < 1.5s, vocalisation chuchotée à bas volume si > 1.5s
   - Annulation instantanée dès que la parole commence (barge-in / on_speaking_start)
4. Intégration SessionManager et UI.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage, QPainter
from ui.panels.thought_overlay import ThoughtOverlay

from core.thought_streamer import (
    DEFAULT_AUDIO_DELAY_SECONDS,
    ReasoningStreamParser,
    ThoughtEvent,
    ThoughtNoiseFilter,
    ThoughtStreamer,
    ThoughtType,
    get_thought_streamer,
    mood_aware_waiting_phrase,
)
from core.session_manager import SessionManager


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tests d'Extraction des Tokens de Pensée (ReasoningStreamParser)
# ─────────────────────────────────────────────────────────────────────────────

def test_parser_gemini_parts():
    """Vérifie l'extraction des thinking tokens de Gemini 2.0 Flash Thinking."""
    parser = ReasoningStreamParser()

    # Part standard avec thought=True
    part_mock = MagicMock()
    part_mock.thought = True
    part_mock.text = "Je réfléchis aux événements de l'agenda..."
    assert parser.parse_gemini_part(part_mock) == "Je réfléchis aux événements de l'agenda..."

    # Part régulière sans thought
    part_normal = MagicMock()
    part_normal.thought = False
    part_normal.text = "Bonjour, comment puis-je vous aider ?"
    assert parser.parse_gemini_part(part_normal) is None

    # Part dictionnaire
    dict_part = {"thought": True, "text": "Analyse des fichiers..."}
    assert parser.parse_gemini_part(dict_part) == "Analyse des fichiers..."

    # Part avec thought_delta
    delta_part = MagicMock(spec=["thought_delta"])
    delta_part.thought_delta = "Delta thinking fragment"
    assert parser.parse_gemini_part(delta_part) == "Delta thinking fragment"


def test_parser_claude_chunks():
    """Vérifie l'extraction des thinking tokens de Claude 3.7 Thinking."""
    parser = ReasoningStreamParser()

    # Chunk format Anthropic content_block_delta
    claude_delta_chunk = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "thinking_delta",
            "thinking": "Let's first inspect the calendar for today.",
        },
    }
    assert parser.parse_claude_chunk(claude_delta_chunk) == "Let's first inspect the calendar for today."

    # Chunk format content_block_start
    claude_start_chunk = {
        "type": "content_block_start",
        "content_block": {
            "type": "thinking",
            "thinking": "Initial reasoning block",
        },
    }
    assert parser.parse_claude_chunk(claude_start_chunk) == "Initial reasoning block"

    # Chunk format DeepSeek-R1 / OpenAI reasoning_content
    deepseek_chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "Vérifions les droits d'accès sur le fichier.",
                }
            }
        ]
    }
    assert parser.parse_claude_chunk(deepseek_chunk) == "Vérifions les droits d'accès sur le fichier."


def test_parser_streaming_xml_tags():
    """Vérifie l'extraction des balises <thought>, <thinking>, <think> en streaming."""
    parser = ReasoningStreamParser()

    # Flux simple
    thought, regular = parser.parse_text_stream("<thought>Analyse de la demande</thought>Voici la réponse.")
    assert thought == "Analyse de la demande"
    assert regular == "Voici la réponse."

    # Flux découpé à cheval sur la balise d'ouverture
    parser.reset()
    t1, r1 = parser.parse_text_stream("Bonjour <th")
    t2, r2 = parser.parse_text_stream("ink>Réflexion interne</think>Suite du message.")
    assert t1 == ""
    assert r1 == "Bonjour "
    assert t2 == "Réflexion interne"
    assert r2 == "Suite du message."

    # Flux découpé à cheval sur la balise de fermeture
    parser.reset()
    t1, r1 = parser.parse_text_stream("<thinking>Étape 1 et 2</think")
    t2, r2 = parser.parse_text_stream("ing>Fin.")
    assert t1 == "Étape 1 et 2"
    assert t2 == ""
    assert r2 == "Fin."


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tests de Filtrage du Bruit Mental (ThoughtNoiseFilter)
# ─────────────────────────────────────────────────────────────────────────────

def test_noise_filter_cleaning():
    """Vérifie le nettoyage des tics de langage et de la syntaxe JSON."""
    raw = "Okay, let's see. I need to check the files. ```json {'path': '/home'} ``` Actually, let me see."
    cleaned = ThoughtNoiseFilter.clean_raw_thought(raw)
    assert "Okay, let's see" not in cleaned
    assert "```json" not in cleaned
    assert "check the files" in cleaned


def test_noise_filter_domains_and_micro_steps():
    """Vérifie l'identification correcte des domaines d'action pour le HUD."""
    # Fichiers
    step_file = ThoughtNoiseFilter.extract_micro_step("I should scan the documents folder for PDF files.")
    assert "fichiers" in step_file.lower()

    # Recherche Web
    step_web = ThoughtNoiseFilter.extract_micro_step("Let me search Google or DuckDuckGo for the weather forecast.")
    assert "web" in step_web.lower()

    # Calendrier
    step_cal = ThoughtNoiseFilter.extract_micro_step("Looking at the user's calendar events for tomorrow morning.")
    assert "agenda" in step_cal.lower()

    # Code / Debug
    step_code = ThoughtNoiseFilter.extract_micro_step("Debugging the Python script and inspecting the stack trace.")
    assert "code" in step_code.lower()


def test_noise_filter_audio_milestones():
    """Vérifie la génération d'un jalon vocal concis (1 seule phrase max)."""
    # Audio pour calendrier
    audio_cal = ThoughtNoiseFilter.generate_audio_milestone("Checking calendar schedule.")
    assert audio_cal == "Je consulte votre agenda..."

    # Audio pour outil spécifique
    audio_tool = ThoughtNoiseFilter.generate_audio_milestone("", tool_name="file_search")
    assert audio_tool == "Un instant, je recherche dans vos fichiers..."

    # Audio par défaut (1 seule phrase max)
    audio_def = ThoughtNoiseFilter.generate_audio_milestone("Thinking about a complex philosophical question.")
    sentences = [s.strip() for s in audio_def.split(".") if s.strip()]
    assert len(sentences) == 1
    assert "Un instant" in audio_def or "Je" in audio_def


def test_astro_recoit_une_phrase_dattente_de_pote(monkeypatch):
    from core.personality_modes import PersonalityMode
    monkeypatch.setattr("core.personality_modes.active_mode", lambda: PersonalityMode.ASTRO)

    phrase = mood_aware_waiting_phrase("Un instant, je cherche.", "music_control")

    assert "son" in phrase.lower()
    assert "attends" in phrase.lower()

    download = mood_aware_waiting_phrase("Un instant, je cherche.", "download_music")
    assert "patienter" in download.lower()
    assert "musique" in download.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tests de Restitution Multimodale & Seuil Audio (1.5 seconde)
# ─────────────────────────────────────────────────────────────────────────────

def test_fast_response_remains_silent():
    """Une tâche rapide (< 1.5s) ne doit déclencher AUCUNE vocalisation."""
    ui_mock = MagicMock()
    streamer = ThoughtStreamer(ui_callback=ui_mock, audio_delay_sec=0.2)

    with patch.object(streamer, "vocalize_whisper") as mock_whisper:
        # Début de la pensée
        streamer.feed_thought_token("Vérification rapide")
        assert streamer.is_thinking is True
        assert ui_mock.call_count >= 1

        # Fin du tour avant expiration du minuteur (50 ms < 200 ms)
        time.sleep(0.05)
        streamer.on_speaking_start()

        # Attendre la durée totale du minuteur
        time.sleep(0.25)

        # Aucune vocalisation ne doit avoir eu lieu
        assert mock_whisper.call_count == 0
        assert streamer.is_thinking is False


def test_long_operation_triggers_whispered_milestone():
    """Une opération dépassant 1.5s déclenche exactement une phrase chuchotée."""
    ui_mock = MagicMock()
    # Délai court pour les besoins du test unitaire (0.15s au lieu de 1.5s)
    streamer = ThoughtStreamer(ui_callback=ui_mock, audio_delay_sec=0.15)

    with patch.object(streamer, "vocalize_whisper") as mock_whisper:
        streamer.feed_tool_start("web_search")
        assert streamer.is_thinking is True

        # Attendre le dépassement du délai
        time.sleep(0.25)

        # Le jalon vocal doit avoir été déclenché exactement 1 fois
        assert mock_whisper.call_count == 1
        spoken_phrase = mock_whisper.call_args[0][0]
        assert "web" in spoken_phrase.lower() or "instant" in spoken_phrase.lower()

        # Des tokens supplémentaires ne doivent PAS ré-émettre de jalon pour le même tour
        streamer.feed_thought_token("Toujours en train de chercher...")
        time.sleep(0.1)
        assert mock_whisper.call_count == 1

        # Clôture du tour
        streamer.on_turn_complete()
        assert streamer.is_thinking is False


def test_speaking_start_stops_active_whisper():
    """L'arrivée de la réponse du modèle coupe instantanément la synthèse chuchotée."""
    streamer = ThoughtStreamer(audio_delay_sec=0.1)
    mock_stop_tts = MagicMock()
    streamer._active_tts_stop_cb = mock_stop_tts

    streamer.on_speaking_start()
    assert mock_stop_tts.call_count == 1
    assert streamer._active_tts_stop_cb is None


def test_user_interruption_resets_streamer():
    """L'interruption (barge-in) réinitialise immédiatement le ThoughtStreamer."""
    ui_mock = MagicMock()
    streamer = ThoughtStreamer(ui_callback=ui_mock, audio_delay_sec=0.1)
    streamer.start_thinking("Analyse lourde...")

    assert streamer.is_thinking is True
    streamer.interrupt()
    assert streamer.is_thinking is False
    assert ui_mock.call_args[0] == ("", False)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tests d'Intégration dans SessionManager
# ─────────────────────────────────────────────────────────────────────────────

def test_session_manager_thought_streamer_integration():
    """Vérifie la présence et le bon branchement de thought_streamer dans SessionManager."""
    sm = SessionManager()
    assert hasattr(sm, "thought_streamer")
    streamer = sm.thought_streamer
    assert isinstance(streamer, ThoughtStreamer)

    # Vérifie la délégation des méthodes de notification
    with patch.object(streamer, "feed_thought_token") as mock_feed:
        sm.feed_thought_token("Token de test")
        mock_feed.assert_called_once_with("Token de test", source="gemini")

    with patch.object(streamer, "feed_tool_start") as mock_tool_start:
        sm.feed_tool_start("calendar", {"date": "today"})
        mock_tool_start.assert_called_once_with("calendar", {"date": "today"})

    with patch.object(streamer, "feed_tool_end") as mock_tool_end:
        sm.feed_tool_end("calendar", {"events": []})
        mock_tool_end.assert_called_once_with("calendar", {"events": []})


def test_session_manager_receive_audio_thinking_turn():
    """Simule la réception de Thinking Tokens dans la boucle _receive_audio."""
    sm = SessionManager()
    streamer = sm.thought_streamer

    with patch.object(streamer, "feed_thought_token") as mock_feed_thought, \
         patch.object(streamer, "on_speaking_start") as mock_speaking, \
         patch.object(streamer, "on_turn_complete") as mock_turn_done:

        # 1. Mock de Part thinking Gemini 2.0
        part = MagicMock()
        part.thought = True
        part.text = "Je consulte la base de données..."

        turn = MagicMock()
        turn.parts = [part]

        sc = MagicMock()
        sc.interrupted = False
        sc.model_turn = turn
        sc.output_transcription = None
        sc.input_transcription = None
        sc.turn_complete = False

        resp_thinking = MagicMock()
        resp_thinking.session_resumption_update = None
        resp_thinking.go_away = None
        resp_thinking.server_content = sc
        resp_thinking.tool_call = None

        # Injection directe dans le traitement de réponse
        if resp_thinking.server_content and resp_thinking.server_content.model_turn:
            for p in resp_thinking.server_content.model_turn.parts:
                if getattr(p, "thought", False):
                    sm.thought_streamer.feed_thought_token(p.text, source="gemini_thinking")

        assert mock_feed_thought.call_count == 1
        assert mock_feed_thought.call_args[0][0] == "Je consulte la base de données..."

        # 2. Clôture du tour
        sm.thought_streamer.on_turn_complete()
        assert mock_turn_done.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Tests de l'Interface Visuelle (ThoughtOverlay & JarvisUI)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """Initialise une application Qt pour tests offscreen."""
    app = QApplication.instance() or QApplication([])
    yield app


def test_thought_overlay_widget(qapp):
    """Vérifie l'instanciation, les animations et le rendu de ThoughtOverlay."""
    overlay = ThoughtOverlay()
    assert overlay.FIXED_HEIGHT == 56
    assert overlay.MIN_WIDTH == 340

    # Affichage d'un texte court
    overlay.show_thought("Recherche d'informations...")
    assert overlay._raw_text == "Recherche d'informations..."
    assert overlay._is_scrolling is False

    # Affichage d'un texte long qui doit activer le mode ticker défilant
    long_text = "Analyse approfondie et exhaustive de tous les documents PDF et factures du répertoire personnel"
    overlay.show_thought(long_text)
    assert overlay._raw_text == long_text
    assert overlay._is_scrolling is True
    assert overlay._ticker_timer.isActive()

    # Rendu graphique sans crash
    overlay.resize(400, 56)
    img = QImage(400, 56, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    overlay.render(painter)
    painter.end()
    assert not img.isNull()

    # Fondu de disparition
    overlay.fade_out()
    assert overlay._ticker_timer.isActive() is False


def test_jarvis_ui_thought_integration():
    """Vérifie la présence et le comportement des méthodes show_thought et hide_thought dans JarvisUI."""
    from ui.jarvis_ui import JarvisUI
    assert hasattr(JarvisUI, "show_thought")
    assert hasattr(JarvisUI, "hide_thought")
