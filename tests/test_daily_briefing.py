"""Tests pour le briefing quotidien parlé (core/daily_briefing.py)."""

import asyncio
from datetime import date, datetime

from core.daily_briefing import (
    BriefingData,
    format_briefing_card,
    format_briefing_prompt,
    is_briefing_due_today,
    is_explicit_briefing_request,
    is_greeting,
    mark_briefing_delivered,
    should_trigger_daily_briefing,
    collect_briefing_data,
)


def test_greeting_detection():
    assert is_greeting("Bonjour") is True
    assert is_greeting("bonjour ano") is True
    assert is_greeting("Salut JARVIS !") is True
    assert is_greeting("bon matin") is True
    assert is_greeting("hello") is True
    assert is_greeting("ouvre firefox") is False
    assert is_greeting("quelle est la météo") is False


def test_explicit_briefing_request():
    assert is_explicit_briefing_request("Donne-moi le briefing") is True
    assert is_explicit_briefing_request("briefing du jour") is True
    assert is_explicit_briefing_request("quel est le programme aujourd'hui ?") is True
    assert is_explicit_briefing_request("fais-moi le point") is True
    assert is_explicit_briefing_request("mets de la musique") is False


def test_briefing_due_today_and_delivered(tmp_path, monkeypatch):
    import core.daily_briefing as db_mod
    monkeypatch.setattr(db_mod, "STATE_FILE", tmp_path / "daily_briefing_state.json")

    d1 = date(2026, 8, 16)
    d2 = date(2026, 8, 17)

    # 1. Non délivré aujourd'hui
    assert is_briefing_due_today(d1) is True

    # 2. Déclenchement sur « Bonjour »
    assert should_trigger_daily_briefing("Bonjour ANO", today=d1) is True

    # 3. Marqué comme délivré
    mark_briefing_delivered(d1)
    assert is_briefing_due_today(d1) is False

    # 4. Deuxième bonjour de la même journée -> ne redéclenche pas le briefing complet
    assert should_trigger_daily_briefing("Bonjour ANO", today=d1) is False

    # 5. Mais une demande explicite redéclenche même le même jour
    assert should_trigger_daily_briefing("Donne-moi le briefing", today=d1) is True

    # 6. Le jour suivant -> redéclenche à nouveau
    assert is_briefing_due_today(d2) is True
    assert should_trigger_daily_briefing("Bonjour ANO", today=d2) is True


def test_format_briefing_prompt_and_card():
    data = BriefingData(
        timestamp=datetime(2026, 8, 16, 8, 30),
        user_name="Anonymous",
        weather="Ensoleillé, 22°C",
        location="Paris (France)",
        emails="2 e-mails non lus",
        reminders="1 rappel à 14h",
        news=["Titre 1 Tech", "Titre 2 Cyber"],
        system="Batterie 100%, 8 Go RAM",
    )

    prompt = format_briefing_prompt(data)
    assert "Ensoleillé, 22°C" in prompt
    assert "2 e-mails non lus" in prompt
    assert "Titre 1 Tech" in prompt
    assert "30 SECONDES CHRONO" in prompt

    title, card = format_briefing_card(data)
    assert title == "☀️ Briefing du jour"
    assert "Météo" in card
    assert "Ensoleillé, 22°C" in card
    assert "Titre 1 Tech" in card


def test_briefing_ne_rejoue_pas_les_anciens_messages_non_lus(monkeypatch):
    import core.daily_briefing as briefing

    class Service:
        def status(self):
            return type("Status", (), {"authenticated": True})()

        def get_unread(self, **_kwargs):
            raise AssertionError("Le briefing ne doit pas lister les anciens e-mails")

    monkeypatch.setattr(briefing, "get_gmail_service", lambda: Service(), raising=False)

    async def scenario():
        # Le module est importé à la demande : injecter le faux service sur
        # l'import réel pour garder le test indépendant d'OAuth.
        import core.email_service as email_service
        monkeypatch.setattr(email_service, "get_gmail_service", lambda: Service())
        result = await briefing._fetch_emails()
        assert "Veille Gmail active" in result

    asyncio.run(scenario())


def test_collect_briefing_data():
    async def _test():
        data = await collect_briefing_data(user_name="Anonymous")
        assert isinstance(data, BriefingData)
        assert data.user_name == "Anonymous"
        assert len(data.weather) > 0
        assert len(data.system) > 0

    asyncio.run(_test())


def test_briefing_uses_phone_gps_instead_of_france(monkeypatch):
    import core.daily_briefing as db_mod

    monkeypatch.setattr(
        "core.geolocation.get_user_location",
        lambda: {
            "city": "Kouriah", "country_name": "Guinée",
            "lat": 9.7921, "lon": -13.3185, "source": "phone-gps",
        },
    )

    async def weather(location):
        assert location["source"] == "phone-gps"
        return "Météo à Kouriah."

    async def empty_text(*_args, **_kwargs):
        return "RAS"

    async def empty_news(*_args, **_kwargs):
        return []

    monkeypatch.setattr(db_mod, "_fetch_weather", weather)
    monkeypatch.setattr(db_mod, "_fetch_emails", empty_text)
    monkeypatch.setattr(db_mod, "_fetch_reminders", empty_text)
    monkeypatch.setattr(db_mod, "_fetch_calendar", empty_text)
    monkeypatch.setattr(db_mod, "_fetch_news", empty_news)
    monkeypatch.setattr(db_mod, "_fetch_system_status", empty_text)

    data = asyncio.run(db_mod.collect_briefing_data("Anonymous"))

    assert data.location == "Kouriah, Guinée"
    assert "Kouriah" in data.weather


def test_weather_ne_retombe_jamais_sur_paris_sans_position():
    import core.daily_briefing as db_mod

    result = asyncio.run(db_mod._fetch_weather({"lat": None, "lon": None}))

    assert "position vérifiée" in result
    assert "Paris" not in result
