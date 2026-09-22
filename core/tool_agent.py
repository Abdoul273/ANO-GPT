"""Outils exposés aux agents externes (MCP) et cartes des tâches de fond.

Mixin de ``ToolDispatcher`` : les méthodes sont liées à l'hôte ``JarvisLive``.
Chaque outil d'agent est une méthode synchrone ``_agent_<nom>(args) -> str``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from core import memory_store, routines, speaker_id
from core.live_model_policy import DEFAULT_PRIMARY_MODEL as LIVE_MODEL
from core.multimodal_vision import inspect_screen_live

from actions.auto_debug import auto_debug_action
from actions.background_tasks import BackgroundTaskService, format_agent_result, format_tasks
from actions.calendar import calendar_control
from actions.cloud_integrations import cloud_integrations_control
from actions.contacts import contacts_control
from actions.devsecops import devsecops_control
from actions.hypr_orchestrator import hypr_orchestrator_control
from actions.navigation import navigation_action
from actions.prayer import prayer_control

BASE_DIR = Path(__file__).resolve().parent.parent


class AgentToolsMixin:
    """Table des outils d'agents et rendu des tâches de fond."""

    _agent_tool_table: dict | None = None

    def _agent_tools(self) -> dict:
        """Table des outils qu'un agent externe peut exécuter.

        Chaque valeur prend le dictionnaire d'arguments et rend le texte que
        l'agent lira. Ces fonctions tournent dans un fil d'exécution (voir
        `_tool` dans `_start_control_server`), jamais sur la boucle asyncio :
        elles ont donc le droit de bloquer.

        Le contrat est volontairement plat — des chaînes en entrée, une chaîne
        en sortie. Un agent lit du texte ; lui rendre une structure l'obligerait
        à la reformater avant de la restituer.
        """
        if self._agent_tool_table is None:
            self._agent_tool_table = {
                # ── le cœur visuel ──────────────────────────────────────────
                "find_nearby": self._agent_find_nearby,
                "show_map": self._agent_show_map,
                "close_map": lambda a: (self.ui.close_map(), "Carte fermée.")[1],
                "camera": self._agent_camera,
                "show_card": self._agent_show_card,
                # ── voix et état ────────────────────────────────────────────
                "speak": self._agent_speak,
                "ask": self._agent_ask,
                "assistant_status": self._agent_status,
                # ── données personnelles ────────────────────────────────────
                "email": self._agent_email,
                "calendar": lambda a: calendar_control(a),
                "cloud_integrations": lambda a: cloud_integrations_control(a),
                "contacts": lambda a: contacts_control(a),
                "memory_save": self._agent_memory_save,
                "memory_search": self._agent_memory_search,
                "second_brain": self._agent_second_brain,
                # ── machine et médias ───────────────────────────────────────
                "weather": self._agent_weather,
                "web_search": self._agent_web_search,
                "image_search": self._agent_image_search,
                "close_image_gallery": lambda a: (
                    self.ui.close_image_gallery(), "Galerie d'images fermée."
                )[1],
                "screenshot": self._agent_screenshot,
                "open_app": self._agent_open_app,
                "close_app": self._agent_close_app,
                "computer_settings": self._agent_computer_settings,
                "file_search": self._agent_file_search,
                "location": self._agent_location,
                "youtube": self._agent_youtube,
                "reminder": self._agent_reminder,
                "system_status": self._agent_system_status,
                "music": self._agent_music,
                "download_music": self._agent_download_music,
                "routine": self._agent_routine,
                "voice_id": self._agent_voice_id,
                "voice_style": self._agent_voice_style,
                "self_repair": self._agent_self_repair,
                "background_tasks": self._background_tasks_control,
                "devsecops": lambda a: devsecops_control(a),
                "hypr_orchestrator": lambda a: hypr_orchestrator_control(a),
                "auto_debug": lambda a: auto_debug_action(a, player=self.ui, speak=self.speak),
                "inspect_screen": lambda a: self._agent_inspect_screen(a),
                "navigate": lambda a: navigation_action(a, player=self.ui, speak=self.speak),
                "point_on_screen": lambda a: self._agent_point_on_screen(a),
                "search_personal_docs": self._agent_search_personal_docs,
                "prayer": lambda a: prayer_control(a),
            }
        return self._agent_tool_table

    @staticmethod
    def _arg(args: dict, key: str, default: str = "") -> str:
        value = args.get(key, default)
        return "" if value is None else str(value).strip()

    def _agent_find_nearby(self, args: dict) -> str:
        """Cherche des lieux ET les épingle dans la grande carte."""
        from actions.find_nearby import find_nearby

        return find_nearby(
            {
                "query": self._arg(args, "query"),
                "near": self._arg(args, "near"),
                "radius_km": args.get("radius_km") or 5.0,
            },
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_show_map(self, args: dict) -> str:
        query = self._arg(args, "query")
        radius = float(args.get("radius_km") or 3.0)
        lat, lon = args.get("lat"), args.get("lon")
        if lat is not None and lon is not None:
            self.ui.show_map(query or "Position", float(lat), float(lon), radius)
            return f"Carte centrée sur {lat}, {lon}."
        if not query:
            return "Précisez un lieu, ou des coordonnées lat/lon."
        from core.geolocation import geocode

        coords = geocode(query)
        if not coords:
            return f"Impossible de situer « {query} »."
        self.ui.show_map(query, coords[0], coords[1], radius)
        return f"Carte centrée sur {query}."

    def _agent_camera(self, args: dict) -> str:
        return self._camera_tool(
            self._arg(args, "action", "open").lower(),
            self._arg(args, "source").lower(),
            self._arg(args, "lens").lower(),
        )

    def _agent_show_card(self, args: dict) -> str:
        title = self._arg(args, "title") or "Agent"
        self.ui.show_card(
            self._arg(args, "type", "info").lower() or "info",
            title,
            self._arg(args, "body"),
        )
        return f"Carte « {title} » affichée."

    def _agent_speak(self, args: dict) -> str:
        """Fait prononcer une phrase par ANO-GPT.

        Le texte traverse la session Gemini Live, qui possède la voix : il est
        donc reformulé au passage, pas lu mot pour mot.
        """
        text = self._arg(args, "text")
        if not text:
            return "Rien à dire : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active : rien n'a été prononcé."
        self.speak(
            "[AGENT] Dis ceci à l'utilisateur, dans sa langue et sans appeler "
            f"le moindre outil : {text}"
        )
        return "Énoncé transmis à la voix."

    def _agent_ask(self, args: dict) -> str:
        """Envoie une demande à l'assistant vocal, qui décidera lui-même."""
        text = self._arg(args, "text")
        if not text:
            return "Rien à demander : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active."
        self._wake_up("agent")
        self._on_text_command(text)
        return f"Demande transmise à l'assistant : {text[:80]}"

    def _agent_status(self, args: dict) -> str:
        camera = self._camera
        continuous_active = bool(getattr(self, "_continuous", None) and self._continuous.is_active)
        if self.ui.muted:
            micro = "coupé"
        elif continuous_active:
            micro = "écoute continue"
        else:
            micro = "à l'écoute"
        session = "active" if self.session else "inactive"
        if camera is not None and camera.active:
            optique = f"ouverte ({camera.source}/{camera.lens})"
        else:
            optique = "fermée"
        policy = getattr(self, "_live_models", None)
        model = str(getattr(policy, "current", LIVE_MODEL)).removeprefix("models/")
        precision = (
            "active" if getattr(self, "_precision_stt_enabled", True)
            else "désactivée"
        )
        return (
            f"micro={micro} · session={session} · caméra={optique} · "
            f"Gemini Live={model} · vérification 3.5={precision}"
        )

    def _agent_email(self, args: dict) -> str:
        from actions.email import email_control

        parameters = dict(args)
        parameters.update({
            "action": self._arg(args, "action", "unread").lower() or "unread",
            "query": self._arg(args, "query"),
            "id": self._arg(args, "id"),
            "max_results": args.get("max_results") or 10,
        })
        return email_control(
            parameters,
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_memory_save(self, args: dict) -> str:
        from memory.memory_manager import remember

        key = self._arg(args, "key")
        value = self._arg(args, "value")
        if not key or not value:
            return "Précisez la clé et la valeur à mémoriser."
        category = self._arg(args, "category", "notes") or "notes"
        remember(key, value, category)
        return self._store_memory(category, key, value)

    def _agent_memory_search(self, args: dict) -> str:
        query = self._arg(args, "query")
        if query:
            rows = memory_store.search(query, limit=6)
            if rows:
                return "\n".join(
                    f"- {(r['key'] or '').replace('_', ' ') or r['kind']} : {r['value']}"
                    for r in rows
                )
            return f"Rien en mémoire à propos de « {query} »."

        # Sans terme de recherche, on rend le socle : profil, faits récents et
        # dernières conversations — de quoi situer sans tout déverser.
        summary = memory_store.session_block(max_chars=2500)
        return summary.strip() or "Aucun souvenir enregistré."

    def _agent_second_brain(self, args: dict) -> str:
        from actions.second_brain import second_brain_action

        return second_brain_action(args, player=self.ui, speak=self.speak)

    def _agent_voice_id(self, args: dict) -> str:
        """Outil `voice_id` : apprendre, oublier ou interroger l'empreinte."""
        action = (self._arg(args, "action", "status") or "status").lower()
        if getattr(self, "_speaker", None) is None:
            self._speaker = speaker_id.SpeakerID()
        speaker = self._speaker

        if not speaker.available:
            return ("La reconnaissance vocale n'est pas installée : il manque "
                    "le modèle (scripts/install_speaker_id.sh).")

        if action == "forget":
            self._speaker_verdict = None
            self._speaker_announced = None
            return speaker.forget()

        if action in ("enroll", "learn", "apprendre"):
            clips = list(self._voice_clips)
            if len(clips) < 2:
                return ("Il me faut deux ou trois phrases avant d'apprendre "
                        "une voix. Parle-moi encore un peu, puis redemande.")
            name = self._arg(args, "name") or "Anonymous"
            answer = speaker.enroll(clips[-3:], name)
            self._speaker_verdict = None
            self._speaker_announced = None
            return answer

        if not speaker.enrolled:
            return "Aucune voix n'est enregistrée pour l'instant."
        verdict = self._speaker_verdict
        if verdict is None:
            return f"Voix enregistrée : {speaker.name}. Rien de vérifié pour l'instant."
        return (f"Voix enregistrée : {speaker.name}. Dernier tour : "
                f"{'reconnu' if verdict.known else 'voix inconnue'} "
                f"(score {verdict.score:.2f}).")

    def _agent_voice_style(self, args: dict) -> str:
        """Lit ou change la préférence durable d'élocution."""
        from core.prosody import get_prosody_manager

        manager = get_prosody_manager()
        style = self._arg(args, "style")
        if not style:
            labels = {
                "professional": "professionnel",
                "stark": "Tony Stark",
                "synthetic": "ultra-synthétique",
            }
            label = labels[manager.preferred_style()]
            return f"Style vocal actuel : {label}."
        try:
            label = manager.set_preferred_style(style)
        except ValueError as exc:
            return str(exc)
        # Force la prochaine directive, même si le profil acoustique ne change pas.
        self._prosody_mode = ""
        return f"Style vocal {label} activé."
    def _dispatch_agent_tool(self, name: str, args: dict) -> str:
        """Appelle un outil de l'assistant par son nom, comme le ferait l'agent."""
        handler = self._agent_tools().get(name)
        if handler is None:
            return f"outil inconnu : {name}"
        return handler(dict(args or {}))

    def _agent_routine(self, args: dict) -> str:
        """Outil `routine` : exécute une routine nommée, ou liste les routines."""
        name = self._arg(args, "name")
        if not name:
            available = routines.names()
            return ("Routines disponibles : " + ", ".join(available)
                    if available else "Aucune routine définie.")
        found = routines.match(name)
        if found is None:
            available = routines.names()
            return (f"Aucune routine « {name} ». "
                    + ("Disponibles : " + ", ".join(available) if available
                       else "Le fichier config/routines.yaml est vide."))
        # La voix appelle déjà cet outil : elle annoncera elle-même le résultat,
        # inutile de lui faire répéter la phrase de la routine.
        self._run_routine(found, announce=False)
    def _agent_self_repair(self, args: dict) -> str:
        from actions.self_repair import self_repair

        return self_repair(
            parameters={
                "action": self._arg(args, "action", "diagnose") or "diagnose",
                "tool": self._arg(args, "tool"),
            },
            player=self.ui,
            # La réparation tourne en fond : c'est par `speak` que son résultat
            # est annoncé plus tard. Le diagnostic, lui, ne parle pas deux fois.
            speak=self.speak if (self._arg(args, "action", "") or "").lower() in
                  {"repair", "reparer", "fix", "corriger", "corrige", "restart"} else None,
            session_memory=self._tool_session_memory,
        )
    def _agent_weather(self, args: dict) -> str:
        from actions.weather_report import get_last_weather_card, weather_report

        result = weather_report({
            "city": self._arg(args, "city"),
            "time": self._arg(args, "time", "aujourd'hui") or "aujourd'hui",
        })
        # Ce chemin est employé par les raccourcis/agents locaux ; il doit
        # offrir la même carte que l'appel d'outil Gemini classique.
        card = get_last_weather_card()
        if card and getattr(self, "ui", None) is not None:
            self.ui.show_card("result", "Météo", card)
        return result

    def _agent_web_search(self, args: dict) -> str:
        from actions.web_search import web_search

        return web_search({
            "query": self._arg(args, "query"),
            "mode": self._arg(args, "mode", "search") or "search",
            "platform": self._arg(args, "platform"),
            "items": args.get("items") or [],
            "aspect": self._arg(args, "aspect", "general") or "general",
            "count": args.get("count") or 5,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_image_search(self, args: dict) -> str:
        from actions.image_search import image_search

        return image_search({
            "query": self._arg(args, "query"),
            "limit": args.get("limit") or 6,
        }, player=self.ui)

    def _agent_screenshot(self, args: dict) -> str:
        from actions.capture import capture_control

        return capture_control({
            "action": self._arg(args, "action", "screenshot") or "screenshot",
            "path": self._arg(args, "path") or None,
            "monitor": self._arg(args, "monitor") or None,
            "window": self._arg(args, "window") or None,
            "copy_clipboard": args.get("copy_clipboard", True),
            "annotate": args.get("annotate", False),
            "delay": args.get("delay") or 0,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_inspect_screen(self, args: dict) -> str:
        query = self._arg(args, "query") or "Analyse ce qui est affiché à l'écran."
        domain = self._arg(args, "domain", "auto") or "auto"
        target = self._arg(args, "target", "active_window") or "active_window"
        spoken, diag = inspect_screen_live(
            user_query=query,
            target=target,
            domain=None if domain == "auto" else domain,
            player=self.ui,
        )
        return spoken

    def _agent_point_on_screen(
        self,
        args_or_desc: Union[dict, str],
        coordinates: list | None = None,
        mode: str = "auto",
        duration: float = 3.0,
    ) -> str:
        """Active l'overlay d'annotation visuelle sur écran (rectangle, laser ou trajectoire)."""
        from ui.visual_pointer import get_visual_pointer

        target = ""
        if isinstance(args_or_desc, dict):
            desc = str(args_or_desc.get("description") or "Élément ciblé")
            target = str(args_or_desc.get("target") or args_or_desc.get("widget") or "").strip()
            raw_coords = args_or_desc.get("coordinates")
            if isinstance(raw_coords, str):
                try:
                    import json
                    coords = json.loads(raw_coords)
                except Exception:
                    coords = []
            else:
                coords = list(raw_coords) if raw_coords else []
            m = str(args_or_desc.get("mode") or "auto")
            dur = float(args_or_desc.get("duration") or 3.0)

            # Les coordonnées générées par un modèle sont souvent relatives à
            # une image recadrée, pas au bureau virtuel. Les afficher telles
            # quelles pouvait placer le laser sur le mauvais écran ou dans un
            # coin sans rapport avec l'élément cité. Une cible textuelle est
            # donc toujours relocalisée sur une capture fraîche avant dessin.
            if coords and not target:
                target = desc
                coords = []
        else:
            desc = str(args_or_desc or "Élément ciblé")
            coords = coordinates or []
            m = mode
            dur = duration

        vp = getattr(self.ui, "visual_pointer", None) or get_visual_pointer()
        return vp.point_on_screen(desc, coords, mode=m, duration=dur, target=target or None)

    def _agent_open_app(self, args: dict) -> str:
        from actions.open_app import open_app

        return open_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "command": self._arg(args, "command"),
            "target": self._arg(args, "target"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "hidden": bool(args.get("hidden", False)),
            "count": args.get("count"),
            "instance_name": self._arg(args, "instance_name"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_close_app(self, args: dict) -> str:
        from actions.close_app import close_app

        return close_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "force": bool(args.get("force", False)),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_computer_settings(self, args: dict) -> str:
        from actions.computer_settings import computer_settings

        return computer_settings(parameters={
            "action": self._arg(args, "action"),
            "description": self._arg(args, "description"),
            "value": self._arg(args, "value") or None,
            "confirmed": self._arg(args, "confirmed"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_file_search(self, args: dict) -> str:
        from actions.file_controller import file_controller

        return file_controller(parameters={
            "action": "find",
            "name": self._arg(args, "name"),
            "extension": self._arg(args, "extension"),
            "path": self._arg(args, "path", "home") or "home",
            "kind": self._arg(args, "kind"),
            "max_results": args.get("max_results") or 20,
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_search_personal_docs(self, args: dict) -> str:
        """Recherche sémantique et syntaxique dans les documents personnels et code source."""
        from core.personal_rag import search_personal_docs

        query = self._arg(args, "query")
        pattern = self._arg(args, "file_pattern")
        try:
            max_results = int(args.get("max_results") or 5)
        except (ValueError, TypeError):
            max_results = 5

        return search_personal_docs(
            query=query,
            file_pattern=pattern,
            max_results=max_results,
        )

    def _agent_location(self, args: dict) -> str:
        import json
        from core.geolocation import get_user_location

        refresh = self._arg(args, "refresh").lower() in {
            "1", "true", "yes", "oui",
        }
        location = get_user_location(force_refresh=refresh)
        return json.dumps(location, ensure_ascii=False, sort_keys=True)

    def _agent_youtube(self, args: dict) -> str:
        from actions.youtube_video import youtube_video

        return youtube_video(parameters={
            "action": self._arg(args, "action"),
            "query": self._arg(args, "query"),
            "url": self._arg(args, "url"),
            "region": self._arg(args, "region", "FR") or "FR",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory, speak=self.speak)

    def _agent_reminder(self, args: dict) -> str:
        from actions.reminder import reminder

        return reminder(parameters={
            "action": self._arg(args, "action", "set") or "set",
            "description": self._arg(args, "description"),
            "date": self._arg(args, "date"),
            "time": self._arg(args, "time"),
            "message": self._arg(args, "message", "Rappel") or "Rappel",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory)

    def _agent_system_status(self, args: dict) -> str:
        from actions.system_monitor import system_status_tool

        return system_status_tool({
            "action": self._arg(args, "action", "status") or "status",
            "component": self._arg(args, "component"),
        })

    def _agent_music(self, args: dict) -> str:
        from actions.music import music_control

        return music_control(
            {
                "action": self._arg(args, "action", "play").lower() or "play",
                "query": self._arg(args, "query"),
                "value": self._arg(args, "value"),
                "kind": self._arg(args, "kind", "audio") or "audio",
            },
            player=self.ui, session_memory=self._tool_session_memory,
        )

    def _agent_download_music(self, args: dict) -> str:
        from actions.download_music import download_music as _download

        return _download(
            {
                "query": self._arg(args, "query") or self._arg(args, "url"),
                "url": self._arg(args, "url"),
            },
            player=self.ui,
            speak=self.speak,
        )

    def _background_tasks_control(self, args: dict) -> str:
        if getattr(self, "_background_tasks", None) is None:
            publisher = (
                self._proactive.publish
                if hasattr(self, "_proactive") else (lambda *a, **k: False)
            )
            self._background_tasks = BackgroundTaskService(
                publisher, on_task_update=self._render_background_task,
            )
        action = self._arg(args, "action", "list").casefold() or "list"
        if action in {"list", "status", "statut"}:
            return format_tasks(self._background_tasks.list_tasks())
        if action in {"history", "historique"}:
            return format_tasks(self._background_tasks.list_tasks(include_finished=True))
        if action in {"result", "rapport", "résultat", "resultat"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return format_agent_result(self._background_tasks.agent_result(task_id))
        if action in {"cancel", "annuler", "delete"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return (
                f"Tâche {task_id} annulée."
                if self._background_tasks.cancel(task_id)
                else "Tâche active introuvable."
            )
        try:
            if action in {"watch_price", "price", "surveille_prix"}:
                url = self._arg(args, "url")
                if not url:
                    from actions.browser_control import current_browser_url

                    url = current_browser_url()
                if not url:
                    return (
                        "Je ne connais pas l'URL de cette page. Donne-moi son "
                        "adresse ou ouvre-la avec mon outil navigateur."
                    )
                target = args.get("target_price")
                task = self._background_tasks.add_price_watch(
                    url,
                    target_price=float(target) if target is not None else None,
                    interval_seconds=int(args.get("interval_minutes") or 15) * 60,
                    selector=self._arg(args, "selector"),
                    label=self._arg(args, "label"),
                )
                return f"Je surveille ce prix en arrière-plan ({task['id']})."
            if action in {"wait_build", "build", "wait_command"}:
                task = self._background_tasks.add_build_wait(
                    self._arg(args, "command_contains"),
                    message=self._arg(args, "message"),
                )
                return f"Je te préviendrai à la fin du build ({task['id']})."
            if action in {"wait_arrival", "arrival", "arrivee"}:
                task = self._background_tasks.add_arrival_wait(
                    self._arg(args, "message", "Tu es arrivé à la maison."),
                    radius_m=float(args.get("radius_m") or 250),
                    label=self._arg(args, "label", "maison"),
                )
                return f"Je te le rappellerai à l'arrivée ({task['id']})."
            if action in {"delegate", "ghost", "agent", "fantome", "fantôme"}:
                workspace = self._arg(args, "workspace") or str(BASE_DIR)
                task = self._background_tasks.add_agent_mission(
                    self._arg(args, "mission") or self._arg(args, "message"),
                    workspace=workspace,
                    timeout_minutes=int(args.get("timeout_minutes") or 60),
                    show_terminal=bool(args.get("show_terminal", False)),
                )
                return (
                    "Mode Agent Fantôme activé. Je continue de t'écouter pendant "
                    f"que le sous-agent travaille ({task['id']})."
                    + (" Son journal est visible dans Kitty." if args.get("show_terminal") else "")
                )
        except (TypeError, ValueError) as exc:
            return f"Impossible de créer cette veille : {exc}."
        return "Action de tâche de fond inconnue."

    @staticmethod
    def _background_card_title(task: dict) -> str:
        return f"Tâche en arrière-plan · {str(task.get('id') or '')[-8:]}"

    @staticmethod
    def _background_progress_bar(progress: int | None) -> str:
        if progress is None:
            return "[░░░░░░░░░░] En attente"
        value = max(0, min(100, int(progress)))
        filled = round(value / 10)
        return f"[{'█' * filled}{'░' * (10 - filled)}] {value}%"

    def _background_card_body(self, task: dict, *, detail: bool = False) -> str:
        spec = task.get("spec") if isinstance(task.get("spec"), dict) else {}
        state = task.get("state") if isinstance(task.get("state"), dict) else {}
        kind = str(task.get("kind") or "tâche")
        status = str(task.get("status") or "active")
        phase = str(state.get("phase") or "queued")
        mission = str(spec.get("mission") or spec.get("label") or spec.get("message") or kind)
        progress = state.get("progress") if kind == "agent" else None
        lines = [
            f"**{mission[:300]}**",
            f"État : **{phase if status == 'active' else status}**",
            self._background_progress_bar(progress),
        ]
        if detail:
            journal = str(state.get("log_tail") or "").strip()
            if journal:
                lines += ["", "**Sortie en temps réel**", "```text", journal[-6000:], "```"]
            elif kind == "agent":
                lines += ["", "L'agent prépare encore sa première sortie."]
            if task.get("summary"):
                lines += ["", "**Résultat**", str(task["summary"])]
            if task.get("report_path"):
                lines += ["", f"Rapport : `{task['report_path']}`"]
        return "\n".join(lines)

    def _render_background_task(self, task: dict) -> None:
        """Met à jour une carte persistante sans jamais bloquer le worker agy."""
        task_id = str(task.get("id") or "")
        if not task_id or not hasattr(self, "ui"):
            return
        title = self._background_card_title(task)
        status = str(task.get("status") or "active")
        if status != "active":
            # La carte de suivi ne doit vivre que pendant le travail. À la
            # fin, la sortie cyberpunk de CardManager libère la place pour la
            # tâche suivante ; le résultat arrive séparément dans le journal.
            self.ui.dismiss_cards("agent-task", title)
            self.ui.dismiss_cards("agent-task-detail", f"Détails · {task_id[-8:]}")
            getattr(self, "_background_cards", set()).discard(task_id)
            getattr(self, "_background_detail_cards", set()).discard(task_id)
            return
        body = self._background_card_body(task)
        shown = getattr(self, "_background_cards", set())
        if task_id not in shown:
            shown.add(task_id)
            self._background_cards = shown
            actions = [
                {
                    "label": "Détails",
                    "dismiss": False,
                    "callback": lambda ident=task_id: self._show_background_task_details(ident),
                },
                {
                    "label": "Annuler",
                    "callback": lambda ident=task_id: self._background_tasks.cancel(ident),
                },
            ]
            self.ui.show_card("agent-task", title, body, actions)
        else:
            self.ui.update_card("agent-task", title, body)
        if task_id in getattr(self, "_background_detail_cards", set()):
            self.ui.update_card("agent-task-detail", f"Détails · {task_id[-8:]}",
                                self._background_card_body(task, detail=True))

    def _show_background_task_details(self, task_id: str) -> None:
        service = getattr(self, "_background_tasks", None)
        if service is None:
            return
        task = next((item for item in service.list_tasks(include_finished=True)
                     if str(item.get("id")) == task_id), None)
        if task is None:
            return
        shown = getattr(self, "_background_detail_cards", set())
        title = f"Détails · {task_id[-8:]}"
        body = self._background_card_body(task, detail=True)
        if task_id in shown:
            self.ui.update_card("agent-task-detail", title, body)
            return
        shown.add(task_id)
        self._background_detail_cards = shown
        self.ui.show_card("agent-task-detail", title, body)
