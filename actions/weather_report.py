"""
weather_report.py — Météo conversationnelle ultra‑réaliste
Récupération réelle des données (Open‑Meteo / wttr.in), géocodage,
parsing local multilingue, ouverture navigateur en dernier recours.
Aucune clé API requise pour la météo.
"""
import json
import re
from core.browser_policy import open_chrome
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Tuple
from urllib.parse import quote_plus

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

# ── Configuration ───────────────────────────────────────────────────────────
def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_api_key() -> str:
    try:
        config_path = _base_dir() / "config" / "api_keys.json"
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""

# ── Codes météo WMO → description française ────────────────────────────────
_WMO_CODES: Dict[int, str] = {
    0:  "ciel dégagé",            1:  "principalement dégagé",
    2:  "partiellement nuageux",  3:  "couvert",
    45: "brouillard",             48: "brouillard givrant",
    51: "bruine légère",          53: "bruine modérée",
    55: "bruine dense",           56: "bruine verglaçante légère",
    57: "bruine verglaçante dense",
    61: "pluie légère",           63: "pluie modérée",
    65: "pluie forte",            66: "pluie verglaçante légère",
    67: "pluie verglaçante forte",
    71: "neige légère",           73: "neige modérée",
    75: "neige forte",            77: "grains de neige",
    80: "averses légères",        81: "averses modérées",
    82: "averses violentes",      85: "averses de neige légères",
    86: "averses de neige fortes",
    95: "orage",                  96: "orage avec grêle légère",
    99: "orage avec grêle forte",
}

_WMO_EMOJI: Dict[int, str] = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️", 56: "🌧️", 57: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️", 77: "🌨️",
    80: "🌦️", 81: "🌧️", 82: "⛈️", 85: "🌨️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


def _weather_emoji(code: Optional[int]) -> str:
    return _WMO_EMOJI.get(code, "🌡️")

def _deg_to_cardinal(deg: Optional[float]) -> str:
    if deg is None:
        return ""
    dirs = ["nord", "nord-est", "est", "sud-est",
            "sud", "sud-ouest", "ouest", "nord-ouest"]
    return dirs[round(deg / 45) % 8]

# ── Géocodage (Open‑Meteo, sans clé) ────────────────────────────────────────
# Réseau : trois appels en cascade (géocodage, prévision, repli) doivent tenir
# sous le plafond de 25 s du répartiteur, marge comprise.
_HTTP_TIMEOUT = 6.0


@kit.memo(6 * 3600, key=lambda city: city.casefold().strip())
def _geocode_city(city: str) -> Optional[Tuple[float, float, str, str]]:
    """Retourne (lat, lon, nom_officiel, pays) ou None. Une ville ne bouge
    pas : le résultat est mémorisé pour la session."""
    url = ("https://geocoding-api.open-meteo.com/v1/search"
           f"?name={quote_plus(city)}&count=1&language=fr&format=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        return (float(top["latitude"]), float(top["longitude"]),
                top.get("name", city), top.get("country", ""))
    except Exception as e:
        print(f"[Weather] géocodage échoué : {e}")
        return None

# ── Récupération météo Open‑Meteo (sans clé) ───────────────────────────────
@kit.memo(600, key=lambda lat, lon: (round(lat, 2), round(lon, 2)))
def _fetch_open_meteo(lat: float, lon: float) -> Optional[Dict]:
    """Prévision mémorisée dix minutes : « et demain ? » ne repaie pas l'appel."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
           "is_day,precipitation,weather_code,wind_speed_10m,wind_direction_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,sunrise,sunset"
           "&forecast_days=7&timezone=auto")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[Weather] Open‑Meteo échoué : {e}")
        return None

# ── Récupération météo wttr.in (repli, sans clé) ───────────────────────────
def _fetch_wttr(city: str) -> Optional[Dict]:
    url = f"https://wttr.in/{quote_plus(city)}?format=j1&lang=fr"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[Weather] wttr.in échoué : {e}")
        return None

# ── Formatage des réponses ──────────────────────────────────────────────────
def _day_index(when: str) -> int:
    when = (when or "").lower()
    if "demain" in when or "tomorrow" in when:
        return 1
    if "après-demain" in when or "after tomorrow" in when:
        return 2
    return 0

def _v(value, unit: str = "", digits: int = 0) -> str:
    """Valeur lisible, ou « n/d » : Open-Meteo et wttr laissent des trous
    (`null`, chaîne vide) qui s'entendaient « None degrés » à voix haute."""
    if value is None or value == "":
        return "n/d"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value}{unit}"
    text = f"{number:.{digits}f}" if digits or number != int(number) else f"{int(number)}"
    return f"{text}{unit}"


