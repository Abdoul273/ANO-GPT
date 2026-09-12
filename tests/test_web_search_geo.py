"""Tests de la détection de pays / localisation dans actions/web_search.py."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import web_search as ws


def test_detects_explicit_country_in_query():
    result = ws._detect_country_geo("combien coûte un iphone en Guinée")
    assert result is not None
    name, gl, hl, currency, has_shopping = result
    assert gl == "gn"
    assert currency == "GNF"
    assert has_shopping is False


def test_no_country_mentioned_returns_none():
    assert ws._detect_country_geo("combien coûte un iphone") is None


def test_ivory_coast_long_form_not_shadowed_by_short_form():
    # "ivoire" seul existe aussi dans la table ; la forme longue doit gagner.
    result = ws._detect_country_geo("prix à Abidjan en Côte d'Ivoire")
    assert result[1] == "ci"


def test_geo_params_falls_back_to_user_location_when_no_country_in_query():
    with patch("core.geolocation.get_user_location", return_value={
        "country_code": "gn", "hl": "fr", "city": "", "country_name": "Guinée",
        "lat": None, "lon": None, "source": "config",
    }):
        params = ws._geo_params("hôpital le plus proche")
    assert params["gl"] == "gn"
    assert params["location"] == "Guinea"


def test_geo_params_uses_explicit_country_over_user_location():
    with patch("core.geolocation.get_user_location", return_value={
        "country_code": "gn", "hl": "fr", "city": "", "country_name": "Guinée",
        "lat": None, "lon": None, "source": "config",
    }):
        params = ws._geo_params("prix iphone 16 en France")
    assert params["gl"] == "fr"


def test_nearby_intent_extracts_place_category():
    parsed = ws._parse_search_request_locally("où se trouve l'hôpital le plus proche de moi")
    assert parsed == {"mode": "nearby", "query": "hôpital"}


def test_nearby_intent_near_me_variant():
    parsed = ws._parse_search_request_locally("pharmacie près de moi")
    assert parsed == {"mode": "nearby", "query": "pharmacie"}


def test_price_mode_still_detected_normally():
    parsed = ws._parse_search_request_locally("combien coûte un iphone 16")
    assert parsed["mode"] == "price"


def test_tiktoker_handle_uses_social_profile_mode():
    parsed = ws._parse_search_request_locally(
        "est-ce que tu connais le tiktokeur techenclair ?"
    )

    assert parsed == {
        "mode": "social",
        "platform": "tiktok",
        "handle": "techenclair",
        "query": "techenclair",
    }


def test_social_profile_query_is_constrained_to_requested_network():
    assert ws._social_search_query("techenclair", "tiktok") == (
        'site:tiktok.com "techenclair"'
    )


def test_social_profile_intent_handles_account_of_name_wording():
    parsed = ws._parse_search_request_locally("cherche le compte TikTok de @techenclair")

    assert parsed["platform"] == "tiktok"
    assert parsed["handle"] == "techenclair"


def test_social_result_never_confirms_a_video_as_a_profile():
    result = ws._format_social_profiles("techenclair", "tiktok", [{
        "title": "Une vidéo",
        "url": "https://www.tiktok.com/@techenclair/video/123",
    }])

    assert "Aucun profil TikTok public" in result


def test_social_result_returns_only_a_plausible_profile_url():
    result = ws._format_social_profiles("techenclair", "tiktok", [
        {"title": "Recherche", "url": "https://www.tiktok.com/search?q=techenclair"},
        {"title": "TechEnClair (@techenclair)", "url": "https://www.tiktok.com/@techenclair"},
    ])

    assert "TechEnClair" in result
    assert "https://www.tiktok.com/@techenclair" in result
    assert "Recherche" not in result
