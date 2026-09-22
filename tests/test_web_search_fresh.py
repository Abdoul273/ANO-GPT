"""Recherche fraîche : un sujet par requête, actualités datées, jamais de faux « pas d'accès »."""
from actions import web_search as ws


def test_list_of_versioned_subjects_is_split():
    assert ws._split_subjects("Grok 4.7, GPT-6 Sol et Opus 5.5") == ["Grok 4.7", "GPT-6 Sol", "Opus 5.5"]
    assert ws._split_subjects("GPT-6 et Opus 5.5") == ["GPT-6", "Opus 5.5"]


def test_titles_with_et_stay_whole():
    assert ws._split_subjects("Tom et Jerry") == ["Tom et Jerry"]
    assert ws._split_subjects("la guerre et la paix") == ["la guerre et la paix"]


def test_explicit_queries_win_over_merged_query():
    merged = "Grok 4.7 GPT-6 Claude 3 Opus 5.5 release news"
    assert ws._clean_subjects(["Grok 4.7", "GPT-6 Sol", "grok 4.7"], merged) == ["Grok 4.7", "GPT-6 Sol"]


def test_version_number_must_match_for_relevance():
    assert ws._relevant("Grok 4.7", "xAI lance Grok 4.7 pour le code")
    assert not ws._relevant("Grok 4.7", "Grok 4.6 disponible pour tous")


def test_each_subject_searched_in_parallel_and_reported(monkeypatch):
    monkeypatch.setattr(ws, "_get_serpapi_api_key", lambda: "k")
    monkeypatch.setattr(ws, "_web_geo", lambda q: {"gl": "fr", "hl": "fr"})
    monkeypatch.setattr(ws, "_news_geo", lambda q: {"gl": "fr", "hl": "fr"})

    def fake_call(params, timeout=None):
        if params["engine"] == "google_news" and params["q"] == "Grok 4.7":
            return {"news_results": [{
                "title": "xAI lance Grok 4.7", "source": {"name": "ZDNET"},
                "link": "https://z.fr/a", "iso_date": "2026-09-22T06:04:18Z",
            }]}
        return {}

    monkeypatch.setattr(ws, "_call_serpapi", fake_call)
    text, card = ws._fresh_search(["Grok 4.7", "Opus 5.5"], "news", budget_s=5)
    assert "xAI lance Grok 4.7 — ZDNET" in text
    assert "▶ Opus 5.5" in text and "rien ne confirme" in text
    assert "pas accès" not in text.split("Consigne")[0]
    assert "[xAI lance Grok 4.7](https://z.fr/a)" in card


def test_runtime_accepts_queries_without_query():
    """Régression : `queries` seul était rejeté (« paramètres requis manquants : query »)."""
    from core.action_runtime import ActionRuntime
    from core.tool_dispatcher import TOOL_DECLARATIONS

    runtime = ActionRuntime(TOOL_DECLARATIONS)
    prepared = runtime.prepare("web_search", {"queries": ["Opus 5.5", "GPT-6 Sol"]})
    assert prepared["queries"] == ["Opus 5.5", "GPT-6 Sol"]
    assert runtime.prepare("web_search", {"mode": "headlines"})["mode"] == "headlines"


def test_version_alone_is_not_relevant():
    assert not ws._relevant("Opus 5.5", "Arcana Unleashed : Les Sorts - DnD 5.5")
    assert ws._relevant("Opus 5.5", "Anthropic dévoile Claude Opus 5.5")
