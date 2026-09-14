"""Fiche pays : dataset local, n'importe quel pays, défaut Guinée."""

from core import country_info as ci


def test_le_jeu_de_donnees_local_existe_et_contient_la_guinee():
    entry = ci._lookup("Guinée")
    assert entry is not None
    assert entry["cca3"] == "GIN"
    assert entry["capital"] == ["Conakry"]


def test_la_recherche_ignore_les_accents_et_la_casse():
    assert ci._lookup("senegal") is not None
    assert ci._lookup("SÉNÉGAL") is not None
    assert ci._lookup("Sénégal")["cca3"] == ci._lookup("senegal")["cca3"]


def test_le_nom_anglais_et_la_traduction_francaise_trouvent_le_meme_pays():
    assert ci._lookup("Japan")["cca3"] == ci._lookup("Japon")["cca3"]


def test_un_pays_inconnu_ne_plante_pas():
    assert ci._lookup("Atlantide") is None


def test_fetch_country_info_sans_reseau(monkeypatch):
    """La fiche reste utile même si météo et population échouent."""
    monkeypatch.setattr(ci, "_fetch_weather", lambda lat, lon: None)
    monkeypatch.setattr(ci, "_fetch_population", lambda cca3: None)

    info = ci.fetch_country_info("Guinée")

    assert info is not None
    assert info.name == "Guinée"
    assert info.capital == "Conakry"
    assert info.flag == "🇬🇳"
    assert info.population is None
    assert info.temp_c is None
    assert "Conakry" in info.as_tool_result()


def test_sans_argument_le_defaut_est_la_guinee(monkeypatch):
    monkeypatch.setattr(ci, "_fetch_weather", lambda lat, lon: None)
    monkeypatch.setattr(ci, "_fetch_population", lambda cca3: None)

    info = ci.fetch_country_info("")

    assert info is not None
    assert info.name == "Guinée"


def test_un_pays_quelconque_fonctionne_pas_seulement_la_guinee(monkeypatch):
    monkeypatch.setattr(ci, "_fetch_weather", lambda lat, lon: None)
    monkeypatch.setattr(ci, "_fetch_population", lambda cca3: None)

    info = ci.fetch_country_info("Brésil")

    assert info is not None
    assert info.name == "Brésil"
    assert info.capital == "Brasília"


def test_meteo_et_population_enrichissent_la_fiche_quand_disponibles(monkeypatch):
    monkeypatch.setattr(
        ci, "_fetch_weather",
        lambda lat, lon: {
            "timezone": "Africa/Conakry",
            "current": {"weather_code": 0, "temperature_2m": 29.0},
        },
    )
    monkeypatch.setattr(ci, "_fetch_population", lambda cca3: 15_000_000)

    info = ci.fetch_country_info("Guinée")

    assert info.temp_c == 29.0
    assert info.weather_emoji == "☀️"
    assert info.timezone == "Africa/Conakry"
    assert info.population == 15_000_000
    result = info.as_tool_result()
    assert "29°C" in result
    assert "15 000 000" in result


def test_as_marker_retourne_le_format_attendu_par_map_render(monkeypatch):
    monkeypatch.setattr(ci, "_fetch_weather", lambda lat, lon: None)
    monkeypatch.setattr(ci, "_fetch_population", lambda cca3: None)

    info = ci.fetch_country_info("Guinée")
    marker = info.as_marker()

    assert marker["lat"] == info.lat
    assert marker["lon"] == info.lon
    assert "Guinée" in marker["name"]
