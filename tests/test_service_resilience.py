"""Relance des lectures et refroidissement de chaque fournisseur."""

import pytest

from core.service_resilience import ServiceCircuit, ServiceUnavailable, read_with_retry


def test_retry_short_then_success_clears_failure(monkeypatch):
    circuit = ServiceCircuit()
    waits = []
    monkeypatch.setattr("core.service_resilience.time.sleep", waits.append)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("réseau")
        return "ok"

    assert read_with_retry("weather", fetch, transient=lambda e: isinstance(e, TimeoutError),
                           circuit=circuit) == "ok"
    assert calls == 2 and waits == [0.25]
    assert circuit.available("weather")


def test_three_failed_calls_skip_provider_until_five_minutes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("core.service_resilience.time.monotonic", lambda: now[0])
    monkeypatch.setattr("core.service_resilience.time.sleep", lambda _: None)
    circuit = ServiceCircuit()
    calls = 0

    def broken():
        nonlocal calls
        calls += 1
        raise TimeoutError("réseau")

    for _ in range(3):
        with pytest.raises(TimeoutError):
            read_with_retry("google", broken, transient=lambda _: True, circuit=circuit)
    with pytest.raises(ServiceUnavailable):
        read_with_retry("google", broken, transient=lambda _: True, circuit=circuit)
    assert calls == 6
    assert circuit.available("wttr")
    now[0] += 301
    assert circuit.available("google")


def test_permanent_error_is_not_retried_or_counted(monkeypatch):
    circuit = ServiceCircuit()
    monkeypatch.setattr("core.service_resilience.time.sleep", lambda _: pytest.fail("attente injustifiée"))
    for _ in range(4):
        with pytest.raises(ValueError):
            read_with_retry("google", lambda: (_ for _ in ()).throw(ValueError("clé absente")),
                            transient=lambda _: False, circuit=circuit)
    assert circuit.available("google")


def test_serpapi_429_is_retried_once(monkeypatch):
    from actions import web_search as ws
    from core.service_resilience import SERVICES

    SERVICES._state.clear()
    monkeypatch.setattr("core.service_resilience.time.sleep", lambda _: None)
    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "test-key")
    calls = []

    class Response:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code == 429:
                import requests
                error = requests.HTTPError("429 Too Many Requests")
                error.response = self
                raise error

        def json(self):
            return {"organic_results": [{"title": "source"}]}

    class Http:
        def get(self, _url, **kwargs):
            calls.append(kwargs)
            return Response(429 if len(calls) == 1 else 200)

    monkeypatch.setattr(ws.kit, "http", lambda: Http())
    try:
        result = ws._call_serpapi({"q": "test", "hl": "fr", "gl": "gn"})
    finally:
        SERVICES._state.clear()
    assert result["organic_results"][0]["title"] == "source"
    assert len(calls) == 2


def test_serpapi_accepts_connect_and_read_timeout_pair(monkeypatch):
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "test-key")
    seen = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Http:
        def get(self, _url, **kwargs):
            seen.append(kwargs["timeout"])
            return Response()

    monkeypatch.setattr(ws.kit, "http", lambda: Http())
    ws._call_serpapi({"q": "test", "hl": "fr", "gl": "gn"}, timeout=(3.0, 7.0))
    assert seen == [(3.0, 7.0)]


def test_web_search_falls_back_to_gemini_grounding(monkeypatch):
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "configured")
    monkeypatch.setattr(ws, "_get_api_key", lambda: "configured")
    monkeypatch.setattr(ws, "_fetch_web", lambda _subject: (_ for _ in ()).throw(TimeoutError("Google")))
    monkeypatch.setattr(ws, "_run_with_timeout", lambda fn, query, **kwargs: "réponse Gemini sourcée")
    monkeypatch.setattr(ws, "_DDGS_AVAILABLE", False)

    text, card = ws._fresh_search(["sujet"], "search")

    assert text == "réponse Gemini sourcée"
    assert card == ""


def test_weather_open_meteo_circuit_uses_wttr_without_network_wait(monkeypatch):
    import time
    from actions import weather_report as weather
    from core.service_resilience import SERVICES

    SERVICES._state["open-meteo"] = (3, time.monotonic() + 300)
    monkeypatch.setattr(weather, "_geocode_city", lambda _city: (88.0, 77.0, "Conakry", "Guinée"))
    monkeypatch.setattr(weather.urllib.request, "urlopen",
                        lambda *_a, **_k: pytest.fail("Open-Meteo aurait dû être sauté"))
    monkeypatch.setattr(weather, "_fetch_wttr", lambda _city: {"ok": True})
    monkeypatch.setattr(weather, "_format_wttr", lambda *_a: "prévision de secours")
    try:
        result = weather.weather_action({"city": "Conakry"})
    finally:
        SERVICES._state.clear()
    assert result == "prévision de secours"


def test_web_nearby_uses_openstreetmap_when_google_is_down(monkeypatch):
    from actions import web_search as ws
    from core import places

    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "key")
    monkeypatch.setattr(ws, "_user_coords", lambda: (9.6, -13.6))
    monkeypatch.setattr(ws, "_serpapi_nearby", lambda _query: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(places, "search_overpass", lambda *_a, **_k: [
        {"name": "Pharmacie du quartier", "dist_km": 0.4, "lat": 9.601, "lon": -13.601},
    ])

    result = ws.web_search({"mode": "nearby", "query": "pharmacie"})

    assert "Pharmacie du quartier" in result
    assert "0.4 km" in result


def test_web_price_uses_gemini_if_google_is_down(monkeypatch):
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "key")
    monkeypatch.setattr(ws, "_get_api_key", lambda: "key")
    monkeypatch.setattr(ws, "_serpapi_price", lambda _query: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(ws, "_gemini_search", lambda _query: "prix sourcé")

    assert ws.web_search({"mode": "price", "query": "PS5"}) == "prix sourcé"
