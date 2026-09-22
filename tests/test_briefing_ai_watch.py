"""Veille IA du briefing : modèles, Google/Gemini, OpenAI/ChatGPT, Anthropic/Claude."""
import asyncio
from datetime import date, datetime

import core.daily_briefing as db_mod
from core.daily_briefing import (
    AI_WATCH_NOTHING_NEW,
    BriefingData,
    build_ai_watch_query,
    format_briefing_card,
    format_briefing_prompt,
    parse_ai_watch,
)


def test_query_couvre_les_quatre_axes_et_la_fenetre():
    query = build_ai_watch_query(date(2026, 9, 22)).casefold()
    for axe in ("modeles", "google", "openai", "anthropic"):
        assert f'"{axe}"' in query
    assert "chatgpt" in query and "claude" in query and "gemini" in query
    assert "2026-09-15" in query  # 7 jours en arrière
    assert AI_WATCH_NOTHING_NEW.casefold() in query


def test_parse_json_meme_entoure_de_texte():
    raw = (
        "Voici la veille :\n```json\n"
        '{"modeles": "Gemini 3 sorti le 20 septembre.", "google": "Gemini app mise à jour.", '
        '"openai": "Rien de neuf vérifié.", "anthropic": ["Claude Code v2", "le 19 septembre."]}'
        "\n```"
    )
    parsed = parse_ai_watch(raw)
    assert parsed["modeles"] == "Gemini 3 sorti le 20 septembre."
    assert parsed["openai"] == AI_WATCH_NOTHING_NEW
    assert parsed["anthropic"] == "Claude Code v2 le 19 septembre."


def test_parse_repli_texte_libre_et_vide():
    parsed = parse_ai_watch("modeles : Llama 5 publié.\nopenai: GPT-6 dispo.")
    assert parsed["modeles"] == "Llama 5 publié."
    assert parsed["openai"] == "GPT-6 dispo."
    assert parsed["google"] == AI_WATCH_NOTHING_NEW
    assert parse_ai_watch("") == db_mod.empty_ai_watch()


def test_prompt_et_carte_incluent_la_veille_ia():
    data = BriefingData(
        timestamp=datetime(2026, 9, 22, 8, 0),
        ai_watch={
            "modeles": "Mistral Large 4 sorti le 21 septembre.",
            "google": AI_WATCH_NOTHING_NEW,
            "openai": "ChatGPT gagne un mode agent le 20 septembre.",
            "anthropic": "Claude Opus 5 disponible via l'API le 18 septembre.",
        },
    )
    prompt = format_briefing_prompt(data)
    assert "Veille IA" in prompt
    assert "Mistral Large 4" in prompt
    assert "Anthropic / Claude : Claude Opus 5" in prompt
    assert "nouveaux modèles sortis" in prompt
    assert "annonce concrètement ce qui est nouveau" in prompt

    _title, card = format_briefing_card(data)
    assert "🤖 Veille IA" in card
    assert "OpenAI / ChatGPT : ChatGPT gagne un mode agent" in card


def test_prompt_sans_nouveaute_le_dit_sans_inventer():
    prompt = format_briefing_prompt(BriefingData(timestamp=datetime(2026, 9, 22, 8, 0)))
    assert "aucune sortie de modèle" in prompt
    assert "jamais de nouveauté inventée" in prompt


def test_fetch_ai_watch_retombe_proprement(monkeypatch):
    import actions.web_search as ws

    def boom(_query):
        raise TimeoutError("gemini trop lent")

    monkeypatch.setattr(ws, "_gemini_search", boom)
    assert asyncio.run(db_mod._fetch_ai_watch()) == db_mod.empty_ai_watch()

    monkeypatch.setattr(ws, "_gemini_search", lambda q: '{"google": "Gemini API : nouveau modèle."}')
    parsed = asyncio.run(db_mod._fetch_ai_watch())
    assert parsed["google"] == "Gemini API : nouveau modèle."
    assert parsed["modeles"] == AI_WATCH_NOTHING_NEW
