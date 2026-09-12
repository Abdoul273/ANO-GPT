#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flight_finder.py — Recherche de vols ultra‑réaliste (Google Flights).
Parsing local avancé, extraction multilingue des paramètres, fallback IA.
Retourne des résultats analysés par Gemini et sauvegarde un rapport sur le bureau.

Corrections par rapport à l'ancienne version :
    - `_get_base_dir` utilisait `Path(file)` au lieu de `Path(__file__)` :
      NameError à chaque lecture de la clé API ;
    - `_save_to_desktop` était appelée mais définie sous le nom
      `save_to_desktop` : NameError à la sauvegarde du rapport ;
    - `_get_api_key` plantait si la config était absente : try/except ajouté ;
    - l'URL Google Flights contenait des espaces littéraux et le « à » non
      encodé : réécrite avec urllib.parse.quote et le format de requête
      que Google comprend réellement ;
    - le parsing local passait le texte en minuscules AVANT des regex qui
      cherchaient des majuscules : la détection des villes ne marchait
      jamais ;
    - attente active des résultats (jusqu'à 15 s) au lieu d'un sleep(5) fixe ;
    - les dates passées sont désormais refusées clairement.
"""
import json
import re
import sys
import time
import platform
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

# ── Configuration ───────────────────────────────────────────────────────────
def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""

# ── Détection OS ────────────────────────────────────────────────────────────
def is_windows(): return platform.system() == "Windows"
def is_mac():     return platform.system() == "Darwin"
def is_linux():   return platform.system() == "Linux"

# ── Parsing des dates (multilingue) ─────────────────────────────────────────
_MONTH_MAP: Dict[str, int] = {
    "january":1, "february":2, "march":3, "april":4, "may":5, "june":6,
    "july":7, "august":8, "september":9, "october":10, "november":11, "december":12,
    "janvier":1, "février":2, "fevrier":2, "mars":3, "avril":4, "mai":5, "juin":6,
    "juillet":7, "août":8, "aout":8, "septembre":9, "octobre":10, "novembre":11,
    "décembre":12, "decembre":12,
    "jan":1, "feb":2, "mar":3, "apr":4, "jun":6, "jul":7,
    "aug":8, "sep":9, "oct":10, "nov":11, "dec":12,
}

def _parse_date(raw: str) -> str:
    raw   = raw.strip()
    lower = raw.lower()
    today = datetime.now()

    # Format ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw

    # Formats numériques courants
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Dates relatives
    relative = {
        "today": today, "tomorrow": today + timedelta(days=1),
        "aujourd'hui": today, "demain": today + timedelta(days=1),
    }
    for key, val in relative.items():
        if key in lower:
            return val.strftime("%Y-%m-%d")

    # Mois nom + jour (ex: "15 mars", "March 12")
    for month_name, month_num in _MONTH_MAP.items():
        if month_name in lower:
            day_match = re.search(r"\b(\d{1,2})\b", raw)
            if day_match:
                day = int(day_match.group(1))
                year = today.year if month_num >= today.month else today.year + 1
                return f"{year}-{month_num:02d}-{day:02d}"

    # Fallback IA
    try:
        api_key = _get_api_key()
        if api_key:
            from google import genai as _genai
            _client = _genai.Client(api_key=api_key)
            resp = _client.models.generate_content(
                model=FAST_MODEL,
                contents=(
                    f"Today is {today.strftime('%Y-%m-%d')}. "
                    f"Convert this date expression to YYYY-MM-DD: '{raw}'. "
                    f"Return ONLY the date string, nothing else."
                )
            )
            result = resp.text.strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", result):
                return result
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Échec parsing date IA : {e}")

    # Dernier recours
    print(f"[FlightFinder] ⚠️ Impossible d'analyser la date '{raw}' — utilisation de la date du jour.")
    return today.strftime("%Y-%m-%d")

# ── Construction de l'URL Google Flights ────────────────────────────────────
_CABIN_CODE: Dict[str, str] = {
    "economy": "1", "eco": "1", "economique": "1",
    "premium": "2",
    "business": "3", "affaires": "3",
    "first": "4", "première": "4", "premiere": "4",
}

def _build_google_flights_url(
    origin: str, destination: str, date: str,
    return_date: Optional[str] = None, passengers: int = 1, cabin: str = "economy",
) -> str:
    """Construit une URL Google Flights pré-remplie, correctement encodée."""
    query = f"Flights from {origin} to {destination} on {date}"
    if return_date:
        query += f" returning {return_date}"
    query += f" for {passengers} passenger{'s' if passengers > 1 else ''}"
    encoded = urllib.parse.quote(query)
    return f"https://www.google.com/travel/flights?q={encoded}"

# ── Navigation et extraction ────────────────────────────────────────────────
def _search_flights_browser(
    origin: str, destination: str, date: str,
    return_date: Optional[str], passengers: int, cabin: str,
) -> Tuple[str, str]:
    """Ouvre Google Flights via browser_control et attend les résultats."""
    try:
        from actions.browser_control import browser_control
    except ImportError:
        raise RuntimeError(
            "Le module browser_control est indisponible. "
            "Impossible d'ouvrir Google Flights."
        )
    url = _build_google_flights_url(origin, destination, date, return_date, passengers, cabin)
    print(f"[FlightFinder] 🌐 Ouverture : {url}")
    browser_control({"action": "go_to", "url": url})

    # Attente active : on sonde la page jusqu'à voir des prix/devise ou 15 s.
    raw = ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            raw = browser_control({"action": "get_text"}) or ""
        except Exception:
            raw = ""
        if raw and re.search(r"(?:\$|€|£|USD|EUR|GBP|\d{2,3}\s?(?:\$|€))", raw):
            break
        time.sleep(2)
    return (raw or ""), url

def _parse_flights_with_gemini(raw_text: str, origin: str, destination: str, date: str) -> List[dict]:
    api_key = _get_api_key()
    if not api_key:
        return []
    try:
        from google import genai as _genai
        from google.genai import types
        _client = _genai.Client(api_key=api_key)
        prompt = (
            f"Extraire les options de vol de {origin} à {destination} le {date} "
            f"à partir du texte de la page Google Flights ci-dessous.\n\n"
            f"{raw_text[:12000]}\n\n"
            f"Réponds UNIQUEMENT par un tableau JSON de 5 vols maximum :\n"
            f'[{{"airline": "...", "departure": "HH:MM", "arrival": "HH:MM",'
            f' "duration": "Xh Ym", "stops":0, "price": "...", "currency": "USD"}}]\n'
            f"Si aucun vol n'est trouvé, réponds : []"
        )
        resp = _client.models.generate_content(
            model=BALANCED_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="Extrais les informations de vols sous forme JSON. Aucun autre texte."
            ),
        )
        text = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        flights = json.loads(text)
        return flights if isinstance(flights, list) else []
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Échec parsing Gemini : {e}")
        return []

# ── Formatage des résultats ─────────────────────────────────────────────────
def _format_spoken(flights: List[dict], origin: str, destination: str, date: str) -> str:
    if not flights:
        return f"Je n'ai trouvé aucun vol de {origin} à {destination} le {date}."
    lines = [f"Voici les meilleurs vols de {origin} à {destination} le {date} :"]
    for i, f in enumerate(flights[:5], 1):
        airline   = f.get("airline", "compagnie inconnue")
        departure = f.get("departure", "--:--")
        arrival   = f.get("arrival", "--:--")
        duration  = f.get("duration", "")
        stops     = f.get("stops", 0)
        price     = f.get("price", "")
        currency  = f.get("currency", "")
        stop_str  = "direct" if stops == 0 else f"{stops} escale(s)"
        price_str = f"{price} {currency}".strip() if price else "prix indisponible"
        dur_str   = f", durée {duration}" if duration else ""
        lines.append(
            f"Option {i} : {airline}, départ {departure}, arrivée {arrival}{dur_str}, {stop_str}, {price_str}."
        )
    # Option la moins chère
    priced = [f for f in flights if f.get("price")]
    if priced:
        def _price_num(x):
            try:
                return int(re.sub(r"[^\d]", "", str(x["price"])) or "999999")
            except Exception:
                return 999999
        cheapest = min(priced, key=_price_num)
        lines.append(
            f"Le vol le moins cher est {cheapest.get('airline')} "
            f"à {cheapest.get('price')} {cheapest.get('currency', '')}."
        )
    return " ".join(lines)

def _format_text_report(
    flights: List[dict], origin: str, destination: str, date: str,
    return_date: Optional[str], page_url: str,
) -> str:
    lines = [
        "JARVIS — Résultat de recherche de vols",
        "─" * 50,
        f"Trajet    : {origin} → {destination}",
        f"Date      : {date}",
    ]
    if return_date:
        lines.append(f"Retour    : {return_date}")
    lines += [
        f"Recherché : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Source    : {page_url}",
        "─" * 50,
        "",
    ]
    if not flights:
        lines.append("Aucun vol trouvé.")
    else:
        for i, f in enumerate(flights, 1):
            stops = f.get("stops", 0)
            stop_str = "Direct" if stops == 0 else f"{stops} escale(s)"
            lines += [
                f"Vol {i} :",
                f"  Compagnie : {f.get('airline', 'N/A')}",
                f"  Départ    : {f.get('departure', 'N/A')}",
                f"  Arrivée   : {f.get('arrival', 'N/A')}",
                f"  Durée     : {f.get('duration', 'N/A')}",
                f"  Escales   : {stop_str}",
                f"  Prix      : {f.get('price', 'N/A')} {f.get('currency', '')}",
                "",
            ]
    return "\n".join(lines)

def _save_to_desktop(content: str, origin: str, destination: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"vols_{origin}_{destination}_{ts}.txt".replace(" ", "_")
    desktop  = Path.home() / "Desktop"
    if not desktop.exists():
        alt = Path.home() / "Bureau"
        if alt.exists():
            desktop = alt
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename
    filepath.write_text(content, encoding="utf-8")
    print(f"[FlightFinder] 💾 Sauvegardé : {filepath}")
    try:
        if is_windows():
            kit.spawn(["notepad.exe", str(filepath)])
        elif is_mac():
            kit.spawn(["open", "-t", str(filepath)])
        else:
            kit.spawn(['xdg-open', str(filepath)])
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Impossible d'ouvrir l'éditeur : {e}")
    return str(filepath)

# ── Parsing local multilingue ───────────────────────────────────────────────
def _parse_flight_request_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrait origine, destination, date aller, date retour, passagers, cabine.
    Retourne un dict ou None si pas assez d'infos.
    """
    original = text.strip()
    text = original.lower()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais|je cherche|trouve-moi|cherche)\b", "", text).strip()
    text = text.replace('"', ' ').replace("'", " ")

    origin = destination = None

    # Pattern "de X à Y" / "X vers Y" / "X - Y"
    m = re.search(r"(?:vols?\s+)?(?:de\s+)?([a-zà-ÿ\s]+?)\s+(?:à|vers|->|–|-)\s+([a-zà-ÿ\s]+)", text)
    if m:
        origin = m.group(1).strip()
        destination = m.group(2).strip()
        # Couper ce qui suit la destination si c'est une date ou un mot-clé
        destination = re.split(r"\s+(?:le|on|pour|aller|du|au)\b", destination)[0].strip()
        origin = re.split(r"\s+(?:le|on|pour|aller|du|au)\b", origin)[0].strip()

    # Pattern "Paris New York" (deux noms sans préposition)
    if not origin or not destination:
        m2 = re.search(r"([A-Za-zÀ-ÿ]+(?:\s[A-Za-zÀ-ÿ]+)?)\s+([A-Za-zÀ-ÿ]+(?:\s[A-Za-zÀ-ÿ]+)?)", original)
        if m2:
            origin = m2.group(1).strip()
            destination = m2.group(2).strip()

    if not origin or not destination:
        return None

    # Dates
    date_raw = return_raw = None
    date_patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{2}/\d{2}/\d{4})",
        r"(\d{1,2}\.\d{1,2}\.\d{4})",
        r"(\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre|\w+)\s*\d{0,4})",
    ]
    for pat in date_patterns:
        matches = re.findall(pat, text)
        if matches:
            date_raw = matches[0].strip()
            if len(matches) > 1:
                return_raw = matches[1].strip()
            break

    # Mention explicite "retour le ..."
    ret_match = re.search(r"retour\s+le\s+(.+)", text)
    if ret_match:
        return_raw = ret_match.group(1).strip()

    # Passagers
    passengers = 1
    pax_match = re.search(r"(\d+)\s*(?:passager|personne|adulte|voyageur|pax)", text)
    if pax_match:
        passengers = int(pax_match.group(1))

    # Cabine
    cabin = "economy"
    for word, code in [("business", "business"), ("affaires", "business"), ("premium", "premium"),
                       ("first", "first"), ("première", "first"), ("eco", "economy"), ("économique", "economy")]:
        if word in text:
            cabin = code
            break

    return {
        "origin": origin,
        "destination": destination,
        "date": date_raw or "",
        "return_date": return_raw,
        "passengers": passengers,
        "cabin": cabin,
    }

