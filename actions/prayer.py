"""actions/prayer.py — Outil de consultation et de gestion des heures de prière (Adhan)."""
from __future__ import annotations

import datetime
from typing import Optional

from core.prayer_times import (
    METHODS,
    PRAYER_LABELS,
    PRAYER_NAMES,
    PrayerManager,
)

from core import action_kit as kit

_MANAGER: Optional[PrayerManager] = None


def get_prayer_manager() -> PrayerManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = PrayerManager()
    return _MANAGER


@kit.action("prayer_control")
def prayer_control(parameters: dict | None = None) -> str:
    """Point d'entrée pour la consultation et le réglage des heures de prière."""
    params = parameters or {}
    action = str(params.get("action", "next") or "next").strip().casefold()
    manager = get_prayer_manager()

    try:
        if action in {"next", "prochaine", "suivante"}:
            next_name, next_dt, remaining = manager.get_next_prayer()
            display_name = next_name.capitalize()
            time_str = next_dt.strftime("%H:%M")
            remaining_str = manager.format_time_remaining(remaining)
            _, _, city = manager.resolve_coords()
            loc_suffix = f" à {city}" if city else ""
            enabled = manager.is_prayer_enabled(next_name)
            status_suffix = "" if enabled else " (rappel vocal désactivé)"
            return f"La prochaine prière est {display_name} à {time_str}{loc_suffix}, dans {remaining_str}{status_suffix}."

        if action in {"today", "list", "horaires", "agenda"}:
            now = datetime.datetime.now().astimezone()
            schedule = manager.get_schedule(target_date=now.date())
            _, _, city = manager.resolve_coords()
            next_name, _, remaining = manager.get_next_prayer()

            lines = [
                f"Heures de prière aujourd'hui ({city}, convention {manager.config.method}, "
                f"position : {manager.position_source}) :"
            ]
            order = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
            for name in order:
                dt = schedule.get(name)
                if not dt:
                    continue
                label = PRAYER_LABELS.get(name, name.capitalize())
                time_str = dt.strftime("%H:%M")
                marker = " ◄ prochaine" if name == next_name else ""
                state_icon = ""
                if name in PRAYER_NAMES:
                    state_icon = " [actif]" if manager.is_prayer_enabled(name) else " [désactivé]"
                lines.append(f"• {label} : {time_str}{state_icon}{marker}")

            lines.append(f"\nProchaine prière : {next_name.capitalize()} dans {manager.format_time_remaining(remaining)}.")
            return "\n".join(lines)

        if action in {"toggle", "activer", "desactiver", "désactiver"}:
            prayer_input = str(params.get("prayer") or params.get("nom") or "").strip().lower()
            if not prayer_input:
                return "Précisez le nom de la prière à modifier (Fajr, Dhuhr, Asr, Maghrib ou Isha)."

            matched_prayer = None
            for p in PRAYER_NAMES:
                if p in prayer_input:
                    matched_prayer = p
                    break

            if not matched_prayer:
                return f"Prière non reconnue ('{prayer_input}'). Choisissez parmi : Fajr, Dhuhr, Asr, Maghrib, Isha."

            enabled_val = params.get("enabled")
            if enabled_val is None:
                # Inversion de l'état actuel
                new_state = not manager.is_prayer_enabled(matched_prayer)
            else:
                if isinstance(enabled_val, bool):
                    new_state = enabled_val
                else:
                    new_state = str(enabled_val).strip().lower() in {"true", "1", "actif", "active", "activer", "oui", "yes"}

            manager.set_prayer_enabled(matched_prayer, new_state)
            display_name = matched_prayer.capitalize()
            state_word = "activé" if new_state else "désactivé"
            return f"Le rappel vocal automatique pour la prière de {display_name} a été {state_word}."

        if action in {"toggle_all", "activer_tout", "couper_tout", "désactiver_tout"}:
            enabled_val = params.get("enabled")
            if enabled_val is None:
                new_state = not manager.config.enabled
            else:
                if isinstance(enabled_val, bool):
                    new_state = enabled_val
                else:
                    new_state = str(enabled_val).strip().lower() in {"true", "1", "actif", "active", "activer", "oui", "yes"}

            manager.set_all_enabled(new_state)
            state_word = "activés" if new_state else "désactivés"
            return f"Tous les rappels vocaux d'heures de prière ont été {state_word}."

        if action in {"set_method", "convention", "methode", "méthode"}:
            method_input = str(params.get("method") or params.get("convention") or "").strip().upper()
            if not method_input or method_input not in METHODS:
                avail = ", ".join(METHODS.keys())
                return f"Méthode invalide. Choisissez parmi les conventions supportées : {avail}."

            manager.set_calculation_method(method_input)
            desc = METHODS[method_input].description
            return f"Méthode de calcul mise à jour : {method_input} ({desc})."

        if action in {"status", "config"}:
            _, _, city = manager.resolve_coords()
            status_word = "activé" if manager.config.enabled else "désactivé globalement"
            desc = METHODS.get(manager.config.method, METHODS[manager.config.method]).description
            lines = [
                f"Statut du module de prière : {status_word}",
                f"• Ville / Localité : {city}",
                f"• Source de position : {manager.position_source}",
                f"• Convention de calcul : {manager.config.method} ({desc})",
                f"• Heures calmes (Fajr autorisé) : {'Oui' if manager.config.allow_fajr_in_quiet_hours else 'Non'}",
                "• État individuel des prières :",
            ]
            for p in PRAYER_NAMES:
                en = "Activé" if manager.is_prayer_enabled(p) else "Désactivé"
                lines.append(f"  - {p.capitalize()} : {en}")
            return "\n".join(lines)

        return f"Action inconnue ('{action}'). Actions disponibles : next, today, toggle, toggle_all, set_method, status."

    except Exception as exc:
        return f"Erreur lors de la gestion des heures de prière : {exc}"
