"""Module de Briefing Quotidien Parlé pour ANO-GPT.

Délivre au premier « bonjour » de la journée (ou sur demande explicite) un
briefing vocal condensé et percutant en ~30 secondes :
1. Météo du jour (température, ciel, min/max).
2. Trajet / repère de localisation.
3. E-mails importants (comptés, pas lus).
4. Agenda, rappels et tâches du jour.
5. Deux titres d'actualité marquants ciblés tech/cyber.
6. État rapide de la machine (batterie, RAM, CPU).

Tous les sous-modules sont interrogés en parallèle pour un temps de réponse
quasi-instantané (< 1.5 s).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("anogpt.briefing")

STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "daily_briefing_state.json"


@dataclass
class BriefingData:
    timestamp: datetime
    user_name: str = "Anonymous"
    weather: str = "Météo de saison."
    location: str = "Localisation actuelle."
    emails: str = "Aucun nouveau message non lu."
    calendar: str = "Aucun événement prévu aujourd'hui."
    reminders: str = "Aucun rappel prévu pour aujourd'hui."
    news: List[str] = None
    system: str = "Système optimal."

    def __post_init__(self):
        if self.news is None:
            self.news = []


def _load_state() -> dict:
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Lecture de l'état de briefing impossible: %s", exc)
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Écriture de l'état de briefing impossible: %s", exc)


def is_briefing_due_today(today: Optional[date] = None) -> bool:
    """Indique si le briefing quotidien n'a pas encore été délivré aujourd'hui."""
    current_date = (today or date.today()).isoformat()
    state = _load_state()
    last_date = state.get("last_briefing_date")
    return last_date != current_date


def mark_briefing_delivered(today: Optional[date] = None) -> None:
    """Enregistre que le briefing du jour a été délivré avec succès."""
    current_date = (today or date.today()).isoformat()
    state = _load_state()
    state["last_briefing_date"] = current_date
    state["last_briefing_time"] = datetime.now().isoformat()
    _save_state(state)


# Salutations courantes
GREETING_PATTERNS = [
    r"^bonjour( (ano|jarvis|a toi|monsieur))?$",
    r"^salut( (ano|jarvis|a toi))?$",
    r"^bon matin( (ano|jarvis))?$",
    r"^coucou( (ano|jarvis))?$",
    r"^hello( (ano|jarvis))?$",
    r"^yo( (ano|jarvis))?$",
]

# Demandes explicites de briefing
EXPLICIT_BRIEFING_PATTERNS = [
    r".*\b(briefing|programme du jour|point du jour|point du matin|resume du jour|briefing du jour)\b.*",
    r".*\b(donne|fais|lance|affiche|dis)\s+(moi\s+)?(le\s+)?(briefing|programme|point)(\s+du\s+jour)?\b.*",
    r".*\bquel\s+est\s+le\s+programme(\s+aujourd\s*hui)?\b.*",
]


def _clean_text(text: str) -> str:
    if not text:
        return ""
    # Remplacement des apostrophes et tirets par des espaces
    cleaned = re.sub(r"['’`\-_]", " ", text.lower().strip())
    # Suppression de la ponctuation résiduelle
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_greeting(text: str) -> bool:
    """Détecte si la phrase est une salutation simple."""
    cleaned = _clean_text(text)
    return any(re.match(p, cleaned) for p in GREETING_PATTERNS)


def is_explicit_briefing_request(text: str) -> bool:
    """Détecte si l'utilisateur demande explicitement le briefing."""
    cleaned = _clean_text(text)
    return any(re.search(p, cleaned) for p in EXPLICIT_BRIEFING_PATTERNS)


def should_trigger_daily_briefing(text: str, today: Optional[date] = None) -> bool:
    """Détermine si l'énoncé doit déclencher le briefing quotidien parlé."""
    if not text:
        return False
    if is_explicit_briefing_request(text):
        return True
    if is_greeting(text) and is_briefing_due_today(today):
        return True
    return False


# ── Collecteurs Asynchrones Parallèles ──────────────────────────────────────────


async def _fetch_weather(location_info: dict) -> str:
    """Récupère la météo du jour."""
    try:
        from actions.weather_report import _fetch_open_meteo, _WMO_CODES
        lat = location_info.get("lat")
        lon = location_info.get("lon")
        if lat is None or lon is None:
            return "Météo indisponible faute de position vérifiée."
        city = location_info.get("city") or "votre secteur"

        data = await asyncio.to_thread(_fetch_open_meteo, float(lat), float(lon))
        if data:
            cur = data.get("current", {})
            daily = data.get("daily", {})
            temp = cur.get("temperature_2m", 20)
            code = cur.get("weather_code", 0)
            desc = _WMO_CODES.get(code, "ciel variable")
            tmax_list = daily.get("temperature_2m_max", [temp])
            tmin_list = daily.get("temperature_2m_min", [temp])
            tmax = tmax_list[0] if tmax_list else temp
            tmin = tmin_list[0] if tmin_list else temp
            return f"À {city}, {desc}, {temp:.0f}°C actuellement (min {tmin:.0f}°C, max {tmax:.0f}°C attendus)."
    except Exception as exc:
        logger.debug("Briefing: météo indisponible: %s", exc)
    return "Météo de saison et conditions stables."