def _range(low, high) -> str:
    if low is None and high is None:
        return "n/d"
    if low is None:
        return f"max. {_v(high, '°C')}"
    if high is None:
        return f"min. {_v(low, '°C')}"
    return f"{_v(low)}°C à {_v(high)}°C"


def _format_open_meteo(data: Dict, name: str, when: str) -> str:
    cur   = data.get("current", {}) or {}
    daily = data.get("daily", {}) or {}
    code  = cur.get("weather_code")
    desc  = _WMO_CODES.get(code, "conditions variées")
    temp  = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    hum   = cur.get("relative_humidity_2m")
    wind  = cur.get("wind_speed_10m")
    wdir  = _deg_to_cardinal(cur.get("wind_direction_10m"))
    precip = cur.get("precipitation")

    lines = [f"🌤️ Météo pour {name} :"]
    feels_str = f" (ressenti {_v(feels, '°C')})" if feels is not None else ""
    lines.append(f"Actuellement : {desc}, {_v(temp, '°C')}{feels_str}.")
    lines.append(f"💧 Humidité : {_v(hum, '%')} | 💨 Vent : {_v(wind, ' km/h')} {wdir}".rstrip()
                 + f" | 🌧️ Précipitations : {_v(precip, ' mm')}.")

    tmax_list = daily.get("temperature_2m_max", [])
    tmin_list = daily.get("temperature_2m_min", [])
    code_list = daily.get("weather_code", [])
    prob_list = daily.get("precipitation_probability_max", [])
    sunrise   = daily.get("sunrise", [])
    sunset    = daily.get("sunset", [])

    idx = _day_index(when)
    if when and ("semaine" in when.lower() or "week" in when.lower()):
        # Résumé de la semaine
        if tmax_list and tmin_list and code_list:
            week = []
            for i in range(min(5, len(tmax_list))):
                d = _WMO_CODES.get(code_list[i], "")
                low = tmin_list[i] if i < len(tmin_list) else None
                week.append(f"J+{i}: {_range(low, tmax_list[i])}" + (f", {d}" if d else ""))
            lines.append("📅 Semaine : " + " | ".join(week))
    else:
        if idx < len(tmax_list) and idx < len(tmin_list):
            day_desc = _WMO_CODES.get(code_list[idx] if idx < len(code_list) else None, "")
            prob = prob_list[idx] if idx < len(prob_list) else None
            prob_str = f", {_v(prob, '%')} de risque de pluie" if prob is not None else ""
            label = "Aujourd'hui" if idx == 0 else ("Demain" if idx == 1 else f"Jour {idx}")
            detail = "".join(x for x in (f", {day_desc}" if day_desc else "", prob_str))
            lines.append(f"📅 {label} : {_range(tmin_list[idx], tmax_list[idx])}{detail}.")
            if idx == 0 and sunrise and sunset and sunrise[0] and sunset[0]:
                sr = sunrise[0].split("T")[1] if "T" in sunrise[0] else sunrise[0]
                ss = sunset[0].split("T")[1] if "T" in sunset[0] else sunset[0]
                lines.append(f"🌅 Lever : {sr} | 🌇 Coucher : {ss}.")
    return "\n".join(lines)


# Corps Markdown de la dernière météo Open-Meteo, pour la carte riche de
# l'UI (icônes + prévision 3 jours) — le texte brut ci-dessus reste utilisé
# pour la réponse vocale/LLM.
_last_weather_card: Optional[str] = None


def get_last_weather_card() -> Optional[str]:
    global _last_weather_card
    v, _last_weather_card = _last_weather_card, None
    return v


def format_weather_card_markdown(data: Dict, name: str) -> str:
    cur = data.get("current", {}) or {}
    daily = data.get("daily", {}) or {}
    code = cur.get("weather_code")
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")

    lines = [f"### {_weather_emoji(code)} {name}"]
    if temp is not None:
        lines.append(f"**{temp:.0f}°C** — {_WMO_CODES.get(code, 'conditions variées')}"
                     + (f" _(ressenti {feels:.0f}°C)_" if feels is not None else ""))
    lines.append("")

    tmax_list = daily.get("temperature_2m_max", [])
    tmin_list = daily.get("temperature_2m_min", [])
    code_list = daily.get("weather_code", [])
    day_labels = ["Aujourd'hui", "Demain", "Après-demain"]
    for i in range(min(3, len(tmax_list), len(tmin_list))):
        d_code = code_list[i] if i < len(code_list) else None
        label = day_labels[i] if i < len(day_labels) else f"Jour {i}"
        # Open-Meteo laisse des `null` dans ces tableaux quand une borne
        # manque : la liste a bien sa longueur, mais pas ses valeurs. Formater
        # sans regarder faisait échouer toute la météo pour un seul trou.
        low, high = tmin_list[i], tmax_list[i]
        if low is None and high is None:
            continue
        temps = " / ".join(f"{v:.0f}°" for v in (low, high) if v is not None)
        lines.append(f"{_weather_emoji(d_code)} **{label}** — {temps}C")
    return "\n".join(lines).strip()


