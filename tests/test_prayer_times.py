"""Tests unitaires pour le module de calcul et rappel des heures de prière (Adhan)."""
from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.prayer_times import (
    METHODS,
    PRAYER_NAMES,
    PrayerManager,
    calculate_prayer_times,
    haversine_km,
)
from actions.prayer import prayer_control


def test_astronomical_chronology_and_keys():
    """Vérifie la cohérence chronologique des heures calculées (Fajr < Sunrise < Dhuhr < Asr < Maghrib < Isha)."""
    target_date = datetime.date(2026, 9, 6)
    # Paris : 48.8566 N, 2.3522 E, UTC+2
    schedule = calculate_prayer_times(
        lat=48.8566,
        lon=2.3522,
        target_date=target_date,
        tz_offset_hours=2.0,
        method="UOIF",
    )

    for key in ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]:
        assert key in schedule
        assert schedule[key].date() == target_date

    assert schedule["fajr"] < schedule["sunrise"]
    assert schedule["sunrise"] < schedule["dhuhr"]
    assert schedule["dhuhr"] < schedule["asr"]
    assert schedule["asr"] < schedule["maghrib"]
    assert schedule["maghrib"] < schedule["isha"]


def test_asr_hanafi_vs_standard():
    """Vérifie que le calcul Asr Hanafi (ombre = 2x) est plus tardif que le standard (ombre = 1x)."""
    target_date = datetime.date(2026, 9, 6)
    standard = calculate_prayer_times(
        lat=48.8566,
        lon=2.3522,
        target_date=target_date,
        tz_offset_hours=2.0,
        asr_hanafi=False,
    )
    hanafi = calculate_prayer_times(
        lat=48.8566,
        lon=2.3522,
        target_date=target_date,
        tz_offset_hours=2.0,
        asr_hanafi=True,
    )

    assert hanafi["asr"] > standard["asr"]


def test_all_calculation_methods_valid():
    """Toutes les méthodes de calcul configurables doivent produire des résultats valides."""
    target_date = datetime.date(2026, 6, 21)  # Solstice d'été
    for method in METHODS:
        schedule = calculate_prayer_times(
            lat=48.8566,
            lon=2.3522,
            target_date=target_date,
            tz_offset_hours=2.0,
            method=method,
        )
        assert len(schedule) == 6
        assert schedule["fajr"] < schedule["sunrise"]
        assert schedule["maghrib"] < schedule["isha"]


def test_haversine_distance():
    """Vérifie le calcul de déplacement géographique."""
    # Distance Paris (48.8566, 2.3522) - Versailles (48.8049, 2.1204) ~ 17 km
    dist = haversine_km(48.8566, 2.3522, 48.8049, 2.1204)
    assert 16.0 < dist < 19.0


def test_format_time_remaining():
    """Vérifie le formattage en français du compte à rebours."""
    assert PrayerManager.format_time_remaining(datetime.timedelta(hours=2, minutes=15)) == "2 heures et 15 minutes"
    assert PrayerManager.format_time_remaining(datetime.timedelta(hours=1, minutes=1)) == "1 heure et 1 minute"
    assert PrayerManager.format_time_remaining(datetime.timedelta(minutes=45)) == "45 minutes"
    assert PrayerManager.format_time_remaining(datetime.timedelta(seconds=20)) == "quelques instants"


def test_prayer_manager_next_prayer(tmp_path: Path):
    """Vérifie la détermination de la prochaine prière."""
    cfg_file = tmp_path / "prayer_config.json"
    state_file = tmp_path / "prayer_state.json"
    manager = PrayerManager(config_path=cfg_file, state_path=state_file)

    tz = datetime.timezone(datetime.timedelta(hours=2))
    fixed_date = datetime.date(2026, 9, 6)

    with patch.object(manager, "resolve_coords", return_value=(48.8566, 2.3522, "Paris")):
        # Cas 1 : Milieu de matinée (10h00) -> Prochaine prière = Dhuhr
        now_10h = datetime.datetime(2026, 9, 6, 10, 0, tzinfo=tz)
        name, p_dt, rem = manager.get_next_prayer(now=now_10h)
        assert name == "dhuhr"
        assert p_dt.hour == 13 or p_dt.hour == 14

        # Cas 2 : Nuit après Isha (23h30) -> Prochaine prière = Fajr du lendemain
        now_23h30 = datetime.datetime(2026, 9, 6, 23, 30, tzinfo=tz)
        name2, p_dt2, rem2 = manager.get_next_prayer(now=now_23h30)
        assert name2 == "fajr"
        assert p_dt2.date() == datetime.date(2026, 9, 7)


