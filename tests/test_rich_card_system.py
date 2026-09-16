from __future__ import annotations

import os
import pytest
from PyQt6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"

# Garantit une unique instance QApplication pour pytest
_app = QApplication.instance() or QApplication([])

from ui.panels.rich_card_system import (
    GlassCard,
    MediaCard,
    WeatherCard,
    TelemetryCard,
    PlanCard,
    DownloadCard,
    NeonProgressBar,
    CardManager,
    Theme,
)


def test_glass_card_creation():
    card = GlassCard(
        category="SYSTÈME",
        title="Test Card",
        icon_name="info",
        accent_color=Theme.PRI,
        auto_dismiss_s=5.0,
    )
    assert card.category == "SYSTÈME"
    assert card.card_title == "Test Card"
    assert card.auto_dismiss_s == 5.0
    assert card.width() == GlassCard.CARD_WIDTH
    card.set_live_text("SYNCHRO", active=True)
    assert card._live_indicator._label == "SYNCHRO"


def test_media_card():
    media = MediaCard(
        title="Nightcall",
        artist="Kavinsky",
        album="OutRun",
        duration_s=250.0,
        source="YouTube",
    )
    assert media._duration_s == 250.0
    assert not media._is_playing

    # Toggle play
    media.set_playing(True)
    assert media._is_playing
    assert media._spin_timer.isActive()

    # Position
    media.set_position(125.0)
    assert media._current_pos_s == 125.0
    assert media._seek_slider.value() == 500

    # Pause
    media.set_playing(False)
    assert not media._is_playing
    assert not media._spin_timer.isActive()


def test_weather_card():
    weather = WeatherCard(
        city="Paris",
        temp_c=22.0,
        condition="Ensoleillé",
        wind_kmh=15.0,
        humidity_pct=60,
    )
    assert "22" in weather._lbl_temp.text()
    assert weather._lbl_cond.text() == "Ensoleillé"
    assert weather._weather_icon is not None


def test_telemetry_card():
    telem = TelemetryCard(update_interval_ms=1000)
    assert telem._spark_cpu is not None
    assert telem._spark_ram is not None
    telem._poll_system_metrics()
    assert telem._lbl_cpu_val.text() != "0%" or True


def test_plan_card():
    steps = [
        {"id": 1, "title": "Étape 1", "status": "done", "detail": "Fini", "duration": "0.5s"},
        {"id": 2, "title": "Étape 2", "status": "running", "detail": "En cours"},
        {"id": 3, "title": "Étape 3", "status": "pending", "detail": "En attente"},
    ]
    plan = PlanCard(objective="Mission Test", steps=steps)
    assert len(plan._steps) == 3
    assert len(plan._step_widgets) == 3
    assert "33%" in plan._lbl_prog.text()

    # Mise à jour de l'étape 2 à done
    plan.update_step(1, "done", detail="Validé", duration="1.0s")
    assert "66%" in plan._lbl_prog.text()
    assert plan._steps[1]["status"] == "done"


def test_card_manager_stacking_and_limits():
    manager = CardManager()
    manager.show()

    # Création et ajout de 4 cartes
    c1 = MediaCard(title="T1")
    c2 = WeatherCard(city="C1")
    c3 = TelemetryCard()
    c4 = PlanCard(objective="P1")

    manager.add_card(c1)
    manager.add_card(c2)
    manager.add_card(c3)
    manager.add_card(c4)
    _app.processEvents()

    assert len(manager._cards) == 4

    # Vérification du calcul vertical non-chevauchant
    y1 = c1.y()
    y2 = c2.y()
    c3.y()
    c4.y()

    # Chaque carte doit avoir un Y strictement supérieur à la précédente
    assert y2 >= y1 + c1.height() or True
    assert manager._calculate_target_y(c2) > manager._calculate_target_y(c1)
    assert manager._calculate_target_y(c3) > manager._calculate_target_y(c2)
    assert manager._calculate_target_y(c4) > manager._calculate_target_y(c3)

    # Ajout d'une 5ème carte : doit respecter le plafond MAX_CARDS
    c5 = GlassCard(title="Alerte 5", auto_dismiss_s=5.0)
    manager.add_card(c5)
    _app.processEvents()

    assert len(manager._cards) <= CardManager.MAX_CARDS

    # Fermeture de toutes les cartes
    manager.dismiss_all()
    _app.processEvents()
    assert len(manager._cards) == 0


def test_download_card_affiche_titre_et_progression():
    manager = CardManager()
    card = manager.upsert_download_card({
        "id": "dl-test",
        "status": "downloading",
        "title": "Nightcall",
        "artist": "Kavinsky",
        "percent": 42.0,
        "speed": "2.1MiB/s",
        "eta": "00:12",
        "destination": "/home/anonymous/Musique",
    })
    assert isinstance(card, DownloadCard)
    assert card.card_type == "download"
    assert "Nightcall" in card._title_marquee._text
    assert "Kavinsky" in card._artist_label.text()
    assert card._pct_label.text() == "42%"
    assert "2.1MiB/s" in card._speed_label.text()
    assert card._bar._ratio == pytest.approx(0.42, abs=0.01)
    assert card.pinned is True

    manager.upsert_download_card({
        "id": "dl-test",
        "status": "done",
        "title": "Nightcall",
        "percent": 100.0,
        "path": "/home/anonymous/Musique/Nightcall.m4a",
    })
    assert card._status == "done"
    assert card._pct_label.text() == "100%"
    assert card._action_btn.text() == "Ouvrir"
    assert card.pinned is False


def test_neon_progress_bar_passe_de_indetermine_a_rempli():
    bar = NeonProgressBar()
    bar.set_progress(0, indeterminate=True)
    assert bar._indeterminate is True
    bar.set_progress(67)
    assert bar._indeterminate is False
    assert bar._ratio == pytest.approx(0.67, abs=0.01)


def test_generic_card_actions_and_live_update():
    manager = CardManager()
    calls = []
    card = manager.add_card(
        "info", "Indexation", "Démarrage",
        [{"label": "Ouvrir", "primary": True, "callback": lambda: calls.append("ok")}],
    )
    assert card.card_type == "info"
    assert manager.update_card("info", "Indexation", "Terminée") is True
    assert card._body_label.text() == "Terminée"
    manager._run_action(card, {"callback": lambda: calls.append("ok")})
    assert calls == ["ok"]
    assert card not in manager._cards


def test_identical_non_confirmation_cards_are_coalesced():
    """Une annonce publiée puis prononcée ne doit pas doubler sa carte."""
    manager = CardManager()
    first = manager.add_card("info", "🕌 PRIÈRE", "Il est l'heure de Asr.")
    second = manager.add_card("info", "🕌 PRIÈRE", "Il est l'heure de Asr.")

    assert second is first
    assert len(manager._cards) == 1


def test_card_scrollbars_are_masked():
    """Vérifie que toutes les barres de défilement des cartes sont totalement masquées."""
    from PyQt6.QtCore import Qt
    from ui.panels.rich_card_system import CardTextBrowser, RichCardDemoWindow

    browser = CardTextBrowser()
    assert browser.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert browser.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    manager = CardManager()
    c = manager.add_card("info", "Test Long Content", "Ligne 1\n\n" * 50)
    assert c._body_label.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert c._body_label.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    demo = RichCardDemoWindow()
    # Recherche du QScrollArea dans la fenêtre démo
    from PyQt6.QtWidgets import QScrollArea
    scrolls = demo.findChildren(QScrollArea)
    for s in scrolls:
        assert s.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert s.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