def _format_wttr(data: Dict, city: str, when: str) -> str:
    try:
        cur = data.get("current_condition", [{}])[0]
        desc = (cur.get("weatherDesc") or [{}])[0].get("value", "")
        temp = cur.get("temp_C")
        feels = cur.get("FeelsLikeC")
        hum = cur.get("humidity")
        wind = cur.get("windspeedKmph")
        wdir = cur.get("winddir16Point", "")
        precip = cur.get("precipMM")
        lines = [f"🌤️ Météo pour {city} :"]
        feels_str = f" (ressenti {_v(feels, '°C')})" if feels not in (None, "") else ""
        lines.append(f"Actuellement : {desc or 'conditions variées'}, {_v(temp, '°C')}{feels_str}.")
        lines.append(f"💧 Humidité : {_v(hum, '%')} | 💨 Vent : {_v(wind, ' km/h')} {wdir}".rstrip()
                     + f" | 🌧️ Précipitations : {_v(precip, ' mm')}.")
        weather = data.get("weather", [])
        idx = _day_index(when)
        if idx < len(weather):
            day = weather[idx]
            tmin = day.get("mintempC"); tmax = day.get("maxtempC")
            label = "Aujourd'hui" if idx == 0 else ("Demain" if idx == 1 else f"Jour {idx}")
            # description à midi
            hourly = day.get("hourly", [])
            noon = next((h for h in hourly if h.get("time") in ("1200", "900")), None)
            day_desc = (noon.get("weatherDesc") or [{}])[0].get("value", "") if noon else ""
            tmin = None if tmin in (None, "") else tmin
            tmax = None if tmax in (None, "") else tmax
            lines.append(f"📅 {label} : {_range(tmin, tmax)}" + (f", {day_desc}." if day_desc else "."))
        return "\n".join(lines)
    except Exception as e:
        return f"Impossible de formater la météo wttr : {e}"

