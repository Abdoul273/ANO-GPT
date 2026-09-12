"""Moteur de calcul astronomique local des heures de prière et gestionnaire d'annonces.

Calcul 100% local en pur Python (standard library), sans dépendance externe ni OAuth.
Intègre les méthodes de calcul astronomiques standard (MWL, UOIF, EGYPT, ISNA, MAKKAH, KARACHI).
"""
from __future__ import annotations

import datetime
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Configuration des méthodes de calcul ─────────────────────────────────────

@dataclass
class CalculationMethod:
    name: str
    fajr_angle: float
    isha_angle: float
    isha_interval_min: Optional[int] = None  # Ex: 90 min après Maghrib pour La Mecque
    maghrib_angle: Optional[float] = None
    maghrib_interval_min: Optional[int] = None
    description: str = ""


METHODS: Dict[str, CalculationMethod] = {
    "MWL": CalculationMethod(
        name="MWL",
        fajr_angle=18.0,
        isha_angle=17.0,
        description="Ligue Islamique Mondiale (recommandée en Europe et Afrique)",
    ),
    "UOIF": CalculationMethod(
        name="UOIF",
        fajr_angle=12.0,
        isha_angle=12.0,
        description="Union des Organisations Islamiques de France (angle 12°)",
    ),
    "EGYPT": CalculationMethod(
        name="EGYPT",
        fajr_angle=19.5,
        isha_angle=17.5,
        description="Autorité Générale Égyptienne de l'Arpentage (Afrique du Nord, Moyen-Orient)",
    ),
    "ISNA": CalculationMethod(
        name="ISNA",
        fajr_angle=15.0,
        isha_angle=15.0,
        description="Société Islamique d'Amérique du Nord",
    ),
    "MAKKAH": CalculationMethod(
        name="MAKKAH",
        fajr_angle=18.5,
        isha_angle=0.0,
        isha_interval_min=90,
        description="Umm al-Qura, La Mecque (Fajr 18.5°, Isha 90 min après Maghrib)",
    ),
    "KARACHI": CalculationMethod(
        name="KARACHI",
        fajr_angle=18.0,
        isha_angle=18.0,
        description="Université des Sciences Islamiques, Karachi",
    ),
}

