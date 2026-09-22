"""Proactivité ANO-GPT : habitudes, timers, briefing, Gmail, bouclier.

Concurrence
-----------
* **asyncio** — chaque ``_run_*_watch`` est une tâche du TaskGroup de session.
* **asyncio.to_thread** — évaluation d'habitudes, collecte du briefing,
  I/O Gmail : jamais sur la boucle audio.
* **Qt** — cartes HUD (``show_card``) depuis la boucle asyncio via le
  thread principal ; pas d'animation ici.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Protocol

from core.daily_briefing import (
    collect_briefing_data,
    format_briefing_card,
    format_briefing_prompt,
    mark_briefing_delivered,
)
from core.habit_model import suggestion_text
from core.thread_pool import get_thread_pool
from core import routines, tool_stats

from actions.proactive import desktop_blocks_proactivity, in_quiet_hours

logger = logging.getLogger("anogpt.proactive")


def startup_greeting(now: datetime | None = None) -> str:
    """Salutation courte, locale et cohérente avec le mode actif.

    Elle ne passe pas par le modèle : ANO parle immédiatement après la
    connexion, sans lancer un briefing, une recherche ou une liste d'alertes.
    """
    from core.personality_modes import PersonalityMode, active_mode

    now = now or datetime.now()
    mode = active_mode()
    if mode is PersonalityMode.MAJEUR:
        return "Bonjour, Monsieur." if 5 <= now.hour < 18 else "Bon retour, Monsieur."
    if mode is PersonalityMode.ASTRO:
        return "Salut gros."
    if mode is PersonalityMode.COQUIN:
        return "Salut toi."
    return "Bonjour, Anonymous." if 5 <= now.hour < 18 else "Bon retour, Anonymous."


class ProactiveHost(Protocol):
    ui: Any
    session: Any
    _proactive: Any
    _habits: Any
    _timers: Any
    _focus_guard: Any
    _dashboard: Any

    def speak(self, text: str) -> None: ...
    async def _submit_text_turn(self, text: str, timeout_s: float = 90.0) -> bool: ...
    def interrupt(self) -> None: ...


class ProactiveEngine:
    """HabitModel, TimerService, DistractionGuard, briefing, Gmail watch.

    Les méthodes sont liées à l'hôte ``JarvisLive``.
    """

    def _import_existing_habits(self) -> None:
        """Réutilise une fois les journaux déjà présents, sans contenu sensible."""
        events: list[tuple[str, datetime]] = []
        cutoff = datetime.now().timestamp() - 21 * 86400
        try:
            from actions import launch_tracker
            for entry in launch_tracker._load_raw():
                stamp = float(entry.get("opened_at") or 0)
                app = str(entry.get("app") or "").strip()
                if app and stamp >= cutoff:
                    events.append((f"app:{app}", datetime.fromtimestamp(stamp)))
        except Exception:
            logger.debug("Import des habitudes applicatives ignoré", exc_info=True)
        try:
            for entry in tool_stats.load():
                if entry.get("tool") != "music_control" or not entry.get("ok", True):
                    continue
                stamp = datetime.fromisoformat(str(entry.get("ts") or "").replace("Z", "+00:00"))
                if stamp.timestamp() >= cutoff:
                    events.append(("music", stamp.astimezone().replace(tzinfo=None)))
        except Exception:
            logger.debug("Import des habitudes musicales ignoré", exc_info=True)
        self._habits.import_once("journaux-existants-v1", events)
    def _observe_habit_reply(self, text: str) -> None:
        """Un refus explicite suspend seulement la suggestion concernée.

        On ne devine jamais un refus dans une phrase ordinaire : il doit
        contenir une formulation négative courte, et arriver dans les quinze
        minutes qui suivent la seule suggestion d'habitude en attente.
        """
        pending = self._pending_habit_event
        if not pending or time.monotonic() - pending[1] > 15 * 60:
            self._pending_habit_event = None
            return
        folded = str(text or "").casefold()
        if re.search(r"\b(non|non merci|pas maintenant|laisse tomber|arrete|arrête)\b", folded):
            self._habits.decline(pending[0])
            self._pending_habit_event = None
            self.ui.write_log("SYS : suggestion d'habitude suspendue pendant un mois.")

    # ── Morning briefing ────────────────────────────────────────────────────────

    def _get_user_name(self) -> str:
        from core.personality_modes import user_address
        # Les briefings sont une vraie interaction : l'appellation du mode est
        # prioritaire sur le nom éventuellement mémorisé dans le profil.
        return user_address()

    async def _send_daily_briefing(self, reason: str = "salutation") -> None:
        """Collecte en parallèle et délivre le briefing quotidien parlé en 30 secondes.

        1. Météo du jour (température, ciel, min/max).
        2. Trajet / repère de localisation.
        3. E-mails importants (comptés, pas lus).
        4. Rappels du jour.
        5. Deux titres d'actualité tech/cyber.
        6. Veille IA : nouveaux modèles, Google/Gemini, OpenAI/ChatGPT, Anthropic/Claude.
        7. État rapide de la machine (batterie, RAM, CPU).
        """
        if not self.session:
            return

        # ANO Remote est la source de position prioritaire. Demander un point
        # frais avant la météo empêche un ancien cache IP de contaminer tout le
        # briefing. Un échec laisse la localisation inconnue, jamais Paris.
        if self._dashboard is not None:
            try:
                await self._dashboard.request_fresh_location(timeout=5.0)
            except Exception:
                logger.debug("Position fraîche indisponible pour le briefing", exc_info=True)

        self.ui.write_log(f"SYS : préparation du briefing quotidien ({reason})...")
        user_name = self._get_user_name()

        # Collecte ultra-rapide en parallèle (< 1.5s)
        data = await collect_briefing_data(user_name=user_name)

        # Affichage visuel dans le HUD
        card_title, card_body = format_briefing_card(data)
        self.ui.show_card("info", card_title, card_body)

        # Prompt de restitution vocale calibré en 30 secondes chrono
        prompt = format_briefing_prompt(
            data, getattr(self, "_conversation_language", "fr-FR")
        )
        mark_briefing_delivered()

        try:
            await self._submit_text_turn(prompt)
            self.ui.write_log("SYS : Briefing quotidien parlé délivré (30s).")
        except Exception as exc:
            print(f"[Briefing] Erreur d'envoi du briefing: {exc}")
            self.ui.write_log(f"ERR : Échec d'envoi du briefing: {exc}")
        finally:
            # La carte est un indicateur de briefing en cours, pas une fenêtre
            # à ranger manuellement. CardManager lui applique sa sortie néon.
            self.ui.dismiss_cards("info", card_title)

    async def _send_startup_briefing(self) -> None:
        """Accueille brièvement au démarrage ; le briefing reste sur demande."""
        if not self.session:
            return
        greeting = startup_greeting()
        try:
            await self._submit_text_turn(
                "[SALUTATION DE DÉMARRAGE] Prononce exactement cette phrase, "
                "sans préambule, sans ajout et sans appeler d'outil :\n"
                + greeting
            )
            self.ui.write_log(f"SYS : salutation de démarrage — {greeting}")
        except Exception as exc:
            print(f"[Démarrage] Salutation impossible : {exc}")

    # ── Rappels persistants ─────────────────────────────────────────────────────

    def _show_active_reminders_card(self) -> None:
        """Remplace la carte des rappels par l'état réellement encore actif."""
        from actions.reminder import active_reminders

        self.ui.dismiss_cards("result", "Rappels actifs")
        upcoming = active_reminders()
        if not upcoming:
            return
        body = "\n".join(
            f"**{i}.** {e.get('datetime')} — {e.get('message', '')[:70]}"
            for i, e in enumerate(upcoming, 1)
        )
        actions = [
            {
                "label": f"Suppr. #{i}",
                "callback": (lambda tn=e.get("task_name"):
                             self.ui.on_text_command(f"annule le rappel {tn}")
                             if self.ui.on_text_command else None),
            }
            for i, e in enumerate(upcoming[:4], 1)
        ]
        self.ui.show_card("result", "Rappels actifs", body, actions)

    async def _announce_due_reminder(self, event: dict) -> None:
        """Affiche, prononce puis ferme un rappel arrivé à échéance."""
        from actions.media_control import get_current_volume, system_volume

        message = re.sub(r"\s+", " ", str(event.get("message") or "Rappel")).strip()[:300]
        self.ui.dismiss_cards("result", "Rappels actifs")
        self.ui.show_card(
            "info", "⏰ RAPPEL",
            f"**{message}**\n\nAnnonce vocale en cours…",
        )
        self.ui.write_log(f"SYS: ⏰ Rappel arrivé — {message}")
        if self._dashboard is not None:
            try:
                await self._dashboard.broadcast({
                    "type": "sys", "text": f"⏰ Rappel : {message}",
                })
            except Exception:
                logger.debug("Notification téléphone de rappel indisponible", exc_info=True)

        original_volume = await asyncio.to_thread(get_current_volume)
        volume_raised = original_volume is not None and original_volume < 70
        if volume_raised:
            await asyncio.to_thread(system_volume, 70)

        try:
            if not self.session:
                return

            # Le rappel est prioritaire : il remplace proprement une réponse
            # en cours et son audio ne peut pas être confondu avec l'ancien tour.
            if self._model_turn_active or self._is_speaking:
                self.interrupt()
                await asyncio.sleep(0.08)
                self._model_turn_active = False
                self._clear_interrupted()

            await self._submit_text_turn(
                f"[ALERTE RAPPEL ARRIVÉE À ÉCHÉANCE]\nMessage : {message}\n\n"
                "Prononce immédiatement et distinctement : « Rappel : "
                f"{message} ». Ne réponds à rien d'autre, n'appelle aucun outil "
                "et termine juste après cette annonce."
            )

            # turn_complete précède parfois la fin réelle du tampon sonore.
            if self._turn_done_event:
                try:
                    await asyncio.wait_for(self._turn_done_event.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    logger.debug("Tour précédent non terminé avant le briefing")
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and (
                self._is_speaking
                or (self.audio_in_queue is not None and not self.audio_in_queue.empty())
            ):
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Reminder] Annonce vocale impossible : {exc}")
        finally:
            if volume_raised:
                await asyncio.to_thread(system_volume, original_volume)
            self.ui.dismiss_cards("info", "⏰ RAPPEL")
            # Le script retire le rappel du registre juste après l'événement.
            await asyncio.sleep(0.15)
            await asyncio.to_thread(self._show_active_reminders_card)
    async def _run_reminder_watch(self) -> None:
        """Récupère les rappels tombés pendant que l'application était arrêtée.

        En fonctionnement normal, le script du minuteur publie directement
        sur IPC et cette tâche dort indéfiniment : aucun polling n'est requis.
        """
        from actions.reminder import pop_triggered_reminders

        self.ui.write_log("SYS: Veille des rappels active.")
        try:
            events = await asyncio.to_thread(pop_triggered_reminders)
            for event in events:
                message = re.sub(
                    r"\s+", " ", str(event.get("message") or "Rappel")
                ).strip()[:300]
                self._proactive.publish(
                    "reminder", f"Rappel : {message}",
                    dedupe_key=f"reminder:{event.get('task_name') or message}",
                    priority=100, data=event,
                )
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Reminder] Récupération impossible : {exc}")

    async def _render_timers(self, active: list[dict]) -> None:
        current = set()
        for timer in active:
            title = f"⏳ MINUTEUR · {timer['name']}"
            current.add(title)
            remain = timer["remaining"]
            body = f"**{remain // 60:02d}:{remain % 60:02d}** restant"
            if title in self._timer_card_titles:
                self.ui.update_card("info", title, body)
            else:
                self.ui.show_card("info", title, body)
        for title in self._timer_card_titles - current:
            self.ui.dismiss_cards("info", title)
        self._timer_card_titles = current

    async def _announce_due_timer(self, timer: dict) -> None:
        from actions.media_control import get_current_volume, system_volume
        name = str(timer.get("name") or "Minuteur")
        self.ui.dismiss_cards("info", f"⏳ MINUTEUR · {name}")
        self.ui.show_card("info", "⏰ MINUTEUR TERMINÉ", f"**{name}** est prêt.")
        original = await asyncio.to_thread(get_current_volume)
        lowered = original is not None and original > 35
        if lowered:
            await asyncio.to_thread(system_volume, 35)
        try:
            if self.session:
                await self._submit_text_turn(
                    f"[MINUTEUR ÉCHU] Prononce seulement : « Minuteur {name} terminé. »",
                    timeout_s=20.0,
                )
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.debug("Tour précédent non terminé avant l'annonce proactive")
                while self._is_speaking:
                    await asyncio.sleep(0.08)
        finally:
            if lowered:
                await asyncio.to_thread(system_volume, original)

    async def _run_timer_watch(self) -> None:
        await self._timers.watch(self._render_timers, self._announce_due_timer)

    async def _run_focus_guard_watch(self) -> None:
        """Observe sans bloquer la boucle audio et publie les interventions utiles."""
        while True:
            try:
                events = await asyncio.to_thread(self._focus_guard.poll)
                for event in events:
                    self._proactive.publish(
                        "focus_guard",
                        str(event.get("message") or ""),
                        dedupe_key=str(event.get("key") or "focus-guard"),
                        priority=int(event.get("priority") or 70),
                    )
            except Exception as exc:
                print(f"[FocusGuard] ⚠️ Observation ignorée : {exc}")
            await asyncio.sleep(5.0)

    async def _run_github_backup_watch(self) -> None:
        """Sauvegarde GitHub périodique des seuls projets explicitement suivis."""
        while True:
            try:
                from core.github_backup import run_periodic_pushes
                reports = await asyncio.to_thread(run_periodic_pushes)
                for report in reports:
                    if report and "Rien à commiter" not in report:
                        self.ui.write_log(f"SYS : sauvegarde GitHub — {report.splitlines()[-1]}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[GitHubBackup] ⚠️ sauvegarde ignorée : {exc}")
            await asyncio.sleep(900.0)

    async def _run_auto_extension_watch(self) -> None:
        """Évalue les lacunes observées sans bloquer la voix ni éditer le cœur."""
        while True:
            try:
                from core.auto_extension import get_auto_extension_manager
                from core.session_manager import _voice_engine_settings
                config = _voice_engine_settings()
                get_auto_extension_manager().poll(
                    plugins=self._plugins,
                    refresh=self._refresh_plugin_runtime,
                    ui=self.ui,
                    threshold=int(config.get("auto_extension_threshold", 3) or 3),
                    days=int(config.get("auto_extension_window_days", 14) or 14),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[AutoExtension] ⚠️ surveillance ignorée : {exc}")
            await asyncio.sleep(600.0)

    # ── Veille TikTok (compteurs « à la Blow ») ───────────────────────────────

    async def _run_tiktok_watch(self) -> None:
        """Relit le profil TikTok configuré à intervalle régulier.

        Chaque lecture ouvre un Chrome headless pendant une dizaine de
        secondes : elle part dans un thread et l'intervalle ne descend jamais
        sous 45 s. Les nouveaux abonnés, paliers et vidéos qui décollent sont
        annoncés par le canal proactif ; la carte est mise à jour sans bruit.
        """
        from actions import tiktok_tracker as tt

        announced_idle = False
        while True:
            state = await asyncio.to_thread(tt.load_state)
            if not state.get("enabled") or not state.get("handle"):
                if not announced_idle:
                    announced_idle = True
                await asyncio.sleep(15.0)
                continue
            if announced_idle:
                announced_idle = False
                self.ui.write_log(f"SYS : veille TikTok active — @{state['handle']}.")
            snaps = state.get("snapshots") or []
            # L'outil vient peut-être de lire : ne pas rouvrir Chrome pour rien.
            if snaps and time.time() - snaps[-1]["ts"] < tt.MIN_INTERVAL_S:
                await asyncio.sleep(tt.MIN_INTERVAL_S)
                continue
            try:
                cur, prev, state = await asyncio.to_thread(tt.poll_once, self.ui, state)
                events = tt.summarize_notable_events(tt.notable_events(prev, cur))
                for key, message, priority in events:
                    self._proactive.publish(
                        "tiktok", message, dedupe_key=key, priority=priority,
                        data={"handle": cur.get("handle"), "followers": cur.get("followers")},
                    )
                    self.ui.write_log(f"SYS : TikTok — {message}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[TikTok] ⚠️ lecture ignorée : {exc}")
            await asyncio.sleep(tt.next_delay(state))

    # ── Veille Calendrier (anticipation réunions) ──────────────────────────────

    async def _run_calendar_watch(self) -> None:
        """Interroge le calendrier toutes les 5 minutes et annonce les réunions 10 minutes avant."""
        try:
            from core.calendar_service import get_calendar_service
            from core.calendar_watcher import CalendarWatcher, parse_event_start
        except ImportError as exc:
            print(f"[CalendarWatch] Dépendance manquante : {exc}")
            return

        service = get_calendar_service()
        if not hasattr(self, "_calendar_watcher") or self._calendar_watcher is None:
            self._calendar_watcher = CalendarWatcher()

        watcher = self._calendar_watcher
        self.ui.write_log("SYS: Veille agenda (anticipation réunions) active.")

        while True:
            try:
                # Vérifie d'abord que le service de calendrier est authentifié
                status = await asyncio.to_thread(service.status)
                if not status.authenticated:
                    await asyncio.sleep(300.0)
                    continue

                # Détection et formatage en thread de travail (pas de blocage GIL/audio)
                result = await asyncio.to_thread(
                    watcher.check_and_produce_announcement, service
                )
                if result is not None:
                    message, upcoming = result
                    key = ":".join(sorted(watcher.event_id(m) for m in upcoming))
                    starts = [
                        parse_event_start(m).timestamp()
                        for m in upcoming
                        if parse_event_start(m) is not None
                    ]
                    self._proactive.publish(
                        "calendar",
                        message,
                        dedupe_key=f"calendar:{key}",
                        priority=85,
                        data={
                            "event_ids": [watcher.event_id(m) for m in upcoming],
                            "starts": starts,
                        },
                    )
                    self.ui.write_log(f"SYS: Agenda — {message}")
                    try:
                        self.ui.show_card("info", "📅 AGENDA", message)
                    except Exception:
                        logger.debug("Carte agenda indisponible", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[CalendarWatch] ⚠️ Erreur lors de la veille : {exc}")

            await asyncio.sleep(300.0)

    # ── Veille Heures de Prière (Adhan) ─────────────────────────────────────────

    async def _run_prayer_watch(self) -> None:
        """Surveille les heures de prière (Adhan) et déclenche l'annonce à l'heure exacte."""
        try:
            from actions.prayer import get_prayer_manager
        except ImportError as exc:
            print(f"[PrayerWatch] Dépendance manquante : {exc}")
            return

        manager = get_prayer_manager()
        self.ui.write_log("SYS: Veille des heures de prière (Adhan) active.")

        while True:
            try:
                # Vérifie et produit l'annonce en thread de travail (pas de blocage GIL/audio)
                result = await asyncio.to_thread(manager.check_and_produce_announcement)
                if result is not None:
                    message, p_name, p_time = result
                    key = f"prayer:{p_name}:{p_time.strftime('%Y-%m-%d')}"
                    self._proactive.publish(
                        "prayer",
                        message,
                        dedupe_key=key,
                        priority=85,
                        data={
                            "prayer": p_name,
                            "time": p_time.timestamp(),
                        },
                    )
                    self.ui.write_log(f"SYS: Prière — {message}")
                    try:
                        self.ui.show_card("info", "🕌 PRIÈRE", message)
                    except Exception:
                        logger.debug("Carte prière indisponible", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[PrayerWatch] ⚠️ Erreur lors de la veille : {exc}")

            await asyncio.sleep(30.0)

    # ── Veille Gmail ────────────────────────────────────────────────────────────

    async def _run_gmail_watch(self) -> None:
        """Annonce les nouveaux e-mails à voix haute, dès leur arrivée.

        Le service Gmail surveille la boîte dans son propre fil. Il ne peut pas
        parler depuis là : la session vocale n'appartient qu'à la boucle
        asyncio. Le fil de veille dépose donc dans une file, et cette tâche
        transmet — c'est le seul passage sûr entre les deux mondes.
        """
        try:
            from core.email_service import GmailError, get_gmail_service
        except ImportError:
            return

        service = get_gmail_service()
        blocked = str(getattr(service, "_setup_required", "") or "")
        if blocked:
            # Déjà signalé lors de l'arrêt de la veille : une reconnexion vocale
            # ne doit pas le répéter à chaque fois.
            if not getattr(self, "_gmail_block_logged", False):
                self._gmail_block_logged = True
                self.ui.write_log(f"SYS: Veille Gmail inactive — {blocked}")
            return
        status = await asyncio.to_thread(service.status)
        if not status.authenticated:
            # Sans autorisation, la veille n'a rien à surveiller. Le diagnostic
            # reste disponible : l'utilisateur l'obtient en demandant Gmail.
            self.ui.write_log(f"SYS: Veille Gmail inactive — {status.next_step or status.message}")
            return

        loop = asyncio.get_running_loop()
        arrivals: asyncio.Queue = asyncio.Queue(maxsize=50)

        def _on_mail(mail: dict) -> None:
            # Appelé depuis le fil de veille : ne toucher qu'à la file.
            loop.call_soon_threadsafe(_enqueue, mail)

        def _enqueue(mail: dict) -> None:
            try:
                arrivals.put_nowait(mail)
            except asyncio.QueueFull:
                logger.debug("Rafale Gmail ignorée : file proactive pleine")

        started = await asyncio.to_thread(service.start_polling, _on_mail)
        if not started:
            return
        self.ui.write_log("SYS: Veille Gmail active.")

        try:
            while True:
                mail = await arrivals.get()
                sender = str(mail.get("sender") or "Inconnu")
                subject = str(mail.get("subject") or "Sans objet")
                snippet = str(mail.get("snippet") or "")[:220]

                try:
                    self.ui.show_card(
                        "message", f"✉️ {sender[:36]}",
                        f"**{subject}**\n\n{snippet}",
                    )
                except Exception:
                    logger.debug("Carte Gmail indisponible", exc_info=True)
                if self._dashboard is not None:
                    try:
                        await self._dashboard.broadcast({
                            "type": "sys",
                            "text": f"Nouveau message de {sender} — {subject}",
                        })
                    except Exception:
                        logger.debug("Notification téléphone Gmail indisponible", exc_info=True)

                # Une arrivée réellement nouvelle est annoncée pour tous les
                # messages, pas seulement pour le libellé Gmail IMPORTANT.
                # La veille Gmail établit elle-même son point de départ au
                # lancement : aucun ancien non lu ne rejoint cette file.
                if self.ui.muted:
                    self._wake_up("nouveau Gmail")
                importance = " important" if mail.get("important") else ""
                self._proactive.publish(
                    "mail",
                    f"Nouveau mail{importance} de {sender} : {subject}.",
                    dedupe_key=f"mail:{mail.get('id') or subject.casefold()}",
                    priority=85 if mail.get("important") else 75,
                    data={"sender": sender, "subject": subject},
                )
        except asyncio.CancelledError:
            raise
        except GmailError as exc:
            self.ui.write_log(f"SYS: Veille Gmail arrêtée — {exc}")
        finally:
            await asyncio.to_thread(service.stop_polling)

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert:
                try:
                    self.ui.show_card("info", "Alerte système", alert)
                except Exception:
                    logger.debug("Carte alerte système indisponible", exc_info=True)
                if self.session:
                    try:
                        await self._submit_text_turn(alert)
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """Attend les événements puis les prononce au premier moment sûr."""
        self._proactive.bind(asyncio.get_running_loop())
        system_watch = asyncio.create_task(
            self._proactive.watch_system_events(), name="proactive-system-events"
        )
        try:
            while True:
                event = await self._proactive.next_event()

                # En mode silence, on ne rejoue pas une pile de vieilles alertes
                # lors du réveil. Les cartes et notifications natives restent.
                if self._proactive.silent:
                    self._proactive.mark_delivered(event)
                    continue

                max_age = 6 * 3600 if event.topic == "reminder" else 30 * 60
                if time.time() - event.created_at > max_age:
                    self._proactive.discard(event)
                    continue

                with self._speaking_lock:
                    speaking = self._is_speaking
                locally_busy = (
                    speaking
                    or self._model_turn_active
                    or self._activity_open
                    or self._phone_active
                    or self.ui.muted
                )
                if locally_busy or not self.session:
                    self._proactive.hold(event)
                    continue

                blocked = await asyncio.to_thread(desktop_blocks_proactivity)
                if blocked:
                    # Plusieurs événements peuvent être prêts ensemble. Une
                    # seule ligne suffit pour expliquer le blocage courant ;
                    # répéter la même phrase pour chacun ressemble à une panne.
                    now = time.monotonic()
                    previous = getattr(self, "_last_proactive_defer_log", None)
                    if not previous or previous[0] != blocked or now - previous[1] >= 300.0:
                        self.ui.write_log(
                            f"SYS: Annonce proactive différée ({blocked})."
                        )
                        self._last_proactive_defer_log = (blocked, now)
                    self._proactive.defer(event, 30.0)
                    continue

                if in_quiet_hours():
                    # Exception pour Fajr si expressément autorisée dans les paramètres
                    is_fajr_allowed = False
                    if event.topic == "prayer" and event.data.get("prayer") == "fajr":
                        try:
                            from actions.prayer import get_prayer_manager
                            cfg = get_prayer_manager().config
                            if cfg.allow_fajr_in_quiet_hours and cfg.prayers.get("fajr", True):
                                is_fajr_allowed = True
                        except Exception:
                            logger.debug("Configuration Fajr indisponible", exc_info=True)

                    if not is_fajr_allowed:
                        self.ui.write_log("SYS: Annonce proactive différée (heures calmes).")
                        self._proactive.defer(event, 60.0)
                        continue

                if event.topic == "prayer":
                    prayer_time = event.data.get("time", 0)
                    # Si l'annonce a été différée plus de 35 min après l'heure de prière, on expire proprement
                    if prayer_time and (time.time() - prayer_time > 2100):
                        self._proactive.discard(event)
                        self.ui.write_log("SYS: Annonce prière expirée (> 35 min après l'heure).")
                        continue

                if event.topic == "calendar":
                    starts = event.data.get("starts", [])
                    if starts:
                        now_ts = datetime.now().astimezone().timestamp()
                        all_past = all(float(s) < now_ts for s in starts if isinstance(s, (int, float)))
                        if all_past:
                            self._proactive.discard(event)
                            self.ui.write_log("SYS: Annonce réunion ignorée (déjà commencée).")
                            continue

                # Revérification immédiate : l'utilisateur peut avoir repris
                # la parole pendant les quelques ms de la sonde bureau.
                with self._speaking_lock:
                    if self._is_speaking:
                        self._proactive.defer(event, 5.0)
                        continue
                if self._model_turn_active or self._activity_open:
                    self._proactive.defer(event, 5.0)
                    continue

                try:
                    if event.topic == "prayer":
                        prompt_text = (
                            "[ANNONCE PROACTIVE - TON NOCTURNE & POSÉ]\n"
                            "Consigne vocale : Adopte une voix particulièrement calme, posée, douce et feutrée (style nocturne). "
                            "Évite toute exclamation vive. "
                            "Prononce exactement cette annonce, sans préambule, sans ajout et sans appeler d'outil :\n"
                            f"{event.message}"
                        )
                    else:
                        prompt_text = (
                            "[ANNONCE PROACTIVE] Prononce exactement cette phrase, "
                            "sans préambule, sans ajout et sans appeler d'outil :\n"
                            f"{event.message}"
                        )
                    await self._submit_text_turn(prompt_text)
                    self._proactive.mark_delivered(event)
                    if event.topic == "habit":
                        habit_event = str(event.data.get("habit_event") or "")
                        if habit_event:
                            self._pending_habit_event = (habit_event, time.monotonic())
                    self.ui.write_log(
                        f"SYS: Proactif/{event.topic} — {event.message[:100]}"
                    )
                    try:
                        card_title = "🕌 PRIÈRE" if event.topic == "prayer" else f"JARVIS · {event.topic}"
                        self.ui.show_card(
                            "info", card_title, event.message
                        )
                    except Exception:
                        logger.debug("Carte proactive indisponible", exc_info=True)
                except Exception as exc:
                    print(f"[Proactive] Annonce impossible : {exc}")
                    self._proactive.defer(event, 15.0)
        finally:
            system_watch.cancel()
            try:
                await system_watch
            except asyncio.CancelledError:
                logger.debug("Veille système annulée")

    async def _run_habit_model(self) -> None:
        """Évalue les habitudes au début de chaque créneau de trente minutes."""
        while True:
            now = datetime.now()
            event = await asyncio.to_thread(self._habits.candidate, now)
            if event:
                message = suggestion_text(event)
                if message:
                    self._proactive.publish(
                        "habit", message,
                        dedupe_key=f"habit:{event}:{now.date().isoformat()}",
                        priority=20,
                        data={"habit_event": event},
                    )
            # Le modèle n'a besoin d'être réveillé qu'au prochain créneau,
            # jamais en polling continu.
            delay = 30 * 60 - (now.minute % 30) * 60 - now.second
            await asyncio.sleep(max(1, delay))

    # ── routines ────────────────────────────────────────────────────────────

    def _maybe_routine(self, text: str, source: str) -> bool:
        """Lance la routine correspondant à la phrase, s'il y en a une.

        Rend True quand la phrase a été consommée : l'appelant ne doit alors
        rien envoyer au modèle, l'ordre est déjà en train de s'exécuter.
        """
        try:
            found = routines.match(text)
        except Exception as exc:
            print(f"[Routines] reconnaissance impossible : {exc}")
            return False
        if found is None:
            return False
        print(f"[Routines] ⚡ « {found.name} » ({source})")
        self._run_routine(found)
        return True

    def _run_routine(self, routine, *, announce: bool = True) -> None:
        """Exécute la routine dans un thread : les actions sont bloquantes.

        La confirmation est d'abord visuelle. La voix n'est sollicitée que si
        la session est déjà éveillée : micro coupé, une routine ne doit rien
        envoyer sur le réseau — c'est toute la promesse du chemin hors-ligne.
        """
        self.ui.write_log(f"SYS : routine « {routine.name} » — en cours.")

        def _work() -> None:
            results = routines.run(
                routine,
                dispatch=lambda name, args: self._dispatch_agent_tool(name, args),
                log=lambda line: self.ui.write_log(f"SYS : {line}"),
            )
            report = routines.summary(routine, results)
            self.ui.write_log(f"SYS : {report}")
            if routine.card:
                try:
                    self.ui.show_card(
                        "info", routine.name,
                        "\n".join(f"{'✓' if ok else '✗'} {label}"
                                  + (f" — {msg}" if msg else "")
                                  for label, ok, msg in results),
                    )
                except Exception:
                    logger.debug("Carte de routine indisponible", exc_info=True)
            if announce and routine.say and self.session:
                self.speak(
                    "[ROUTINE] Dis exactement ceci, sans rien ajouter et sans "
                    f"appeler le moindre outil : {routine.say}"
                )

        get_thread_pool().submit(
            "compute-light", _work, task_name="run-routine",
            stall_timeout=60.0,
        )
