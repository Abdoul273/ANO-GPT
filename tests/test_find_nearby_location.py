"""Régression : la recherche de proximité doit suivre la vraie position.

Bug corrigé ici : `find_nearby` faisait `from core.geolocation import
get_location`, un nom qui n'a jamais existé dans ce module. L'ImportError était
rattrapée par un stub codé en dur sur Paris — la recherche tournait donc
TOUJOURS sur Paris, quelle que soit la position réelle de l'utilisateur.

Ces tests n'utilisent pas le réseau : la géolocalisation est simulée.
"""

import pytest

import actions.find_nearby as fn
import core.geolocation as geo


def test_les_noms_importes_existent_vraiment():
    """Le piège d'origine : un import qui échoue en silence.

    Si ces noms disparaissent, l'import de `find_nearby` doit casser bruyamment
    plutôt que retomber sur une position par défaut.
    """
    assert hasattr(geo, "get_user_location")
    assert hasattr(geo, "get_user_coords")


def test_position_reelle_utilisee(monkeypatch):
    # Sans téléphone appairé, il n'y a pas de relevé GPS : c'est la position
    # approximative qui doit servir, et surtout pas un repli codé en dur.
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: None)
    monkeypatch.setattr(fn, "get_user_coords", lambda: (9.53795, -13.67729))
    monkeypatch.setattr(
        fn, "get_user_location",
        lambda: {"city": "Conakry", "country_name": "Guinée"},
    )

    loc = fn.get_location()

    assert loc["city"] == "Conakry"
    assert loc["latitude"] == pytest.approx(9.53795)
    assert loc["longitude"] == pytest.approx(-13.67729)
    # Le repli historique, qu'on ne veut plus jamais voir.
    assert loc["latitude"] != pytest.approx(48.8566, abs=0.01)


def test_repli_sur_le_pays_quand_la_ville_est_vide(monkeypatch):
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: None)
    monkeypatch.setattr(fn, "get_user_coords", lambda: (10.83, -10.66))
    monkeypatch.setattr(
        fn, "get_user_location",
        lambda: {"city": "", "country_name": "Guinée"},
    )

    assert fn.get_location()["city"] == "Guinée"


def test_position_introuvable_leve_plutot_que_de_deviner(monkeypatch):
    """Une position fausse est pire qu'une erreur : elle renvoie des commerces
    à des milliers de kilomètres sans que rien ne le signale."""
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: None)
    monkeypatch.setattr(fn, "get_user_coords", lambda: None)
    monkeypatch.setattr(fn, "get_user_location", lambda: {"city": ""})

    with pytest.raises(RuntimeError):
        fn.get_location()


def test_lappelant_remonte_lerreur_au_lieu_de_chercher_a_paris(monkeypatch):
    def _boom():
        raise RuntimeError("pas de position")

    monkeypatch.setattr(fn, "get_location", _boom)
    # Aucune source ne doit être interrogée sans position valide.
    monkeypatch.setattr(
        fn, "search_places",
        lambda *a, **k: pytest.fail("recherche lancée sans position valide"),
    )

    out = fn.find_nearby({"category": "pharmacy"})

    assert "position" in out.lower()


def test_un_geocodage_inverse_lent_ne_retient_pas_la_recherche(monkeypatch):
    """Avec un GPS précis, le nom de ville n'est qu'un libellé : il ne doit
    pas coûter cinq secondes de silence avant même de chercher."""
    import threading
    import time

    release = threading.Event()

    def _slow_location():
        release.wait(5)
        return {"city": "Kaloum"}

    monkeypatch.setattr(fn, "_CITY_WAIT_S", 0.05)
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: (9.6001, -13.6002))
    monkeypatch.setattr(fn, "get_user_location", _slow_location)

    start = time.monotonic()
    loc = fn.get_location()
    release.set()

    assert time.monotonic() - start < 1.0
    assert loc["latitude"] == pytest.approx(9.6001)
    assert loc["city"] == "votre position"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_le_releve_gps_frais_prime_sur_la_position_approximative(monkeypatch):
    """Une pharmacie annoncée à cent cinquante mètres n'a de sens que mesurée
    depuis l'endroit où l'on se tient vraiment, pas depuis une position IP."""
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: (9.6001, -13.6002))
    monkeypatch.setattr(fn, "get_user_coords", lambda: (9.53795, -13.67729))
    monkeypatch.setattr(
        fn, "get_user_location",
        lambda: {"city": "Conakry", "country_name": "Guinée"},
    )

    loc = fn.get_location()

    assert loc["latitude"] == pytest.approx(9.6001)
    assert loc["longitude"] == pytest.approx(-13.6002)


def test_mode_gps_strict_refuse_toute_position_de_repli(monkeypatch):
    monkeypatch.setattr(fn, "get_precise_user_coords", lambda: None)
    monkeypatch.setattr(fn, "get_user_coords", lambda: (9.53795, -13.67729))
    monkeypatch.setattr(
        fn, "get_user_location",
        lambda: {"city": "Conakry", "country_name": "Guinée"},
    )

    with pytest.raises(RuntimeError, match="ANO Remote"):
        fn.get_location(require_precise_gps=True)
