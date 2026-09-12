"""La session vocale doit connaître la position réelle d'ANO Remote."""

from core import context_probe


def test_phone_gps_is_injected_in_ambient_context(monkeypatch):
    monkeypatch.setattr(
        "core.geolocation.get_user_location",
        lambda: {
            "city": "Kouriah", "country_name": "Guinée",
            "source": "phone-gps", "accuracy_m": 75.3,
        },
    )
    context_probe.clear_cache()

    context = context_probe.ambient_context()

    assert "Localisation: Kouriah, Guinée" in context
    assert "source=phone-gps" in context
    assert "±75 m" in context


def test_unknown_location_explicitly_forbids_language_guess(monkeypatch):
    monkeypatch.setattr(
        "core.geolocation.get_user_location",
        lambda: {"city": "", "country_name": "", "source": "default"},
    )
    context_probe.clear_cache()

    assert "ne jamais déduire le pays de la langue" in context_probe.ambient_context()


def test_live_model_has_a_verified_location_tool():
    import main

    declaration = next(
        item for item in main.TOOL_DECLARATIONS if item["name"] == "location"
    )
    description = declaration["description"]
    assert "ALWAYS" in description
    assert "Never infer a country" in description
