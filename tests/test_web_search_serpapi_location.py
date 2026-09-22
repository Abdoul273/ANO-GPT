"""SerpApi refuse les lieux hors de sa base canonique : repli sans échec."""
from unittest.mock import patch

from actions import web_search as ws


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            exc = requests.HTTPError(
                f"{self.status_code} Client Error: Bad Request for url: "
                "https://serpapi.com/search.json?q=x&api_key=SECRET123"
            )
            exc.response = self
            raise exc

    def json(self):
        return self._payload


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, _url, params=None, timeout=None):
        self.calls.append(dict(params))
        return self.responses.pop(0)


def test_location_fallbacks_chain():
    assert ws._location_fallbacks("Bailobaya Centre, Guinea") == ["Bailobaya Centre, Guinea", "Guinea", None]
    assert ws._location_fallbacks("Guinea") == ["Guinea", None]
    assert ws._location_fallbacks(None) == [None]


def test_unknown_village_degrades_to_country_then_none(monkeypatch):
    http = _Http([_Resp(400), _Resp(400), _Resp(200, {"organic_results": [{"title": "ok"}]})])
    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "k")
    monkeypatch.setattr(ws.kit, "http", lambda: http)

    data = ws._call_serpapi({"q": "stations", "engine": "google", "gl": "gn", "hl": "fr",
                             "location": "Bailobaya Centre, Guinea"})

    assert data["organic_results"][0]["title"] == "ok"
    assert [c.get("location") for c in http.calls] == ["Bailobaya Centre, Guinea", "Guinea", None]


def test_error_never_leaks_api_key(monkeypatch):
    http = _Http([_Resp(400)])
    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "k")
    monkeypatch.setattr(ws.kit, "http", lambda: http)

    try:
        ws._call_serpapi({"q": "x", "engine": "google", "gl": "gn", "hl": "fr"})
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("un 400 final doit lever")
    assert "SECRET123" not in message
    assert "api_key=" not in message
    assert "400" in message


def test_nearby_uses_gps_coordinates_first(monkeypatch):
    calls = []

    def fake_call(params):
        calls.append(params)
        return {"local_results": [{"title": "Station Total", "address": "Bailobaya",
                                   "gps_coordinates": {"latitude": 9.79, "longitude": -13.31}}]}

    monkeypatch.setattr(ws, "_call_serpapi", fake_call)
    with patch("core.geolocation.get_user_location", return_value={
        "country_code": "gn", "hl": "fr", "city": "Bailobaya Centre", "country_name": "Guinée",
        "lat": 9.7921, "lon": -13.3185, "source": "phone-gps",
    }):
        result = ws._serpapi_nearby("station")

    assert calls[0]["engine"] == "google_maps"
    assert calls[0]["ll"].startswith("@9.792100,-13.318500")
    assert "location" not in calls[0]
    assert "Station Total" in result
    assert "9.79, -13.31" in result
