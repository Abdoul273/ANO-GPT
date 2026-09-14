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


# ── Biais pays au géocodage direct + repli Nominatim ────────────────────────
# « Kaloum » (quartier de Conakry) a déjà renvoyé un village du Niger : nom
# générique, absent d'Open-Meteo même avec un biais pays. Nominatim, plus
# fin (quartiers, communes), le trouve.

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_geocode_sans_biais_utilise_le_premier_resultat_open_meteo(tmp_path):
    cache_file = tmp_path / "geocode_cache.json"
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(url)
        return _FakeResponse({"results": [{"latitude": 9.5, "longitude": -13.7}]})

    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", True), \
         patch.object(geo.requests, "get", side_effect=fake_get):
        assert geo.geocode("Conakry") == (9.5, -13.7)
    assert all("open-meteo" in u for u in calls)


def test_geocode_bascule_sur_nominatim_quand_open_meteo_ne_connait_pas_le_lieu(tmp_path):
    cache_file = tmp_path / "geocode_cache.json"

    def fake_get(url, params=None, **kwargs):
        if "open-meteo" in url:
            return _FakeResponse({"results": []})
        assert params.get("countrycodes") == "gn"
        return _FakeResponse([{"lat": "9.5149641", "lon": "-13.7084413"}])

    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", True), \
         patch.object(geo.requests, "get", side_effect=fake_get):
        coords = geo.geocode("Kaloum", country_code="GN")

    assert coords == (9.5149641, -13.7084413)


def test_geocode_biaise_et_non_biaise_ne_partagent_pas_le_cache(tmp_path):
    """« Kaloum » plein monde (Niger) et « Kaloum, GN » (Conakry) sont deux
    lieux différents : une même clé de cache aurait mélangé les deux."""
    cache_file = tmp_path / "geocode_cache.json"
    cache_file.write_text(
        '{"Kaloum": [13.8254, 6.75676]}', encoding="utf-8",
    )
    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", False):
        # L'appel biaisé ne doit pas lire l'entrée non biaisée en cache.
        assert geo.geocode("Kaloum", country_code="GN") is None
        # L'appel non biaisé, lui, retrouve l'ancienne entrée telle quelle.
        assert geo.geocode("Kaloum") == (13.8254, 6.75676)


# ── Le géocodage inverse rejette une réponse loin du point demandé ─────────

def test_reverse_geocode_rejette_une_reponse_trop_loin(tmp_path):
    cache_file = tmp_path / "geocode_cache.json"

    def fake_get(url, params=None, **kwargs):
        # Nominatim recale sur une feature à ~2000 km du point demandé.
        return _FakeResponse({
            "lat": "13.8", "lon": "6.7",
            "address": {"city": "Un Lieu Très Loin", "country_code": "ne",
                        "country": "Niger"},
        })

    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", True), \
         patch.object(geo.requests, "get", side_effect=fake_get):
        place = geo.reverse_geocode(9.545, -13.678)

    assert place is not None
    assert place["city"] == ""  # pas de faux nom, silence plutôt qu'un mensonge


def test_reverse_geocode_garde_une_reponse_proche(tmp_path):
    cache_file = tmp_path / "geocode_cache.json"

    def fake_get(url, params=None, **kwargs):
        return _FakeResponse({
            "lat": "9.546", "lon": "-13.679",
            "address": {"neighbourhood": "Camayenne", "country_code": "gn",
                        "country": "Guinée"},
        })

    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", True), \
         patch.object(geo.requests, "get", side_effect=fake_get):
        place = geo.reverse_geocode(9.545, -13.678)

    assert place["city"] == "Camayenne"


def test_reverse_geocode_najamais_confiance_eternelle_au_cache(tmp_path):
    """Une entrée écrite avant le TTL (ancien format, sans horodatage) ne
    doit pas rester fausse pour toujours — un mauvais résultat mis en cache
    restait faux indéfiniment avant ce correctif."""
    cache_file = tmp_path / "geocode_cache.json"
    cache_file.write_text(
        '{"rev:9.545,-13.678": {"city": "Bailobaya Centre", '
        '"country_code": "gn", "country_name": "Guinée"}}',
        encoding="utf-8",
    )
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(url)
        return _FakeResponse({
            "lat": "9.546", "lon": "-13.679",
            "address": {"neighbourhood": "Camayenne", "country_code": "gn",
                        "country": "Guinée"},
        })

    with patch.object(geo, "_GEOCODE_CACHE_PATH", cache_file), \
         patch.object(geo, "_REQUESTS", True), \
         patch.object(geo.requests, "get", side_effect=fake_get):
        place = geo.reverse_geocode(9.545, -13.678)

    assert calls, "l'ancienne entrée sans horodatage aurait dû être ignorée"
    assert place["city"] == "Camayenne"