DEFAULT_METHOD = "MWL"
PRAYER_NAMES = ("fajr", "dhuhr", "asr", "maghrib", "isha")
PRAYER_LABELS = {
    "fajr": "Fajr (Aube)",
    "sunrise": "Chourouk (Lever du soleil)",
    "dhuhr": "Dhuhr (Midi solaire)",
    "asr": "Asr (Après-midi)",
    "maghrib": "Maghrib (Coucher du soleil)",
    "isha": "Isha (Nuit)",
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance géodésique en km entre deux points (Haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


# ── Calculs astronomiques solaires ──────────────────────────────────────────

def _julian_date(year: int, month: int, day: int) -> float:
    """Calcul du Jour Julien à 0h temps universel pour une date grégorienne."""
    if month <= 2:
        year -= 1
        month += 12
    a = math.floor(year / 100)
    b = 2 - a + math.floor(a / 4)
    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def _sun_coordinates(jd: float) -> Tuple[float, float]:
    """Coordonnées solaires (déclinaison en degrés, équation du temps en heures)."""
    d = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    l_ecl = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = math.radians(23.439 - 0.00000036 * d)
    ra = math.degrees(math.atan2(math.cos(e) * math.sin(l_ecl), math.cos(l_ecl))) / 15.0
    ra = (ra + 24) % 24
    dec = math.degrees(math.asin(math.sin(e) * math.sin(l_ecl)))
    eqt = (q / 15.0) - ra
    if eqt > 12.0:
        eqt -= 24.0
    elif eqt < -12.0:
        eqt += 24.0
    return dec, eqt


def _hour_angle(alpha: float, lat: float, dec: float) -> Optional[float]:
    """Angle horaire en heures pour une altitude donnée (alpha en degrés)."""
    phi_rad = math.radians(lat)
    dec_rad = math.radians(dec)
    alpha_rad = math.radians(alpha)
    denom = math.cos(phi_rad) * math.cos(dec_rad)
    if abs(denom) < 1e-9:
        return None
    cos_h = (math.sin(alpha_rad) - math.sin(phi_rad) * math.sin(dec_rad)) / denom
    if cos_h > 1.0:
        return None  # Le soleil n'atteint jamais cette altitude (nuit polaire)
    if cos_h < -1.0:
        return None  # Le soleil est toujours au-dessus (soleil de minuit)
    return math.degrees(math.acos(cos_h)) / 15.0


def calculate_prayer_times(
    lat: float,
    lon: float,
    target_date: datetime.date,
    tz_offset_hours: float,
    method: CalculationMethod | str = DEFAULT_METHOD,
    asr_hanafi: bool = False,
    dhuhr_buffer_min: float = 1.0,
) -> Dict[str, datetime.datetime]:
    """Calcule les heures de prière pour une latitude, longitude et date données.

    Renvoie un dictionnaire avec les 5 prières + 'sunrise' en objets datetime conscients.
    """
    if isinstance(method, str):
        method = METHODS.get(method.upper(), METHODS[DEFAULT_METHOD])

    jd = _julian_date(target_date.year, target_date.month, target_date.day)
    dec, eqt = _sun_coordinates(jd + 0.5)

    # Midi solaire (Transit)
    noon = 12.0 + tz_offset_hours - (lon / 15.0) - eqt

    # Lever et coucher du soleil (-0.8333° pour réfraction atmosphérique et diamètre apparent)
    h_sun = _hour_angle(-0.8333, lat, dec)
    if h_sun is None:
        # Repli haute latitude : approximations proportionnelles
        h_sun = 6.0

    sunrise_h = noon - h_sun
    sunset_h = noon + h_sun
    night_duration = (24.0 - (sunset_h - sunrise_h)) % 24.0

    # 1. Fajr
    h_fajr = _hour_angle(-method.fajr_angle, lat, dec)
    if h_fajr is None:
        # Règle de sécurité haute latitude : angle-based / 1/60th
        fajr_h = sunrise_h - (method.fajr_angle / 60.0) * night_duration
    else:
        fajr_h = noon - h_fajr

    # 2. Dhuhr (midi solaire + buffer de franchissement du zénith)
    dhuhr_h = noon + (dhuhr_buffer_min / 60.0)

    # 3. Asr (Ombre simple = Shafi'i/Maliki/Hanbali ou Ombre double = Hanafi)
    shadow_factor = 2.0 if asr_hanafi else 1.0
    phi_rad = math.radians(lat)
    dec_rad = math.radians(dec)
    asr_alt = math.degrees(math.atan(1.0 / (shadow_factor + math.tan(abs(phi_rad - dec_rad)))))
    h_asr = _hour_angle(asr_alt, lat, dec)
    if h_asr is None:
        asr_h = noon + 3.0
    else:
        asr_h = noon + h_asr

    # 4. Maghrib
    if method.maghrib_interval_min is not None:
        maghrib_h = sunset_h + (method.maghrib_interval_min / 60.0)
    elif method.maghrib_angle is not None:
        h_maghrib = _hour_angle(-method.maghrib_angle, lat, dec)
        maghrib_h = noon + (h_maghrib if h_maghrib is not None else h_sun)
    else:
        maghrib_h = sunset_h + (1.0 / 60.0)  # Coucher du soleil + 1 min

    # 5. Isha
    if method.isha_interval_min is not None:
        isha_h = maghrib_h + (method.isha_interval_min / 60.0)
    else:
        h_isha = _hour_angle(-method.isha_angle, lat, dec)
        if h_isha is None:
            isha_h = sunset_h + (method.isha_angle / 60.0) * night_duration
        else:
            isha_h = noon + h_isha

    # Haute latitude : éviter le chevauchement avec la nuit suivante / nuit trop courte
    if isha_h - sunset_h > night_duration / 2.0:
        isha_h = sunset_h + (night_duration / 2.0)
    if sunrise_h - fajr_h > night_duration / 2.0:
        fajr_h = sunrise_h - (night_duration / 2.0)
    if isha_h < maghrib_h:
        isha_h = maghrib_h + (30.0 / 60.0)

    # Conversion en datetime avec fuseau horaire
    tz = datetime.timezone(datetime.timedelta(hours=tz_offset_hours))

    def _to_dt(hours_float: float) -> datetime.datetime:
        day_offset = int(math.floor(hours_float / 24.0))
        rem_hours = hours_float - (day_offset * 24.0)
        h = int(rem_hours)
        m = int(round((rem_hours - h) * 60.0))
        s = 0
        if m >= 60:
            h += 1
            m = 0
        if h >= 24:
            day_offset += 1
            h -= 24
        base_date = target_date + datetime.timedelta(days=day_offset)
        return datetime.datetime(
            base_date.year, base_date.month, base_date.day, h, m, s, tzinfo=tz
        )

    return {
        "fajr": _to_dt(fajr_h),
        "sunrise": _to_dt(sunrise_h),
        "dhuhr": _to_dt(dhuhr_h),
        "asr": _to_dt(asr_h),
        "maghrib": _to_dt(maghrib_h),
        "isha": _to_dt(isha_h),
    }


# ── Modèle de Configuration et Persistance ───────────────────────────────────

@dataclass
class PrayerConfig:
    enabled: bool = True
    method: str = DEFAULT_METHOD
    asr_hanafi: bool = False
    dhuhr_buffer_min: float = 1.0
    voice_tone: str = "calm_night"
    allow_fajr_in_quiet_hours: bool = True
    prayers: Dict[str, bool] = field(default_factory=lambda: {
        "fajr": True,
        "dhuhr": True,
        "asr": True,
        "maghrib": True,
        "isha": True,
    })
    last_lat: Optional[float] = None
    last_lon: Optional[float] = None
    last_city: Optional[str] = None


class PrayerManager:
    """Gestionnaire d'état, de calcul et de suivi des heures de prière pour ANO-GPT."""

    CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "prayer_config.json"
    STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "prayer_state.json"

    # Seuil de déplacement (en km) pour recalculer immédiatement
    DISPLACEMENT_THRESHOLD_KM = 10.0

    def __init__(self, config_path: Path | None = None, state_path: Path | None = None):
        self._config_path = config_path or self.CONFIG_FILE
        self._state_path = state_path or self.STATE_FILE
        self.config = self._load_config()
        self._announced_today: set[str] = self._load_state()
        self._cached_schedule: Dict[str, datetime.datetime] = {}
        self._cached_date: Optional[datetime.date] = None
        self._cached_coords: Optional[Tuple[float, float]] = None

    def _load_config(self) -> PrayerConfig:
        try:
            if self._config_path.exists():
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                prayers = dict(data.get("prayers", {}))
                for name in PRAYER_NAMES:
                    if name not in prayers:
                        prayers[name] = True
                return PrayerConfig(
                    enabled=bool(data.get("enabled", True)),
                    method=str(data.get("method", DEFAULT_METHOD)).upper(),
                    asr_hanafi=bool(data.get("asr_hanafi", False)),
                    dhuhr_buffer_min=float(data.get("dhuhr_buffer_min", 1.0)),
                    voice_tone=str(data.get("voice_tone", "calm_night")),
                    allow_fajr_in_quiet_hours=bool(data.get("allow_fajr_in_quiet_hours", True)),
                    prayers=prayers,
                    last_lat=data.get("last_lat"),
                    last_lon=data.get("last_lon"),
                    last_city=data.get("last_city"),
                )
        except Exception:
            pass
        return PrayerConfig()

    def save_config(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self.config)
            self._config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[Prayer] Erreur sauvegarde config : {exc}")

    def _load_state(self) -> set[str]:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                today_str = datetime.date.today().isoformat()
                if data.get("date") == today_str:
                    return set(data.get("announced", []))
        except Exception:
            pass
        return set()

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            today_str = datetime.date.today().isoformat()
            payload = {
                "date": today_str,
                "announced": sorted(list(self._announced_today)),
                "updated_at": time.time(),
            }
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[Prayer] Erreur sauvegarde état : {exc}")

    def resolve_coords(self) -> Tuple[float, float, str]:
        """Obtient les coordonnées via core.geolocation ou retombe sur la config/défaut."""
        try:
            from core.geolocation import get_user_coords, get_user_location
            coords = get_user_coords()
            loc = get_user_location()
            city = loc.get("city") or loc.get("country_name") or "Localité inconnue"
            if coords and len(coords) == 2 and coords[0] is not None:
                lat, lon = float(coords[0]), float(coords[1])
                self.config.last_lat = lat
                self.config.last_lon = lon
                self.config.last_city = city
                return lat, lon, city
        except Exception:
            pass

        # Repli sur les coordonnées enregistrées ou Paris par défaut
        lat = self.config.last_lat if self.config.last_lat is not None else 48.8566
        lon = self.config.last_lon if self.config.last_lon is not None else 2.3522
        city = self.config.last_city or "Paris"
        return lat, lon, city

    def get_schedule(
        self,
        target_date: Optional[datetime.date] = None,
        force_refresh: bool = False,
        now_ref: Optional[datetime.datetime] = None,
        tz_offset_hours: Optional[float] = None,
    ) -> Dict[str, datetime.datetime]:
        """Renvoie les heures de prière pour la date demandée (défaut: aujourd'hui)."""
        ref = now_ref or datetime.datetime.now().astimezone()
        today = target_date or ref.date()

        # Nettoyage automatique de l'état quotidien au changement de date (minuit)
        if self._cached_date != today:
            self._announced_today = self._load_state()

        lat, lon, city = self.resolve_coords()
        if tz_offset_hours is None:
            tz_offset = ref.utcoffset().total_seconds() / 3600.0 if ref.utcoffset() else 0.0
        else:
            tz_offset = float(tz_offset_hours)

        # Vérification du déplacement géographique
        position_changed = False
        if self._cached_coords is not None:
            dist = haversine_km(lat, lon, self._cached_coords[0], self._cached_coords[1])
            if dist > self.DISPLACEMENT_THRESHOLD_KM:
                position_changed = True

        if (
            not force_refresh
            and not position_changed
            and self._cached_date == today
            and self._cached_schedule
        ):
            return self._cached_schedule

        schedule = calculate_prayer_times(
            lat=lat,
            lon=lon,
            target_date=today,
            tz_offset_hours=tz_offset,
            method=self.config.method,
            asr_hanafi=self.config.asr_hanafi,
            dhuhr_buffer_min=self.config.dhuhr_buffer_min,
        )
        self._cached_schedule = schedule
        self._cached_date = today
        self._cached_coords = (lat, lon)
        return schedule

    def get_next_prayer(
        self, now: Optional[datetime.datetime] = None
    ) -> Tuple[str, datetime.datetime, datetime.timedelta]:
        """Renvoie (nom_prière, heure_prière, temps_restant).

        Si toutes les prières du jour sont passées, calcule automatiquement
        Fajr du lendemain.
        """
        now = now or datetime.datetime.now().astimezone()
        today_schedule = self.get_schedule(target_date=now.date(), now_ref=now)

        for name in PRAYER_NAMES:
            prayer_dt = today_schedule[name]
            if prayer_dt > now:
                return name, prayer_dt, prayer_dt - now

        # Toutes les prières d'aujourd'hui sont passées -> prochaine = Fajr demain
        tomorrow = now.date() + datetime.timedelta(days=1)
        tomorrow_schedule = self.get_schedule(target_date=tomorrow, now_ref=now)
        fajr_dt = tomorrow_schedule["fajr"]
        return "fajr", fajr_dt, fajr_dt - now

    def set_prayer_enabled(self, prayer: str, enabled: bool) -> bool:
        """Active ou désactive une prière spécifique."""
        prayer = prayer.strip().lower()
        if prayer not in PRAYER_NAMES:
            return False
        self.config.prayers[prayer] = bool(enabled)
        self.save_config()
        return True

    def toggle_prayer(self, prayer: str, enabled: Optional[bool] = None) -> bool:
        """Active, désactive ou inverse l'état d'activation d'une prière."""
        prayer = prayer.strip().lower()
        if prayer not in PRAYER_NAMES:
            return False
        if enabled is None:
            new_val = not self.config.prayers.get(prayer, True)
        else:
            new_val = bool(enabled)
        return self.set_prayer_enabled(prayer, new_val)

    def set_all_enabled(self, enabled: bool) -> None:
        """Active ou coupe tous les rappels de prière."""
        self.config.enabled = bool(enabled)
        self.save_config()

    def set_calculation_method(self, method_name: str) -> bool:
        """Change la convention astronomique de calcul."""
        m = method_name.strip().upper()
        if m not in METHODS:
            return False
        self.config.method = m
        self.save_config()
        self._cached_schedule.clear()
        return True

    def is_prayer_enabled(self, prayer: str) -> bool:
        """Indique si l'annonce de cette prière est activée."""
        if not self.config.enabled:
            return False
        return self.config.prayers.get(prayer.lower(), True)

    def check_and_produce_announcement(
        self, now: Optional[datetime.datetime] = None
    ) -> Optional[Tuple[str, str, datetime.datetime]]:
        """Vérifie si une prière activée est arrivée à échéance et doit être annoncée.

        Renvoie (message_vocal, nom_prière, heure_dt) ou None.
        Marque atomiquement la prière comme annoncée pour aujourd'hui.
        """
        if not self.config.enabled:
            return None

        now = now or datetime.datetime.now().astimezone()
        schedule = self.get_schedule(target_date=now.date())
        _, _, city = self.resolve_coords()

        for name in PRAYER_NAMES:
            if not self.is_prayer_enabled(name):
                continue

            if name in self._announced_today:
                continue

            prayer_dt = schedule.get(name)
            if prayer_dt is None:
                continue

            # Détection du moment d'annonce :
            # La prière est annoncée dès qu'on atteint ou dépasse son heure,
            # dans une fenêtre de tolérance de 15 minutes (pour rattraper si le PC était occupé).
            time_diff = (now - prayer_dt).total_seconds()
            if 0.0 <= time_diff <= 900.0:
                # Marquer comme annoncé
                self._announced_today.add(name)
                self._save_state()

                display_name = name.capitalize()
                message = f"Il est l'heure de la prière de {display_name}."
                return message, name, prayer_dt

        return None

    @staticmethod
    def format_time_remaining(delta: datetime.timedelta) -> str:
        """Formate une durée restante en français convivial."""
        total_seconds = int(max(0, delta.total_seconds()))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0 and minutes > 0:
            return f"{hours} heure{'s' if hours > 1 else ''} et {minutes} minute{'s' if minutes > 1 else ''}"
        elif hours > 0:
            return f"{hours} heure{'s' if hours > 1 else ''}"
        elif minutes > 0:
            return f"{minutes} minute{'s' if minutes > 1 else ''}"
        else:
            return "quelques instants"
