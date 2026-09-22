"""Résolution nom de site → URL : catalogue, vérification, jamais de page morte."""
import pytest

from core import site_resolver as sr


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "_LEARNED_PATH", tmp_path / "learned.json")
    monkeypatch.setattr(sr, "_learned", None)


def test_catalog_names_are_instant_and_exact():
    assert sr.resolve("", site="Google Vids").url == "https://vids.google.com"
    assert sr.resolve("", site="le site de TikTok Studio").url == "https://www.tiktok.com/tiktokstudio"
    assert sr.resolve("youtub").url == "https://www.youtube.com"


def test_user_name_wins_over_model_guess():
    res = sr.resolve("googlevids.com", site="Google Vids")
    assert res.url == "https://vids.google.com" and res.how == "catalog"


def test_local_and_special_urls_untouched():
    assert sr.resolve("localhost:3000").url == "http://localhost:3000"
    assert sr.resolve("file:///tmp/a.html").url == "file:///tmp/a.html"


def test_dead_domain_is_replaced_by_official_site(monkeypatch):
    monkeypatch.setattr(sr, "_host_exists", lambda host: False)
    monkeypatch.setattr(sr, "_lookup_official", lambda name: "https://www.orange-guinee.com/fr/offres")
    res = sr.resolve("orangeguinee.gn", site="Orange Guinée")
    assert res.url == "https://www.orange-guinee.com" and res.how == "lookup"
    # Mémorisé : la deuxième demande ne cherche plus.
    monkeypatch.setattr(sr, "_lookup_official", lambda name: pytest.fail("pas de 2e recherche"))
    assert sr.resolve("", site="orange guinée").url == "https://www.orange-guinee.com"


def test_dead_path_on_live_domain_opens_home(monkeypatch):
    monkeypatch.setattr(sr, "_host_exists", lambda host: True)
    monkeypatch.setattr(sr, "_page_alive", lambda url: url.endswith(".fr"))
    assert sr.resolve("https://exemple.fr/page-inventee").url == "https://exemple.fr"


def test_unknown_site_falls_back_to_google_search(monkeypatch):
    monkeypatch.setattr(sr, "_lookup_official", lambda name: None)
    monkeypatch.setattr(sr, "_guess_dotcom", lambda simple: None)
    res = sr.resolve("", site="zzqxw plateforme")
    assert res.how == "search" and res.url.startswith("https://www.google.com/search?q=")


def test_official_pick_requires_the_name_in_the_domain():
    rows = [{"link": "https://ivg.gouv.fr/"}, {"link": "https://fr.wikipedia.org/wiki/Kayak"},
            {"link": "https://www.kayak.fr/"}]
    assert sr._pick_official(rows, "kayak") == "https://www.kayak.fr/"
    assert sr._pick_official([{"link": "https://ivg.gouv.fr/"}], "kayak") is None


def test_multiword_site_parsed_from_voice_command():
    from actions.browser_control import _parse_browser_command_locally as parse
    assert parse("ouvre google vids dans chrome") == {"action": "go_to", "site": "google vids"}
    assert parse("ouvre un nouvel onglet")["action"] == "new_tab"
