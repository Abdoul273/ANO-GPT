"""web_search rend toujours la main avant le répartiteur (15 s) : chaque
appel réseau est borné par le temps qui reste, jamais par son propre délai."""
import time

from actions import web_search as ws


def test_serpapi_timeout_shrinks_to_the_remaining_budget(monkeypatch):
    seen = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"organic_results": []}

    class _Http:
        def get(self, url, params=None, timeout=None):
            seen.append(timeout)
            return _Resp()

    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "cle")
    monkeypatch.setattr(ws.kit, "http", lambda: _Http())
    token = ws._deadline.set(time.monotonic() + 2.0)
    try:
        ws._call_serpapi({"q": "x", "hl": "fr", "gl": "gn"})
    finally:
        ws._deadline.reset(token)

    assert seen and seen[0] <= 2.0


def test_no_call_starts_once_the_budget_is_spent(monkeypatch):
    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "cle")
    monkeypatch.setattr(ws.kit, "http", lambda: (_ for _ in ()).throw(AssertionError("appel lancé")))
    token = ws._deadline.set(time.monotonic() + 0.1)
    try:
        try:
            ws._call_serpapi({"q": "x", "hl": "fr", "gl": "gn"})
        except TimeoutError:
            pass
        else:
            raise AssertionError("un appel hors budget aurait dû être refusé")
    finally:
        ws._deadline.reset(token)


def test_slow_intent_analysis_falls_back_to_a_plain_search(monkeypatch):
    monkeypatch.setattr(ws, "_INTENT_TIMEOUT", 0.1)
    monkeypatch.setattr(ws, "_parse_search_request_locally", lambda _t: None)
    monkeypatch.setattr(ws, "_detect_search_intent_ai", lambda _t: time.sleep(3) or {"mode": "price"})
    captured = {}

    def _fake_fresh(subjects, mode, budget_s):
        captured.update(subjects=subjects, mode=mode, budget=budget_s)
        return "résultats", ""

    monkeypatch.setattr(ws, "_fresh_search", _fake_fresh)

    started = time.monotonic()
    out = ws.web_search({"description": "qui a gagné le match hier", "_budget_s": 10})

    assert out == "résultats"
    assert time.monotonic() - started < 1.0
    assert captured["mode"] == "search"
    assert captured["budget"] <= 10


def test_the_deadline_does_not_leak_to_the_next_caller(monkeypatch):
    monkeypatch.setattr(ws, "_fresh_search", lambda *a: ("ok", ""))
    ws.web_search({"query": "météo", "_budget_s": 0.01})
    assert ws._deadline.get() is None