def _detect_flight_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Extrais les informations de vol de la phrase suivante en JSON :\n"
            f"{{'origin': 'Ville de départ', 'destination': 'Ville d\'arrivée', "
            f"'date': 'date aller (YYYY-MM-DD)', 'return_date': 'date retour (YYYY-MM-DD)', "
            f"'passengers': nombre, 'cabin': 'economy'/'premium'/'business'/'first'}}\n"
            f"Si un champ est absent, mets null. Phrase : \"{description}\"\n"
            "Réponds UNIQUEMENT par le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'{.*}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[FlightFinder] Erreur IA intent: {e}")
    return None

# ── Point d'entrée principal ────────────────────────────────────────────────
@kit.action("flight_finder")
def flight_finder(parameters: dict, player=None, speak=None) -> str:
    """
    Recherche de vols via Google Flights.
    Accepte des paramètres explicites (origin, destination, date, return_date,
    passengers, cabin) ou une phrase naturelle via 'description'.
    """
    params = parameters or {}
    description = params.get("description", "").strip()

    if description and not params.get("origin"):
        local = _parse_flight_request_locally(description)
        if local and local.get("origin") and local.get("destination"):
            params.update({k: v for k, v in local.items() if v is not None})
        else:
            ai = _detect_flight_intent_ai(description)
            if ai:
                params.update({k: v for k, v in ai.items() if v is not None})
            else:
                return "Je n'ai pas compris votre demande de vol. Précisez l'origine, la destination et la date."

    origin      = str(params.get("origin", "") or "").strip()
    destination = str(params.get("destination", "") or "").strip()
    date_raw    = str(params.get("date", "") or "").strip()
    return_raw  = str(params.get("return_date", "") or "").strip()
    passengers  = max(1, int(params.get("passengers", 1) or 1))
    cabin       = str(params.get("cabin", "economy") or "economy").strip().lower()
    save        = bool(params.get("save", True))

    if not origin or not destination:
        return "Veuillez fournir une ville de départ et une ville d'arrivée."
    if not date_raw:
        return "Veuillez préciser une date de départ."
    if cabin not in _CABIN_CODE:
        cabin = "economy"

    date        = _parse_date(date_raw)
    return_date = _parse_date(return_raw) if return_raw else None

    # Refuser les dates déjà passées.
    try:
        if datetime.strptime(date, "%Y-%m-%d").date() < datetime.now().date():
            return "La date de départ est déjà passée. Indiquez une date future."
    except ValueError:
        pass

    if player:
        try:
            player.write_log(f"[FlightFinder] {origin} → {destination} le {date}")
        except Exception:
            pass
    if speak:
        try:
            speak(f"Je cherche des vols de {origin} à {destination} le {date}.")
        except Exception:
            pass

    print(f"[FlightFinder] ▶️ {origin} → {destination} | {date}"
          f"{' → ' + return_date if return_date else ''}"
          f" | {cabin} | {passengers} passager(s)")

    try:
        raw_text, page_url = _search_flights_browser(
            origin, destination, date, return_date, passengers, cabin
        )
        if not raw_text:
            return ("Impossible de récupérer les données de vol. La page ne s'est "
                    "peut-être pas chargée correctement.")
        if speak:
            try:
                speak("Analyse des résultats en cours...")
            except Exception:
                pass

        flights = _parse_flights_with_gemini(raw_text, origin, destination, date)
        spoken  = _format_spoken(flights, origin, destination, date)
        if speak:
            try:
                speak(spoken)
            except Exception:
                pass

        result = spoken
        if save and flights:
            report     = _format_text_report(flights, origin, destination, date, return_date, page_url)
            saved_path = _save_to_desktop(report, origin, destination)
            result    += f"\n\nRapport détaillé sauvegardé sur le bureau : {saved_path}"
        return result
    except Exception as e:
        print(f"[FlightFinder] ❌ {e}")
        return f"La recherche de vol a échoué : {e}"

# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(flight_finder({"description": " ".join(sys.argv[1:])}))
    else:
        print('Usage: python flight_finder.py "cherche un vol de Paris à New York le 15 mars"')
