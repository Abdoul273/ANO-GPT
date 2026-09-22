from actions import web_search


def test_daily_news_request_defaults_to_ai_and_cyber():
    parsed = web_search._parse_search_request_locally("donne-moi les actualités du jour")

    assert parsed == {"mode": "news", "query": ""}
    topic = web_search.DAILY_AI_CYBER_NEWS_QUERY.casefold()
    assert "intelligence artificielle" in topic
    assert "cybersécurité" in topic
    assert "cyberattaques" in topic


def test_empty_news_query_uses_focused_grounded_prompt(monkeypatch):
    gemini_calls = []
    monkeypatch.setattr(
        web_search, "_gemini_search",
        lambda query: gemini_calls.append(query) or ("Actualité IA vérifiée. " * 5),
    )

    response = web_search._news("")

    assert "Actualité IA" in response
    prompt = gemini_calls[0].casefold()
    assert "intelligence artificielle" in prompt
    assert "cybersécurité" in prompt


def test_daily_headlines_label_and_fallback_are_ai_cyber(monkeypatch):
    monkeypatch.setattr(
        web_search, "_gemini_headlines",
        lambda count: (["Une avancée majeure en intelligence artificielle"], ""),
    )

    response = web_search._headlines(5)

    assert "Actualités IA & cyber du jour" in response
    assert "intelligence artificielle" in response
