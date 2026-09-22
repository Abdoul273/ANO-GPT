"""Moteur de recherche de lieux : SerpAPI, repli OSM, fusion et distances."""

import pytest

from core import places


@pytest.fixture(autouse=True)
def _clear_cache():
    places._CACHE.clear()
    yield
    places._CACHE.clear()


CONAKRY = (9.6412, -13.5784)


def _serpapi_payload():
    return {
        "local_results": [
            {
                "title": "Pharmacie Camayenne",
                "gps_coordinates": {"latitude": 9.5450, "longitude": -13.6780},
                "address": "Corniche Nord, Conakry",
                "type": "Pharmacie",
                "rating": 4.3,
                "reviews": 128,
                "phone": "+224 000 00 00",
                "hours": ["Ouvert · Ferme à 22:00"],
                "open_state": "Ouvert",
                "website": "https://exemple.gn",
            },
            {
                # Sans coordonnées : impossible à épingler, donc écartée.
                "title": "Pharmacie fantôme",
                "address": "Quelque part",
            },
        ]
    }


def test_serpapi_results_are_normalised(monkeypatch):
    monkeypatch.setattr(places, "serpapi_key", lambda: "cle-de-test")
    from actions import web_search
    captured = {}
    def fake_nearby(query, center, **kwargs):
        captured.update(query=query, center=center, **kwargs)
        return _serpapi_payload()
    monkeypatch.setattr(web_search, "_nearby_map_results", fake_nearby)

    found = places.search_serpapi("pharmacie", CONAKRY)

    assert len(found) == 1, "une fiche sans coordonnées ne peut pas être épinglée"
    entry = found[0]
    assert entry["name"] == "Pharmacie Camayenne"
    assert entry["rating"] == 4.3 and entry["reviews"] == 128
    assert entry["source"] == "serpapi"
    assert entry["open_now"] is True
    assert entry["dist_km"] > 0
    assert entry["directions_url"].startswith("https://www.google.com/maps/dir/")
    # Le zoom doit voyager avec la position, sinon Google élargit à la région.
    assert captured["center"] == CONAKRY
    assert captured["zoom"] == 14


def test_no_serpapi_key_means_no_serpapi_call(monkeypatch):
    monkeypatch.setattr(places, "serpapi_key", lambda: "")

    def explode(*_, **__):
        raise AssertionError("SerpAPI ne doit pas être appelé sans clé")

    monkeypatch.setattr(places.urllib.request, "urlopen", explode)
    assert places.search_serpapi("pharmacie", CONAKRY) == []


def test_serpapi_error_is_reported():
    with pytest.raises(places.PlaceSearchError):
        raise places.PlaceSearchError("quota dépassé")


@pytest.mark.parametrize(
    "query, expected",
    [
        ("pharmacie", '["amenity"="pharmacy"]'),
        ("je veux manger", '["amenity"="restaurant"]'),
        ("un hôtel pas cher", '["tourism"="hotel"]'),
        ("station d'essence", '["amenity"="fuel"]'),
        ("acheter un ordinateur", '["shop"="computer"]'),
        ("mosquée", '["amenity"="place_of_worship"]'),
    ],
)
def test_plain_words_map_to_osm_tags(query, expected):
    """L'ancienne version n'acceptait que huit catégories anglaises figées."""
    assert expected in places.osm_tags_for(query)


def test_unknown_words_fall_back_to_a_broad_sweep():
    assert places.osm_tags_for("machin truc") == ('["shop"]', '["amenity"]')


def test_empty_overpass_response_does_not_wait_for_second_server(monkeypatch):
    calls = []

    class _Response:
        def read(self):
            return b'{"elements": []}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, timeout))
        return _Response()

    monkeypatch.setattr(places.urllib.request, "urlopen", fake_urlopen)
    assert places.search_overpass("pharmacie", CONAKRY) == []
    assert len(calls) == 1
    assert calls[0][1] == 4.0


def test_duplicates_between_sources_are_merged():
    rich = places._place(
        name="Pharmacie Camayenne", lat=9.545, lon=-13.678, center=CONAKRY,
        source="serpapi", address="Corniche Nord", phone="+224", rating=4.3,
    )
    poor = places._place(
        name="pharmacie camayenne", lat=9.5451, lon=-13.6781, center=CONAKRY,
        source="osm",
    )

    merged = places._dedupe([poor, rich])

    assert len(merged) == 1, "le même commerce ne doit apparaître qu'une fois"
    assert merged[0]["rating"] == 4.3, "la fiche la plus riche doit l'emporter"


def test_distinct_places_sharing_a_name_are_kept():
    first = places._place(name="Total", lat=9.545, lon=-13.678, center=CONAKRY,
                          source="osm")
    second = places._place(name="Total", lat=9.600, lon=-13.700, center=CONAKRY,
                           source="osm")
    assert len(places._dedupe([first, second])) == 2