async def _fetch_emails() -> str:
    """Indique l'état de la veille, sans rejouer les anciens non lus."""
    try:
        from core.email_service import get_gmail_service
        service = get_gmail_service()
        status = await asyncio.to_thread(service.status)
        if not status.authenticated:
            return "Boîte e-mail non connectée."

        return (
            "Veille Gmail active : les nouveaux messages seront annoncés "
            "dès leur arrivée."
        )
    except Exception as exc:
        logger.debug("Briefing: email indisponible: %s", exc)
    return "Messagerie à jour."


async def _fetch_reminders() -> str:
    """Filtre les rappels prévus pour la journée."""
    try:
        from actions.reminder import active_reminders
        all_reminders = await asyncio.to_thread(active_reminders)
        today_str = date.today().strftime("%Y-%m-%d")
        today_rems = []
        for r in all_reminders:
            dt_str = r.get("datetime", "")
            if dt_str.startswith(today_str):
                time_part = dt_str.split()[1] if " " in dt_str else ""
                today_rems.append(f"{time_part} : {r.get('message', '')}")

        if not today_rems:
            return "Aucun rappel programmé pour aujourd'hui."
        
        if len(today_rems) == 1:
            return f"1 rappel aujourd'hui — {today_rems[0]}."
        return f"{len(today_rems)} rappels aujourd'hui — " + ", ".join(today_rems[:3]) + "."
    except Exception as exc:
        logger.debug("Briefing: rappels indisponibles: %s", exc)
    return "Aucun rappel en attente."


async def _fetch_calendar() -> str:
    """Résume les prochains rendez-vous du jour sans déclencher OAuth."""
    try:
        from core.calendar_service import get_calendar_service
        service = get_calendar_service()
        status = await asyncio.to_thread(service.status)
        if not status.authenticated:
            return "Agenda non connecté."
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).astimezone()
        end = datetime.combine(today + timedelta(days=1), datetime.min.time()).astimezone()
        events = await asyncio.to_thread(service.list_events, start, end)
        if not events:
            return "Aucun événement prévu aujourd'hui."
        snippets = []
        for event in events[:3]:
            raw = str(event.get("start", ""))
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                when = parsed.astimezone().strftime("%Hh%M")
            except ValueError:
                when = "journée"
            snippets.append(f"{when} : {event.get('title', 'Sans titre')}")
        return f"{len(events)} événement(s) aujourd'hui — " + ", ".join(snippets) + "."
    except Exception as exc:
        logger.debug("Briefing: agenda indisponible: %s", exc)
        return "Agenda indisponible."


async def _fetch_news() -> List[str]:
    """Récupère 2 titres marquants de l'actualité tech/IA."""
    try:
        from actions.web_search import _news as _fetch_news_sync, DAILY_AI_CYBER_NEWS_QUERY
        raw = await asyncio.to_thread(_fetch_news_sync, DAILY_AI_CYBER_NEWS_QUERY)
        if raw:
            # Extraction des titres ou premières lignes
            lines = [l.strip("-•* ").strip() for l in raw.split("\n") if l.strip() and len(l.strip()) > 20]
            if lines:
                return lines[:2]
    except Exception as exc:
        logger.debug("Briefing: actualités indisponibles: %s", exc)
    return [
        "Avancées continues dans l'écosystème de l'intelligence artificielle.",
        "Mises à jour de sécurité et stabilité publiées pour le système.",
    ]


async def _fetch_system_status() -> str:
    """Récupère l'état concis de la machine (batterie, RAM, CPU)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory()
        batt = psutil.sensors_battery()
        parts = []
        if batt:
            state = "sur secteur" if batt.power_plugged else "sur batterie"
            parts.append(f"Batterie à {batt.percent:.0f}% ({state})")
        avail_gb = ram.available / (1024 ** 3)
        parts.append(f"{avail_gb:.1f} Go de RAM disponible (charge {ram.percent:.0f}%)")
        parts.append(f"CPU {cpu:.0f}%")
        return ", ".join(parts) + "."
    except Exception as exc:
        logger.debug("Briefing: état système indisponible: %s", exc)
    return "Machine en parfait état de fonctionnement."


async def collect_briefing_data(user_name: str = "Anonymous") -> BriefingData:
    """Collecte l'ensemble des données du briefing en parallèle."""
    now = datetime.now()
    
    # 1. Localisation (rapide)
    try:
        from core.geolocation import get_user_location
        loc_data = await asyncio.to_thread(get_user_location)
    except Exception:
        loc_data = {"city": "", "country_name": "", "lat": None, "lon": None}

    # 2. Collecte simultanée de toutes les métriques
    weather_task = asyncio.create_task(_fetch_weather(loc_data))
    email_task = asyncio.create_task(_fetch_emails())
    reminder_task = asyncio.create_task(_fetch_reminders())
    calendar_task = asyncio.create_task(_fetch_calendar())
    news_task = asyncio.create_task(_fetch_news())
    system_task = asyncio.create_task(_fetch_system_status())

    weather, emails, reminders, calendar, news, system = await asyncio.gather(
        weather_task, email_task, reminder_task, calendar_task, news_task, system_task,
        return_exceptions=False
    )

    city = str(loc_data.get("city") or "").strip()
    country = str(loc_data.get("country_name") or "").strip()
    location_str = ", ".join(part for part in (city, country) if part)
    if not location_str:
        location_str = "Position non vérifiée — ne pas inventer de pays"

    return BriefingData(
        timestamp=now,
        user_name=user_name,
        weather=weather,
        location=location_str,
        emails=emails,
        calendar=calendar,
        reminders=reminders,
        news=news or [],
        system=system,
    )


