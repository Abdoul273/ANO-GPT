"""tests/test_multimodal_vision.py — Tests unitaires pour core/multimodal_vision.py."""

import pytest
from unittest.mock import patch, MagicMock
from core.screen_capture import WindowInfo
from core.multimodal_vision import (
    detect_visual_domain,
    _build_domain_system_prompt,
    analyze_visual_content,
    capture_policy,
    inspect_screen_live,
    should_use_expert_vision,
    vision_model_cascade,
    wants_pointing,
    VisionAnalysisResult,
    _normalize_targets,
    _boxes_to_pixels,
)


def test_detect_visual_domain_keywords():
    assert detect_visual_domain("Explique-moi ce schéma d'architecture réseau") == "architecture"
    assert detect_visual_domain("Analyse la topologie du cluster Kubernetes") == "architecture"
    assert detect_visual_domain("Décris ce graphique et les métriques de la courbe") == "chart"
    assert detect_visual_domain("Résume ce document PDF technique") == "document"
    assert detect_visual_domain("Regarde ce code et trouve la fonction") == "code"
    assert detect_visual_domain("C'est quoi ce bug et ce traceback ?") == "debug"
    assert detect_visual_domain("Que vois-tu à l'écran ?") == "general"


def test_detect_visual_domain_contextual():
    win_term = WindowInfo(window_class="kitty", title="zsh")
    assert detect_visual_domain("Aide-moi sur ce qui est affiché", window_info=win_term) == "debug"

    win_ide = WindowInfo(window_class="code", title="main.rs")
    assert detect_visual_domain("Analyse ce fichier", window_info=win_ide) == "code"

    win_pdf = WindowInfo(window_class="zathura", title="report.pdf")
    assert detect_visual_domain("Lis la page", window_info=win_pdf) == "document"


def test_build_domain_system_prompt():
    prompt_arch = _build_domain_system_prompt("architecture")
    assert "SCHÉMA D'ARCHITECTURE & RÉSEAU" in prompt_arch
    assert "goulots d'étranglement" in prompt_arch

    prompt_chart = _build_domain_system_prompt("chart")
    assert "GRAPHIQUES & VISUALISATION DE DONNÉES" in prompt_chart
    assert "axes X et Y" in prompt_chart

    prompt_doc = _build_domain_system_prompt("document")
    assert "DOCUMENTS, PDFS & TABLEAUX" in prompt_doc

    prompt_code = _build_domain_system_prompt("code")
    assert "ANALYSE DE CODE & LOGIQUE ALGORITHMIQUE" in prompt_code


def test_analyze_visual_content_mock():
    mock_resp = MagicMock()
    mock_resp.text = """{
        "spoken_summary": "Il s'agit d'une architecture microservices avec ingress Nginx et base PostgreSQL.",
        "key_points": ["Ingress Nginx", "PostgreSQL", "Cluster Redis"],
        "detailed_markdown": "# Architecture\\n- Ingress Nginx\\n- Base Postgres"
    }"""

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    with patch("core.multimodal_vision._get_api_key", return_value="fake_key"), \
         patch("google.genai.Client", return_value=mock_client), \
         patch("core.screen_reader.read", return_value=None):
        res = analyze_visual_content(
            image_bytes=b"fake_jpeg",
            mime_type="image/jpeg",
            user_query="Explique ce schéma",
            domain="architecture",
        )

        assert isinstance(res, VisionAnalysisResult)
        assert res.domain == "architecture"
        assert "architecture microservices" in res.spoken_summary
        assert len(res.key_points) == 3
        assert res.hud_card["title"] == "🏗️ Architecture & Réseau"


def test_inspect_screen_live():
    mock_res = VisionAnalysisResult(
        domain="chart",
        spoken_summary="La courbe montre une hausse de 35% du trafic.",
        detailed_markdown="# Métriques",
        hud_card={"title": "📊 Données & Graphiques", "body": "Détails", "type": "info"},
    )
    mock_player = MagicMock()

    with patch("core.multimodal_vision.screen_capture.capture_window_or_screen", return_value=(b"fake_img", "image/jpeg", {})), \
         patch("core.multimodal_vision.screen_capture.get_active_window", return_value=None), \
         patch("core.multimodal_vision.analyze_visual_content", return_value=mock_res):
        spoken, res = inspect_screen_live(
            user_query="Analyse cette courbe",
            domain="chart",
            player=mock_player,
        )

        assert "hausse de 35%" in spoken
        assert res == mock_res
        mock_player.show_card.assert_called_once_with(
            type="info",
            title="📊 Données & Graphiques",
            body="Détails",
        )


def test_vision_model_cascade_puts_pro_first():
    models = vision_model_cascade({"vision_model": "", "vision_model_fallback": "gemini-flash-latest"})
    assert models[0] == "gemini-pro-latest"
    assert "gemini-flash-latest" in models
    assert models.count("gemini-flash-latest") == 1

    custom = vision_model_cascade({
        "vision_model": "gemini-3-pro-preview",
        "vision_model_fallback": "gemini-flash-latest",
    })
    assert custom[0] == "gemini-3-pro-preview"


def test_capture_policy_keeps_text_sharp():
    term = WindowInfo(window_class="kitty", title="zsh")
    policy = capture_policy(term, "general")
    assert policy["max_dim"][0] >= 2560
    assert policy["quality"] >= 90

    chart = capture_policy(None, "chart")
    assert chart["max_dim"] == (1920, 1080)


def test_expert_vision_triggers_on_schema_not_on_plain_ocr():
    assert should_use_expert_vision("Explique ce schéma", ocr_usable=True, domain="architecture") is True
    assert should_use_expert_vision("où est le bouton Valider", ocr_usable=True, domain="ui") is True
    assert should_use_expert_vision("lis juste le texte", ocr_usable=True, domain="general") is False
    assert should_use_expert_vision("que vois-tu", ocr_usable=False, domain="general") is True
    assert should_use_expert_vision("qu'est-ce que la caméra filme", angle="camera") is True
    assert wants_pointing("montre-moi le bouton Envoyer") is True
    assert wants_pointing("résume ce paragraphe") is False


def test_ui_targets_convert_to_pixels_and_tool_result():
    targets = _normalize_targets([
        {"label": "Valider", "ymin": 100, "xmin": 200, "ymax": 200, "xmax": 400},
        {"label": "vide", "ymin": 10, "xmin": 10, "ymax": 10, "xmax": 10},
    ])
    assert len(targets) == 1
    assert targets[0]["label"] == "Valider"
    box = _boxes_to_pixels(targets[0], (100, 50), (1000, 1000))
    assert box == (300, 150, 200, 100)

    result = VisionAnalysisResult(
        domain="ui",
        spoken_summary="Le bouton Valider est en bas à droite.",
        detailed_markdown="UI",
        ui_targets=targets,
        model_used="gemini-pro-latest",
    )
    payload = result.as_tool_result("où est Valider")
    assert "[VISION EXPERTE" in payload
    assert "Valider" in payload
    assert "Ne rappelle PAS screen_process" in payload