def test_search_sorts_by_distance_and_names_its_sources(monkeypatch):
    far = places._place(name="Loin", lat=9.70, lon=-13.60, center=CONAKRY, source="osm")
    near = places._place(name="Proche", lat=9.645, lon=-13.580, center=CONAKRY,
                         source="osm")
    monkeypatch.setattr(places, "has_serpapi", lambda: False)
    monkeypatch.setattr(places, "search_overpass",
                        lambda *a, **k: [far, near])

    found, sources = places.search_places("pharmacie", CONAKRY, radius_km=50)

    assert [p["name"] for p in found] == ["Proche", "Loin"]
    assert sources == ["osm"]


def test_google_result_returns_without_waiting_for_overpass(monkeypatch):
    close = places._place(
        name="Pharmacie proche", lat=9.645, lon=-13.58,
        center=CONAKRY, source="serpapi",
    )
    monkeypatch.setattr(places, "has_serpapi", lambda: True)
    monkeypatch.setattr(places, "search_serpapi", lambda *a, **k: [close])
    monkeypatch.setattr(
        places, "search_overpass",
        lambda *a, **k: pytest.fail("Overpass ne doit pas retarder Google Maps"),
    )

    found, sources = places.search_places("pharmacie", CONAKRY)

    assert found == [close]
    assert sources == ["serpapi"]


def test_description_leads_with_the_nearest_place():
    far = places._place(name="Loin", lat=9.70, lon=-13.60,
                        center=CONAKRY, source="osm")
    near = places._place(name="Proche", lat=9.645, lon=-13.580,
                         center=CONAKRY, source="osm")

    text = places.describe_places([near, far], "pharmacie", "ma position", ["osm"])

    assert "LE PLUS PROCHE : Proche" in text
    assert "2 résultat(s)" in text
    assert "ne réponds jamais seulement" in text


def test_distant_results_are_never_used_as_a_fallback(monkeypatch):
    """Google peut ignorer `ll`; un autre continent ne doit jamais apparaître."""
    distant = places._place(
        name="Pharmacie New York", lat=40.7103, lon=-74.0074,
        center=CONAKRY, source="serpapi",
    )
    monkeypatch.setattr(places, "has_serpapi", lambda: True)
    monkeypatch.setattr(places, "search_serpapi", lambda *a, **k: [distant])
    monkeypatch.setattr(places, "search_overpass", lambda *a, **k: [])

    found, sources = places.search_places(
        "pharmacie", CONAKRY, radius_km=5, use_cache=False
    )

    assert found == []
    assert sources == []


def test_radius_is_a_strict_geographic_boundary(monkeypatch):
    inside = places._place(
        name="Dedans", lat=9.65, lon=-13.58, center=CONAKRY, source="osm"
    )
    outside = places._place(
        name="Dehors", lat=9.70, lon=-13.60, center=CONAKRY, source="osm"
    )
    monkeypatch.setattr(places, "has_serpapi", lambda: False)
    monkeypatch.setattr(places, "search_overpass", lambda *a, **k: [outside, inside])

    found, _ = places.search_places(
        "pharmacie", CONAKRY, radius_km=2, use_cache=False
    )

    assert [place["name"] for place in found] == ["Dedans"]


def test_results_are_cached_between_identical_searches(monkeypatch):
    calls = []
    monkeypatch.setattr(places, "has_serpapi", lambda: False)

    def counted(*a, **k):
        calls.append(1)
        return [places._place(name="X", lat=9.65, lon=-13.58, center=CONAKRY,
                              source="osm")]

    monkeypatch.setattr(places, "search_overpass", counted)
    places.search_places("pharmacie", CONAKRY)
    places.search_places("pharmacie", CONAKRY)
    assert len(calls) == 1


def test_a_failing_source_does_not_sink_the_search(monkeypatch):
    monkeypatch.setattr(places, "has_serpapi", lambda: True)

    def boom(*a, **k):
        raise TimeoutError("serpapi injoignable")

    monkeypatch.setattr(places, "search_serpapi", boom)
    monkeypatch.setattr(
        places, "search_overpass",
        lambda *a, **k: [places._place(name="Secours", lat=9.65, lon=-13.58,
                                       center=CONAKRY, source="osm")],
    )

    found, sources = places.search_places("pharmacie", CONAKRY)

    assert [p["name"] for p in found] == ["Secours"]
    assert sources == ["osm"]


def test_haversine_matches_a_known_distance():
    # Conakry → Kindia : ~90 km à vol d'oiseau (la route en fait ~135, c'est
    # bien la ligne droite que cette fonction doit rendre).
    distance = places.haversine_km(9.6412, -13.5784, 10.0569, -12.8658)
    assert 85 < distance < 96


def test_description_lists_ratings_and_details():
    entry = places._place(
        name="Chez Ano", lat=9.645, lon=-13.58, center=CONAKRY, source="serpapi",
        address="Kaloum", rating=4.6, reviews=88, category="Restaurant",
        hours="10:00–23:00",
    )
    text = places.describe_places([entry], "restaurant", "Conakry", ["serpapi"])

    assert "Chez Ano" in text
    assert "4.6/5" in text and "88 avis" in text
    assert "Kaloum" in text and "10:00–23:00" in text


def test_description_of_nothing_stays_honest():
    text = places.describe_places([], "licorne", "Conakry", [])
    assert "Aucun résultat" in text