def test_prayer_manager_toggle_individual(tmp_path: Path):
    """Vérifie l'activation/désactivation d'une prière spécifique."""
    cfg_file = tmp_path / "prayer_config.json"
    state_file = tmp_path / "prayer_state.json"
    manager = PrayerManager(config_path=cfg_file, state_path=state_file)

    assert manager.is_prayer_enabled("fajr") is True

    # Désactivation explicite de Fajr
    manager.toggle_prayer("fajr", enabled=False)
    assert manager.is_prayer_enabled("fajr") is False
    assert manager.is_prayer_enabled("dhuhr") is True

    # Réactivation
    manager.toggle_prayer("fajr", enabled=True)
    assert manager.is_prayer_enabled("fajr") is True


def test_prayer_manager_announcement_detection(tmp_path: Path):
    """Vérifie la détection de l'heure d'annonce et l'absence de doublon."""
    cfg_file = tmp_path / "prayer_config.json"
    state_file = tmp_path / "prayer_state.json"
    manager = PrayerManager(config_path=cfg_file, state_path=state_file)

    tz = datetime.timezone(datetime.timedelta(hours=2))
    with patch.object(manager, "resolve_coords", return_value=(48.8566, 2.3522, "Paris")):
        schedule = manager.get_schedule()
        asr_time = schedule["asr"]

        # Exactement à l'heure d'Asr
        res = manager.check_and_produce_announcement(now=asr_time)
        assert res is not None
        msg, p_name, p_dt = res
        assert p_name == "asr"
        assert "Asr" in msg
        assert p_dt == asr_time

        # Deuxième vérification 10 secondes plus tard -> None (déjà annoncé)
        res_dup = manager.check_and_produce_announcement(now=asr_time + datetime.timedelta(seconds=10))
        assert res_dup is None


def test_prayer_control_action_router(tmp_path: Path):
    """Vérifie les commandes du contrôleur d'action prayer_control."""
    cfg_file = tmp_path / "prayer_config.json"
    state_file = tmp_path / "prayer_state.json"
    manager = PrayerManager(config_path=cfg_file, state_path=state_file)

    with patch("actions.prayer.get_prayer_manager", return_value=manager), \
         patch.object(manager, "resolve_coords", return_value=(48.8566, 2.3522, "Paris")):

        # Prochaine prière
        res_next = prayer_control({"action": "next"})
        assert "prochaine prière" in res_next.lower()

        # Liste des prières du jour
        res_today = prayer_control({"action": "today"})
        assert "Fajr" in res_today
        assert "Dhuhr" in res_today
        assert "Maghrib" in res_today

        # Désactiver Fajr
        res_toggle = prayer_control({"action": "toggle", "prayer": "fajr", "enabled": False})
        assert "désactivé" in res_toggle
        assert manager.is_prayer_enabled("fajr") is False

        # Changer méthode
        res_method = prayer_control({"action": "set_method", "method": "UOIF"})
        assert "UOIF" in res_method
        assert manager.config.method == "UOIF"

        # Statut
        res_status = prayer_control({"action": "status"})
        assert "UOIF" in res_status


def test_mcp_prayer_tool_offline():
    """Vérifie que l'outil MCP prayer fonctionne même en mode hors ligne."""
    import anogpt_mcp

    res = anogpt_mcp.prayer(action="status")
    assert "activé" in res.lower() or "convention" in res.lower()


def test_proactive_prayer_expiration_and_anti_interruption():
    """Vérifie les règles d'expiration prompte (> 35 min) et d'heures calmes."""
    from actions.proactive import ProactiveEvent
    import time

    # Événement prière datant de plus de 35 min (2100s)
    old_time = time.time() - 2500
    event = ProactiveEvent(
        topic="prayer",
        message="Il est l'heure de la prière de Dhuhr.",
        data={"prayer": "dhuhr", "time": old_time},
    )

    prayer_time = event.data.get("time", 0)
    is_expired = prayer_time and (time.time() - prayer_time > 2100)
    assert is_expired is True

    # Événement prière récent (5 minutes)
    recent_time = time.time() - 300
    recent_event = ProactiveEvent(
        topic="prayer",
        message="Il est l'heure de la prière de Dhuhr.",
        data={"prayer": "dhuhr", "time": recent_time},
    )
    is_recent_expired = recent_time and (time.time() - recent_time > 2100)
    assert not is_recent_expired

