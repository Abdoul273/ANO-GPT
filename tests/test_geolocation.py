"""Tests de core/geolocation.py — pas de réseau requis (config manuelle
mockée) sauf le test marqué 'network'."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core import geolocation as geo


@pytest.fixture(autouse=True)
def _no_live_gps():
    """Neutralise le GPS du téléphone.

    Ces tests couvrent la chaîne de repli SOUS le relevé GPS. Sans ce masque,
    un `config/live_position.json` laissé par une vraie session faisait
    échouer la suite sur la machine du développeur, alors que le code allait
    parfaitement bien.
    """
    with patch.object(geo, "get_live_position", return_value=None):
        yield


def test_manual_override_wins_over_ip():
    with patch.object(geo, "get_config", return_value={
        "user_country": "gn", "user_country_name": "Guinée", "user_lang": "fr",
    }):
        loc = geo.get_user_location()
    assert loc["country_code"] == "gn"
    assert loc["source"] == "config"
    assert loc["lat"] is None  # pas de coordonnées précises en mode manuel


def test_no_country_is_invented_when_nothing_is_available():
    with patch.object(geo, "get_config", return_value={}), \
         patch.object(geo, "_read_cache", return_value=None), \
         patch.object(geo, "_detect_via_ip", return_value=None):
        loc = geo.get_user_location()
    assert loc["country_code"] == ""
    assert loc["country_name"] == ""
    assert loc["source"] == "default"
    assert loc["source"] == "default"


def test_get_user_coords_uses_ip_lat_lon_when_present():
    with patch.object(geo, "get_config", return_value={}), \
         patch.object(geo, "_read_cache", return_value=None), \
         patch.object(geo, "_detect_via_ip", return_value={
             "country_code": "sn", "country_name": "Senegal", "hl": "fr",
             "city": "Dakar", "lat": 14.6, "lon": -17.4, "source": "ip",
         }), \
         patch.object(geo, "_write_cache"):
        coords = geo.get_user_coords()
    assert coords == (14.6, -17.4)


def test_geocode_cache_roundtrip(tmp_path):
    cache_file = tmp_path / "geocode_cache.json"
    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file):
        # Rien en cache, pas de réseau simulé disponible -> None proprement
        with patch.object(geo, "_REQUESTS", False):
            assert geo.geocode("Nulle Part") is None

        # Une entrée pré-remplie doit être relue sans appel réseau.
        cache_file.write_text('{"Conakry, Guinea": [9.64, -13.58]}', encoding="utf-8")
        assert geo.geocode("Conakry, Guinea") == (9.64, -13.58)