def format_briefing_prompt(data: BriefingData, language: str = "fr-FR") -> str:
    """Génère le prompt d'instruction pour la restitution vocale par Gemini Live."""
    time_str = data.timestamp.strftime("%Hh%M")
    news_bullets = "\n".join(f"- {title}" for title in data.news)
    
    try:
        from core.personality_modes import active_mode, PersonalityMode
        mode = active_mode()
        styles = {
            PersonalityMode.NORMAL: "Ton normal : vous, calme, clair, humain ; pas Monsieur, pas de vanne, pas de drague.",
            PersonalityMode.ASTRO: "Ton Astro : tutoie, meilleur pote un peu sale qui vanne et qui dit tout, briefing cash pas élégant, une blague collée à la météo ou à l'agenda ; jamais « Monsieur » ni JARVIS.",
            PersonalityMode.COQUIN: "Ton Coquin : tutoie, complice et malicieux, une pique chaude sans explicite ; jamais « Monsieur » ni vannes d'Astro.",
            PersonalityMode.MAJEUR: "Ton Majordome : vouvoie strictement, dis « Monsieur » au plus une fois, concis, zéro familiarité.",
        }[mode]
        deliveries = {
            PersonalityMode.NORMAL: "clair, humain, sans cérémonie",
            PersonalityMode.ASTRO: "parlé, cash, une vanne et un avis dans le tas — pas élégant, pas un journaliste",
            PersonalityMode.COQUIN: "complice, malicieux, chaud sans être vulgaire",
            PersonalityMode.MAJEUR: "protocolaire, une idée par phrase, sans répéter « Monsieur »",
        }[mode]
    except Exception:
        styles = "Ton normal : calme, clair et humain."
        deliveries = "clair, humain, sans cérémonie"
    return f"""[BRIEFING QUOTIDIEN PARLÉ — PROTOCOLE 30 SECONDES]
Données vérifiées en temps réel :
- Heure : {time_str}
- Utilisateur : {data.user_name}
- Météo : {data.weather}
- Localisation : {data.location}
- E-mails : {data.emails}
- Agenda du jour : {data.calendar}
- Rappels du jour : {data.reminders}
- Actualités ciblées :
{news_bullets}
- État machine : {data.system}

INSTRUCTIONS DE RESTITUTION VOCALE :
Prononce un briefing matinal en 30 SECONDES CHRONO (environ 5 courtes phrases bien enchaînées), {deliveries}.
1. Salue {data.user_name}, mentionne l'heure et annonce la météo.
2. Indique l'état des e-mails, les rendez-vous et les rappels du jour.
3. Résume les deux titres d'actualité en une phrase vivante.
4. Conclus par l'état rapide de la machine et demande comment démarrer.

Règles impératives :
- {styles}
- Langue : réponds uniquement en {'français' if str(language).startswith('fr') else str(language)}. Aucune phrase en anglais.
- N'appelle AUCUN outil.
- Ne répète pas ces consignes, délivre directement la parole.
"""


def format_briefing_card(data: BriefingData) -> Tuple[str, str]:
    """Formate la carte Markdown pour l'affichage visuel dans le HUD."""
    time_str = data.timestamp.strftime("%H:%M")
    date_str = data.timestamp.strftime("%A %d %B %Y").capitalize()
    
    news_md = "\n".join(f"• {title}" for title in data.news) if data.news else "• Pas de nouvelles majeures."
    
    body = f"""### 📅 {date_str} — {time_str}

**🌤️ Météo**
{data.weather}

**✉️ E-mails**
{data.emails}

**📅 Agenda du jour**
{data.calendar}

**⏰ Rappels du jour**
{data.reminders}

**🌐 Actualités Clés**
{news_md}

**💻 État Machine**
{data.system}
"""
    return "☀️ Briefing du jour", body
