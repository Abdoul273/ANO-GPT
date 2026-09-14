"""core/country_info.py — Fiche pays pour la carte : n'importe quel pays du monde.

restcountries.com (utilisé initialement) a fermé son accès sans clé en 2026 —
voir https://restcountries.com/docs/countries/legacy-api-deprecation. L'identité
d'un pays (capitale, monnaie, langues, coordonnées) ne change presque jamais :
elle vient donc d'un jeu de données local (config/countries.json, voir
config/countries.json.LICENSE — mledoze/countries, ODbL). Seul ce qui bouge
vraiment reste en direct, sans clé :
* météo + fuseau horaire — Open‑Meteo, déjà utilisé par actions/weather_report.py ;
* population — Banque mondiale, best‑effort (la fiche reste utile sans elle).

Par défaut, sans pays précisé, la fiche porte sur la Guinée — mais toute
demande explicite (« montre-moi les infos sur le Japon ») fonctionne pour
n'importe lequel des 250 pays du jeu de données.
"""

from __future__ import annotations

import json
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from core import action_kit as kit

DEFAULT_COUNTRY = "Guinée"

_DATA_PATH = Path(__file__).resolve().parent.parent / "config" / "countries.json"
_HTTP_TIMEOUT = 6.0

# Même échelle WMO que actions/weather_report.py, réduite aux catégories
# utiles à une ligne de fiche pays (pas de détail bruine/neige/verglas ici).
_WMO_SHORT: dict[int, tuple[str, str]] = {
    0: ("ciel dégagé", "☀️"), 1: ("principalement dégagé", "🌤️"),
    2: ("partiellement nuageux", "⛅"), 3: ("couvert", "☁️"),
    45: ("brouillard", "🌫️"), 48: ("brouillard givrant", "🌫️"),
    51: ("bruine", "🌦️"), 53: ("bruine", "🌦️"), 55: ("bruine", "🌦️"),
    61: ("pluie légère", "🌧️"), 63: ("pluie", "🌧️"), 65: ("pluie forte", "🌧️"),
    71: ("neige légère", "🌨️"), 73: ("neige", "🌨️"), 75: ("neige forte", "❄️"),
    80: ("averses", "🌦️"), 81: ("averses", "🌧️"), 82: ("averses violentes", "⛈️"),
    95: ("orage", "⛈️"), 96: ("orage avec grêle", "⛈️"), 99: ("orage avec grêle", "⛈️"),
}


def _fold(text: str) -> str:
    """Casefold + sans accents, pour retrouver « Senegal » comme « Sénégal »."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).casefold().strip()


@lru_cache(maxsize=1)
def _load_dataset() -> list[dict]:
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[Pays] jeu de données introuvable ({_DATA_PATH}) : {exc}")
        return []


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    """Toutes les graphies plausibles → l'entrée pays, repliées sans accent."""
    idx: dict[str, dict] = {}
    for entry in _load_dataset():
        keys = {
            entry.get("common", ""), entry.get("official", ""),
            entry.get("fr_common", ""), entry.get("fr_official", ""),
            entry.get("cca2", ""), entry.get("cca3", ""),
        }
        for key in keys:
            if key:
                idx[_fold(key)] = entry
    return idx


def _lookup(name: str) -> Optional[dict]:
    idx = _index()
    folded = _fold(name)
    if folded in idx:
        return idx[folded]
    # Recherche partielle : « congo » doit trouver une des deux Républiques
    # du Congo plutôt que rien — mieux vaut une réponse approximative
    # qu'un refus, tant que l'utilisateur peut préciser derrière.
    matches = [entry for key, entry in idx.items() if folded and folded in key]
    return matches[0] if matches else None


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


@kit.memo(600, key=lambda lat, lon: (round(lat, 2), round(lon, 2)))
def _fetch_weather(lat: float, lon: float) -> Optional[dict]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&timezone=auto"
    )
    try:
        return _get_json(url)
    except Exception as exc:
        print(f"[Pays] météo échouée : {exc}")
        return None


