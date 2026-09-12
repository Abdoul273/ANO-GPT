"""L'outil find_nearby : où l'on cherche, et où les résultats s'affichent."""

import pytest

from actions import find_nearby as module

CONAKRY = (9.6412, -13.5784)


class _UI:
    """Interface factice : retient ce qui a été envoyé à la carte."""

    def __init__(self):
        self.map_calls: list[tuple] = []

    def show_nearby_map(self, query, lat, lon, places):
        self.map_calls.append((query, lat, lon, places))


@pytest.fixture
def ui():
    return _UI()


@pytest.fixture(autouse=True)
def _here(monkeypatch):
    monkeypatch.setattr(
        module, "get_location",
        lambda: {"latitude": CONAKRY[0], "longitude": CONAKRY[1], "city": "Conakry"},
    )


def _found(monkeypatch, places, sources=("serpapi",)):
    monkeypatch.setattr(module, "search_places",
                        lambda *a, **k: (places, list(sources)))


def _place(name="Pharmacie Camayenne", dist=1.2):
    return {
        "name": name, "lat": 9.545, "lon": -13.678, "dist_km": dist,
        "address": "Corniche Nord", "category": "Pharmacie", "rating": 4.3,
        "reviews": 128, "phone": "", "opening_hours": "", "website": "",
        "price": "", "open_now": None, "source": "serpapi",
        "directions_url": "https://maps.example/dir",
    }


def test_results_are_pinned_on_the_large_map(monkeypatch, ui):
    _found(monkeypatch, [_place(), _place("Pharmacie du Port", 2.0)])

    text = module.find_nearby({"query": "pharmacie"}, ui=ui)

    assert len(ui.map_calls) == 1, "les lieux doivent partir vers la carte unique"
    query, lat, lon, places = ui.map_calls[0]
    assert query == "pharmacie"
    assert (lat, lon) == CONAKRY
    assert len(places) == 2
    assert "Pharmacie Camayenne" in text


def test_searching_around_a_named_place_moves_the_centre(monkeypatch, ui):
    monkeypatch.setattr(module, "geocode", lambda name: (10.0569, -12.8658))
    captured = {}

    def spy(terms, center, **kwargs):
        captured["center"] = center
        return [_place()], ["osm"]

    monkeypatch.setattr(module, "search_places", spy)
    module.find_nearby({"query": "hôtel", "near": "Kindia"}, ui=ui)

    assert captured["center"] == (10.0569, -12.8658)
    assert ui.map_calls[0][1] == 10.0569


def test_an_unknown_place_is_reported(monkeypatch, ui):
    monkeypatch.setattr(module, "geocode", lambda name: None)
    text = module.find_nearby({"query": "hôtel", "near": "Atlantide"}, ui=ui)

    assert "Atlantide" in text
    assert not ui.map_calls


def test_no_result_suggests_a_serpapi_key_when_missing(monkeypatch, ui):
    _found(monkeypatch, [], sources=[])
    monkeypatch.setattr(module, "has_serpapi", lambda: False)

    text = module.find_nearby({"query": "licorne"}, ui=ui)

    assert "Aucun résultat" in text
    # Sans clé on n'a qu'OpenStreetMap, très clairsemé hors d'Europe : le dire
    # vaut mieux que laisser croire que le lieu n'existe pas.
    assert "serpapi_api_key" in text
    assert not ui.map_calls


def test_no_result_with_a_key_does_not_nag(monkeypatch, ui):
    _found(monkeypatch, [], sources=[])
    monkeypatch.setattr(module, "has_serpapi", lambda: True)

    text = module.find_nearby({"query": "licorne"}, ui=ui)
    assert "serpapi_api_key" not in text


def test_an_empty_query_asks_for_one(ui):
    assert "Précisez" in module.find_nearby({}, ui=ui)


def test_a_broken_map_never_loses_the_answer(monkeypatch):
    class _Broken:
        def show_nearby_map(self, *_):
            raise RuntimeError("WebEngine absent")

    _found(monkeypatch, [_place()])
    text = module.find_nearby({"query": "pharmacie"}, ui=_Broken())

    assert "Pharmacie Camayenne" in text, "le texte doit survivre à une carte en panne"


def test_a_search_failure_is_explained(monkeypatch, ui):
    def boom(*a, **k):
        raise RuntimeError("réseau coupé")

    monkeypatch.setattr(module, "search_places", boom)
    text = module.find_nearby({"query": "pharmacie"}, ui=ui)

    assert "a échoué" in text and "réseau coupé" in text


def test_category_still_works_for_older_calls(monkeypatch, ui):
    """Le modèle peut encore envoyer l'ancien paramètre `category`."""
    _found(monkeypatch, [_place()])
    text = module.find_nearby({"category": "pharmacy"}, ui=ui)
    assert "Pharmacie Camayenne" in text
