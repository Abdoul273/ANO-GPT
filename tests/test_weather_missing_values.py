"""Une valeur météo absente se dit « n/d », jamais « None » ni un plantage."""
from actions.weather_report import _format_open_meteo, _format_wttr, format_weather_card_markdown

_HOLES = {
    "current": {"temperature_2m": None, "apparent_temperature": None, "relative_humidity_2m": None},
    "daily": {"temperature_2m_max": [None, 31.2], "temperature_2m_min": [24.0, None],
              "weather_code": [None, 61], "precipitation_probability_max": [None, 80],
              "sunrise": [None], "sunset": [None]},
}


def test_open_meteo_holes_never_say_none():
    for when in ("", "demain", "cette semaine"):
        text = _format_open_meteo(_HOLES, "Conakry", when)
        assert "None" not in text and ", ." not in text
    assert "min. 24°C" in _format_open_meteo(_HOLES, "Conakry", "")
    assert "max. 31°C" in _format_open_meteo(_HOLES, "Conakry", "demain")


def test_card_and_wttr_survive_holes():
    assert "None" not in format_weather_card_markdown(_HOLES, "Conakry")
    text = _format_wttr({"current_condition": [{"temp_C": "", "FeelsLikeC": None}],
                         "weather": [{"mintempC": "", "maxtempC": "30"}]}, "Conakry", "")
    assert "None" not in text and "max. 30°C" in text