@kit.memo(86400, key=lambda cca3: cca3)
def _fetch_population(cca3: str) -> Optional[int]:
    """Banque mondiale, mémorisée un jour. Silencieux si indisponible :
    une fiche pays sans population reste utile, mieux vaut ça qu'attendre."""
    if not cca3:
        return None
    url = f"https://api.worldbank.org/v2/country/{cca3}/indicator/SP.POP.TOTL?format=json&mrnev=1"
    try:
        data = _get_json(url)
        rows = data[1] if isinstance(data, list) and len(data) > 1 else None
        if rows and rows[0].get("value") is not None:
            return int(rows[0]["value"])
    except Exception as exc:
        print(f"[Pays] population échouée : {exc}")
    return None


@dataclass
class CountryInfo:
    name: str
    official_name: str
    capital: str
    population: Optional[int]
    currencies: str
    languages: str
    timezone: str
    flag: str
    region: str
    subregion: str
    area_km2: float
    lat: float
    lon: float
    weather_text: str = ""
    weather_emoji: str = ""
    temp_c: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_marker(self) -> dict[str, Any]:
        """Fiche flottante au format déjà attendu par map_render (_marker_payload)."""
        return {
            "n": 0,
            "name": f"{self.flag} {self.name}",
            "lat": self.lat,
            "lon": self.lon,
            "address": self.capital and f"Capitale : {self.capital}",
            "category": self.region,
        }

    def as_tool_result(self) -> str:
        pop = f"{self.population:,}".replace(",", " ") if self.population else "inconnue"
        weather = (
            f"{self.weather_emoji} {self.weather_text}, {self.temp_c:.0f}°C"
            if self.temp_c is not None else "indisponible"
        )
        return (
            f"[FICHE PAYS — {self.name}]\n"
            f"Capitale : {self.capital or 'inconnue'}\n"
            f"Population : {pop}\n"
            f"Monnaie : {self.currencies or 'inconnue'}\n"
            f"Langues : {self.languages or 'inconnues'}\n"
            f"Fuseau horaire : {self.timezone or 'inconnu'}\n"
            f"Région : {self.region}" + (f" ({self.subregion})" if self.subregion else "") + "\n"
            f"Météo à la capitale : {weather}\n\n"
            "Ces données viennent d'un jeu de données pays local, de la "
            "Banque mondiale (population) et d'Open‑Meteo (météo) : réponds "
            "à partir d'elles, sans en inventer d'autres. Si l'utilisateur "
            "demande un autre pays, rappelle show_country_info avec ce pays."
        )


def fetch_country_info(name: str = "") -> Optional[CountryInfo]:
    """Fiche complète pour un pays. Rend None si aucun pays ne correspond."""
    query = (name or DEFAULT_COUNTRY).strip()
    entry = _lookup(query)
    if not entry:
        return None

    lat, lon = (entry.get("latlng") or [0.0, 0.0])[:2]
    currencies = ", ".join(
        f"{v.get('name', code)} ({v.get('symbol', code)})"
        for code, v in (entry.get("currencies") or {}).items()
    )
    languages = ", ".join((entry.get("languages") or {}).values())

    weather_text, weather_emoji, temp_c, timezone = "", "", None, ""
    weather = _fetch_weather(lat, lon)
    if weather:
        timezone = str(weather.get("timezone") or "")
        current = weather.get("current") or {}
        code = current.get("weather_code")
        if code is not None:
            weather_text, weather_emoji = _WMO_SHORT.get(code, ("", "🌡️"))
            temp_c = current.get("temperature_2m")

    return CountryInfo(
        name=str(entry.get("fr_common") or entry.get("common") or query),
        official_name=str(entry.get("fr_official") or entry.get("official") or query),
        capital=", ".join(entry.get("capital") or []),
        population=_fetch_population(str(entry.get("cca3") or "")),
        currencies=currencies,
        languages=languages,
        timezone=timezone,
        flag=str(entry.get("flag") or ""),
        region=str(entry.get("region") or ""),
        subregion=str(entry.get("subregion") or ""),
        area_km2=float(entry.get("area") or 0.0),
        lat=float(lat), lon=float(lon),
        weather_text=weather_text, weather_emoji=weather_emoji, temp_c=temp_c,
        raw=entry,
    )