# ── Parsing local amélioré ──────────────────────────────────────────────────
def _parse_weather_request_locally(text: str) -> Optional[Dict[str, str]]:
    """
    Analyse une phrase naturelle et retourne {'city': ..., 'time': ...}.
    """
    original = text
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|je veux|j'aimerais|dis[- ]moi)\b", " ", text).strip()

    city = None
    stop = {"demain", "aujourd'hui", "ce soir", "cette semaine", "semaine prochaine",
            "le", "la", "un", "une", "mon", "ma", "ville", "temps", "météo", "weather",
            "quel", "quelle", "fait", "il", "donne", "montre"}

    # Motifs avec préposition
    patterns = [
        r"(?:à|sur|pour|de|d'|in|for|at|over)\s+(?:la ville de\s+)?"
        r"([a-zàâäéèêëîïôöùûüç' -]+?)(?=\s+(?:demain|aujourd'hui|ce soir|cette semaine|la semaine prochaine|le \d|tomorrow|today|this week|next week)\b|\s*[?,!.]|\s*$)",
        r"(?:météo|meteo|weather|temps|climat)\s+(?:à|de|sur|pour|in|at)\s+"
        r"([a-zàâäéèêëîïôöùûüç' -]+?)(?=\s+[?,!.]|\s*$)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            cand = m.group(1).strip(" .,!?-'")
            if cand and cand not in stop:
                city = cand
                break

    # Fallback : nom propre en majuscule dans le texte original
    if not city:
        m = re.search(r"\b([A-ZÀ-Þ][a-zà-ÿ]+(?:\s+[A-ZÀ-Þ][a-zà-ÿ]+)*)\b", original)
        if m:
            cand = m.group(1).strip()
            cap_stop = {"Quel", "Quelle", "Météo", "Weather", "Temps", "Dis", "Donne", "Montre", "Bonjour"}
            if cand and cand not in cap_stop:
                city = cand

    # Période temporelle
    time_keywords = {
        "demain": "demain", "tomorrow": "demain",
        "après-demain": "après-demain", "after tomorrow": "après-demain",
        "ce soir": "ce soir", "tonight": "ce soir",
        "cette semaine": "cette semaine", "this week": "cette semaine",
        "semaine prochaine": "semaine prochaine", "next week": "semaine prochaine",
        "aujourd'hui": "aujourd'hui", "today": "aujourd'hui",
    }
    time_str = "aujourd'hui"
    for kw, t in time_keywords.items():
        if kw in text:
            time_str = t
            break
    date_match = re.search(r"le\s+(\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre|january|february|march|april|may|june|july|august|september|october|november|december))", text)
    if date_match:
        time_str = date_match.group(0)

    return {"city": city, "time": time_str} if city else None

def _detect_weather_intent_ai(description: str) -> Optional[Dict[str, str]]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Extrais la ville et la période temporelle de la phrase suivante pour une demande météo. "
            f"Retourne UNIQUEMENT un objet JSON avec les clés 'city' et 'time' "
            f"(ex: 'today', 'demain', 'le 15 mars'). Si absent, mets 'aujourd'hui' pour time. "
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'{.*}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[Weather] Erreur IA : {e}")
    return None

# ── Fonction principale ─────────────────────────────────────────────────────
@kit.action("weather_action")
def weather_action(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Récupère la météo réelle d'une ville (Open‑Meteo / wttr.in) et la retourne.
    Paramètres acceptés :
        description : phrase naturelle
        city        : nom de la ville
        time        : période (aujourd'hui, demain, cette semaine, etc.)
        open_browser: si True, ouvre aussi Google météo dans le navigateur
    """
    params = parameters or {}
    description = params.get("description", "").strip()
    city = params.get("city")
    when = params.get("time", "aujourd'hui")
    open_browser = bool(params.get("open_browser", False))

    # Interprétation à partir d'une description
    if description and not city:
        local = _parse_weather_request_locally(description)
        if local:
            city = local.get("city")
            when = local.get("time", when)
        else:
            ai = _detect_weather_intent_ai(description)
            if ai:
                city = ai.get("city")
                when = ai.get("time", when)
            else:
                return "Je n'ai pas compris la ville ou la période pour la météo. Précisez par exemple : 'météo à Lyon demain'."

    if not city or not isinstance(city, str) or not city.strip():
        # « Quel temps fait-il ? » sans ville : là où se trouve l'utilisateur.
        city = _current_city()
        if not city:
            return "La ville n'est pas précisée pour le rapport météo."
    city = city.strip()
    when = (when or "aujourd'hui").strip()

    # 1. Géocodage + Open‑Meteo
    geo = _geocode_city(city)
    msg = None
    if geo:
        lat, lon, name, country = geo
        data = _fetch_open_meteo(lat, lon)
        if data:
            msg = _format_open_meteo(data, name, when)
            global _last_weather_card
            _last_weather_card = format_weather_card_markdown(data, name)

    # 2. Repli wttr.in si échec
    if not msg:
        wttr = _fetch_wttr(city)
        if wttr:
            msg = _format_wttr(wttr, city, when)

    # 3. Dernier recours : navigateur
    if not msg:
        search_query = f"météo {city}"
        if when and when != "aujourd'hui":
            search_query += f" {when}"
        url = f"https://www.google.com/search?q={quote_plus(search_query)}"
        try:
            open_chrome(url)
            msg = f"Je n'ai pas pu récupérer la météo directement, j'ai ouvert le navigateur pour {city}."
        except Exception as e:
            msg = f"Impossible d'ouvrir le navigateur pour la météo : {e}"

    # Ouverture navigateur optionnelle même si la météo a été récupérée
    if open_browser and geo:
        try:
            open_chrome(f"https://www.google.com/search?q={quote_plus('météo ' + city)}")
        except Exception:
            pass

    _log(msg, player)
    if session_memory:
        try:
            session_memory.set_last_search(query=f"météo {city}", response=msg)
        except Exception:
            pass
    return msg

def _current_city() -> Optional[str]:
    """Ville courante (GPS du téléphone, IP, ou configuration), sinon None."""
    try:
        from core.geolocation import get_user_location
        loc = get_user_location() or {}
    except Exception:
        return None
    city = str(loc.get("city") or "").strip()
    if city:
        country = str(loc.get("country_name") or "").strip()
        return f"{city}, {country}" if country else city
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is not None and lon is not None:
        try:
            from core.geolocation import reverse_geocode
            place = reverse_geocode(float(lat), float(lon)) or {}
            return str(place.get("city") or place.get("name") or "").strip() or None
        except Exception:
            return None
    return None


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass

# Alias pour compatibilité
weather_report = weather_action

# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(weather_action({"description": " ".join(sys.argv[1:])}))
    else:
        print(weather_action({"city": "Paris", "time": "demain"}))
