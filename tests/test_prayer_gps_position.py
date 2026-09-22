"""Les heures de prière suivent le GPS du téléphone, jamais une ville figée ni Paris."""
import datetime
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core import geolocation as geo
from core.prayer_times import PrayerManager, calculate_prayer_times


def _manager(tmp_path: Path) -> PrayerManager:
    return PrayerManager(config_path=tmp_path / "cfg.json", state_path=tmp_path / "state.json")


def _live(tmp_path, monkeypatch, lat, lon, age_s=0.0):
    path = tmp_path / "live_position.json"
    import time
    path.write_text(json.dumps({"lat": lat, "lon": lon, "accuracy_m": 50.0,
                                "source": "phone-gps", "_at": time.time() - age_s}))
    monkeypatch.setattr(geo, "_LIVE_PATH", path)


def test_gps_frais_prime_sur_la_ville_configuree(tmp_path, monkeypatch):
    _live(tmp_path, monkeypatch, 10.05, -12.87)  # Kindia, pas Conakry
    monkeypatch.setattr(geo, "reverse_geocode", lambda lat, lon: {"city": "Kindia", "country_name": "Guinée"})
    m = _manager(tmp_path)
    lat, lon, city = m.resolve_coords()
    assert (lat, lon) == (10.05, -12.87)
    assert city == "Kindia"
    assert "GPS du téléphone" in m.position_source
    # Persisté pour le prochain démarrage
    saved = json.loads((tmp_path / "cfg.json").read_text())
    assert saved["last_lat"] == 10.05 and saved["last_city"] == "Kindia"


def test_gps_perime_reste_prefere_au_centroide(tmp_path, monkeypatch):
    _live(tmp_path, monkeypatch, 10.38, -9.30, age_s=5 * 3600)  # Kankan, il y a 5 h
    monkeypatch.setattr(geo, "reverse_geocode", lambda lat, lon: {"city": "Kankan"})
    m = _manager(tmp_path)
    lat, lon, _city = m.resolve_coords()
    assert (lat, lon) == (10.38, -9.30)
    assert "il y a 300 min" in m.position_source


def test_sans_gps_repli_sur_derniere_position_puis_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_LIVE_PATH", tmp_path / "absent.json")
    m = _manager(tmp_path)
    m.config.last_lat, m.config.last_lon, m.config.last_city = 9.68, -13.52, "Bailobaya"
    assert m.resolve_coords() == (9.68, -13.52, "Bailobaya")

    m2 = _manager(tmp_path / "vide")
    with patch("core.geolocation.get_user_location", return_value={"city": "", "country_name": "", "lat": None, "lon": None}), \
         patch("core.geolocation.get_user_coords", return_value=None):
        with pytest.raises(RuntimeError, match="Position inconnue"):
            m2.resolve_coords()
        # La veille ne plante pas, elle n'annonce rien
        assert m2.check_and_produce_announcement() is None


def test_horaires_recalcules_quand_on_se_deplace(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "reverse_geocode", lambda lat, lon: {"city": "x"})
    m = _manager(tmp_path)
    now = datetime.datetime(2026, 9, 22, 8, 0, tzinfo=datetime.timezone.utc)

    _live(tmp_path, monkeypatch, 9.537, -13.678)  # Conakry
    conakry = m.get_schedule(now_ref=now)
    _live(tmp_path, monkeypatch, 10.38, -9.30)  # Kankan, ~480 km à l'est
    kankan = m.get_schedule(now_ref=now)

    diff_min = (conakry["maghrib"] - kankan["maghrib"]).total_seconds() / 60
    assert 14 <= diff_min <= 22  # ~4.4° de longitude = ~17 min plus tôt à l'est


def test_conakry_mwl_correspond_aux_tables_publiees():
    s = calculate_prayer_times(9.537, -13.678, datetime.date(2026, 9, 22), 0.0, "MWL")
    got = {k: v.strftime("%H:%M") for k, v in s.items()}
    expected = {"fajr": "05:34", "sunrise": "06:44", "dhuhr": "12:48",
                "asr": "16:02", "maghrib": "18:52", "isha": "19:57"}
    for k, v in expected.items():
        gh, gm = map(int, got[k].split(":"))
        eh, em = map(int, v.split(":"))
        assert abs((gh * 60 + gm) - (eh * 60 + em)) <= 2, (k, got[k], v)
