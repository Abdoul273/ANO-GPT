"""Position live : le GPS du téléphone doit primer sur toute valeur figée.

L'utilisateur se déplace entre plusieurs localités de la région de Conakry
(T10, Kouria, Keitaya). Une ville configurée en dur renvoyait des résultats à
des dizaines de kilomètres sans rien signaler ; le PC n'ayant ni GPS ni modem,
la seule source précise est le téléphone, qui pousse sa position au dashboard.

Ces tests n'utilisent pas le réseau : le géocodage inverse est simulé.
"""

import json
import time

import pytest

import core.geolocation as geo


@pytest.fixture
def live_path(tmp_path, monkeypatch):
    """Isole le fichier de position pour ne pas toucher la vraie config."""
    p = tmp_path / "live_position.json"
    monkeypatch.setattr(geo, "_LIVE_PATH", p)
    monkeypatch.setattr(geo, "reverse_geocode",
                        lambda lat, lon: {"city": "Kipé",
                                          "country_code": "gn",
                                          "country_name": "Guinée"})
    return p


def test_le_gps_prime_sur_la_ville_configuree(live_path):
    geo.set_live_position(9.6412, -13.6200, accuracy_m=12.0)

    loc = geo.get_user_location()

    assert loc["source"] == "phone-gps"
    assert loc["lat"] == pytest.approx(9.6412)
    assert loc["lon"] == pytest.approx(-13.6200)
    assert loc["accuracy_m"] == 12.0
    assert geo.get_user_coords() == pytest.approx((9.6412, -13.6200))


def test_le_quartier_est_nomme(live_path):
    geo.set_live_position(9.6412, -13.6200)
    assert geo.get_user_location()["city"] == "Kipé"


def test_position_precise_ne_retombe_jamais_sur_ip(live_path, monkeypatch):
    monkeypatch.setattr(geo, "get_user_location", lambda: {
        "lat": 9.51, "lon": -13.71, "source": "ip", "city": "Kaloum",
    })
    assert geo.get_precise_user_coords() is None


def test_position_gps_trop_imprecise_est_refusee(live_path):
    geo.set_live_position(9.6412, -13.6200, accuracy_m=1200)
    assert geo.get_precise_user_coords(max_accuracy_m=500) is None


def test_position_gps_precise_est_acceptee(live_path):
    geo.set_live_position(9.6412, -13.6200, accuracy_m=18)
    assert geo.get_precise_user_coords() == pytest.approx((9.6412, -13.6200))


def test_une_position_perimee_est_ignoree(live_path):
    """Mieux vaut retomber sur une source vague que d'affirmer une position
    précise mais vieille de plusieurs heures — l'utilisateur s'est déplacé."""
    geo.set_live_position(9.6412, -13.6200)
    d = json.loads(live_path.read_text(encoding="utf-8"))
    d["_at"] = time.time() - geo._LIVE_TTL - 1
    live_path.write_text(json.dumps(d), encoding="utf-8")

    assert geo.get_live_position() is None
    assert geo.get_user_location()["source"] != "phone-gps"


def test_absence_de_releve_ne_casse_rien(live_path):
    assert geo.get_live_position() is None
    assert geo.get_user_location()["source"] != "phone-gps"


@pytest.mark.parametrize("lat,lon", [(91.0, 0.0), (-91.0, 0.0),
                                     (0.0, 181.0), (0.0, -181.0)])
def test_coordonnees_hors_bornes_refusees(live_path, lat, lon):
    """L'endpoint est exposé au réseau : il ne doit pas stocker n'importe quoi."""
    with pytest.raises(ValueError):
        geo.set_live_position(lat, lon)


def test_coordonnees_non_numeriques_refusees(live_path):
    with pytest.raises((ValueError, TypeError)):
        geo.set_live_position("ici", 0.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
