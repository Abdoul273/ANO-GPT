"""Outils longs livrés en arrière-plan : réflexion approfondie, génération
d'image ou de vidéo, simulation de décision, outils différés.

Mixin de ``ToolDispatcher`` : chaque ``_start_*`` lance la tâche sans bloquer
la boucle Live et la carte « tâche en cours » suit son avancement.
"""
from __future__ import annotations

import asyncio
import time

from core.background_task import spawn_logged
from core.tool_cards import (
    _TOOL_LABELS,
    _looks_like_failure,
    _task_card_summary,
    _task_result_excerpt,
)

from actions.document_generation import generate_document as generate_document_action
from actions.download_music import download_music
from actions.email import email_control


class DeferredToolsMixin:
    """Lancement et livraison des outils longs."""

    @staticmethod
    def _compute_deep_research(question: str, context: str = "") -> str:
        """Calcul bloquant isolé du canal Gemini Live."""
        from core import agent_brain
        from core.llm_client import think_deep, BRAIN_UNCONFIGURED

        result = think_deep(question, context)
        if result != BRAIN_UNCONFIGURED:
            return result or "La recherche approfondie n'a rien renvoyé."
        if agent_brain.available():
            return agent_brain.think(question, context)
        return (
            "Aucun agent n'est installé et aucune clé DeepSeek, Grok, OpenAI "
            "ou Claude n'est enregistrée. Réponds toi-même à la demande."
        )

    async def _deliver_deep_research(self, question: str, context: str = "") -> None:
        """Calcule puis livre le résultat sans retenir un tool call Live."""
        task_id = f"deep-research-{time.monotonic_ns()}"
        self._task_card(task_id, "Réflexion approfondie en cours", question[:200], "running")

        try:
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_start("agent_brain", {"question": question})
            result = await asyncio.to_thread(
                self._compute_deep_research, question, context
            )
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_end("agent_brain")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = f"La recherche approfondie a échoué : {str(exc)[:300]}"

        result = str(result or "La recherche approfondie n'a rien renvoyé.").strip()
        # Une sortie anormalement énorme peut elle aussi faire refuser le tour
        # Live. La réponse vocale reste dense ; la carte conserve l'essentiel.
        result = result[:12000]
        self._task_card(task_id, "Réflexion approfondie terminée", _task_result_excerpt(result), "done")
        self._ui_card("show_card", "result", "Réflexion approfondie", result)

        # Session absente (reconnexion en cours) : `_submit_text_turn`
        # conserve le tour et le livre dès la connexion rétablie.
        prompt = (
            "[RÉSULTAT DE RECHERCHE APPROFONDIE TERMINÉ]\n"
            f"Question originale : {question}\n\n"
            f"Résultat : {result}\n\n"
            "Présente maintenant ce résultat directement en français, sans "
            "annoncer un nouveau délai et sans appeler aucun outil."
        )
        try:
            delivered = await self._submit_text_turn(prompt)
        except Exception as exc:
            delivered = False
            print(f"[DeepResearch] Livraison vocale différée : {exc}")
        if not delivered:
            self.ui.write_log(
                "WARN: résultat vocal différé ; il reste disponible dans la carte."
            )

    def _start_deep_research(self, question: str, context: str = "") -> bool:
        """Lance au plus une recherche lourde à la fois."""
        tasks = getattr(self, "_deep_research_tasks", None)
        if tasks is None:
            tasks = self._deep_research_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_deep_research(question, context),
            name="deep-research-background", ui=getattr(self, "ui", None), registry=tasks,
        )
        return True

    async def _deliver_deferred_tool(self, name: str, args: dict) -> None:
        """Travail long hors du tool call, puis restitution dans un nouveau tour."""
        title = _TOOL_LABELS.get(name, name)
        task_id = f"deferred-{name}-{time.monotonic_ns()}"
        self._task_card(task_id, f"{title} en cours", _task_card_summary(name, args), "running")
        try:
            if name == "consult_brain":
                from core import brain_relay
                result = await brain_relay.run_turn(
                    self, str(args.get("question") or ""),
                    context=str(args.get("context") or ""),
                    declarations=self._relay_declarations(), base_prompt=self._relay_base_prompt(),
                    timeout=float(args.get("_budget_s") or 18.0),
                )
            elif name == "generate_document":
                result = await asyncio.to_thread(generate_document_action, parameters=args, player=self.ui)
            elif name == "email_control":
                result = await asyncio.to_thread(
                    email_control, parameters=args, session_memory=self._tool_session_memory, ui=self.ui,
                )
            elif name == "download_music":
                # La recherche YouTube qui précède le worker yt-dlp peut elle
                # aussi être lente : elle ne doit jamais garder le tool call.
                result = await asyncio.to_thread(
                    download_music, parameters=args, player=self.ui, speak=self.speak,
                )
            else:
                return
        except Exception as exc:
            result = f"{title} a échoué : {str(exc)[:300]}"
            self._task_card(task_id, f"{title} — échec", _task_result_excerpt(result), "error")
        else:
            self._task_card(task_id, f"{title} terminée", _task_result_excerpt(result), "done")

        result = str(result or f"{title} terminé.").strip()
        self._ui_card("show_card", "result", title, result[:12_000])
        if self.session is not None:
            try:
                await self._submit_text_turn(
                    f"[RÉSULTAT {title.upper()}]\n{result[:8_000]}\n\n"
                    "Annonce directement ce résultat en français, sans rappeler d'outil.",
                    timeout_s=35.0,
                )
            except Exception as exc:
                self.ui.write_log(f"WARN: livraison différée {name} indisponible : {exc}")

    def _start_deferred_tool(self, name: str, args: dict) -> bool:
        """Un seul travail long du même type, accusé immédiat pour le micro."""
        tasks = getattr(self, "_deferred_tool_tasks", None)
        if tasks is None:
            tasks = self._deferred_tool_tasks = {}
        current = tasks.get(name)
        if current is not None and not current.done():
            return False
        task = asyncio.create_task(self._deliver_deferred_tool(name, dict(args)),
                                   name=f"deferred-{name}")
        tasks[name] = task
        task.add_done_callback(
            lambda done, key=name: tasks.pop(key, None) if tasks.get(key) is done else None
        )
        return True

    async def _deliver_image_generation(self, args: dict) -> None:
        """Génère l'image hors du tour Live et annonce chaque étape en français."""
        from actions.image_generation import generate_image

        prompt = str(args.get("prompt") or "")
        task_id = f"image-{time.monotonic_ns()}"
        self._task_card(task_id, "Création d'image en cours", prompt[:200], "running")
        worker = asyncio.create_task(
            asyncio.to_thread(generate_image, args, self.ui),
            name="azure-image-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=20.0)
            except asyncio.TimeoutError:
                # Le premier accusé de réception vient du résultat immédiat de
                # l'outil. Cette seconde phrase n'est envoyée que si Azure
                # travaille toujours, et passe par Gemini Live : même voix.
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT IMAGE] Dis uniquement en français : « Monsieur, "
                        "la création de l'image est toujours en cours. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération Azure échouée : {exc}"

        result = str(result or "La génération d'image n'a rien renvoyé.").strip()
        failed = _looks_like_failure(result)
        self._task_card(task_id, "Création d'image — échec" if failed else "Création d'image terminée",
                        _task_result_excerpt(result), "error" if failed else "done")
        self._ui_card("show_card", "result", "Création d'image", result)
        # Pas de test `self.session is not None` : si la connexion est en
        # reprise, le tour est conservé puis livré après la reconnexion.
        delivered = await self._submit_text_turn(
            "[RÉSULTAT DE GÉNÉRATION D'IMAGE]\n"
            f"Demande : {prompt}\nRésultat : {result}\n\n"
            "Annonce uniquement le résultat en français, avec le ton Majeur. "
            "Si l'image est créée, confirme qu'elle est prête et indique son chemin. "
            "Si elle a échoué, explique la raison en français sans proposer de la relancer automatiquement.",
            timeout_s=35.0,
        )
        if not delivered:
            self.ui.write_log("WARN: image prête ; annonce vocale différée (carte disponible).")

    def _start_image_generation(self, args: dict) -> bool:
        """Une image Azure à la fois : elle peut prendre plusieurs minutes."""
        tasks = getattr(self, "_image_generation_tasks", None)
        if tasks is None:
            tasks = self._image_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_image_generation(dict(args)),
            name="azure-image-background", ui=self.ui, registry=tasks,
        )
        return True

    async def _deliver_video_generation(self, args: dict) -> None:
        """Sora travaille plusieurs minutes : tout se passe hors du tour Live."""
        from actions.video_generation import generate_video

        prompt = str(args.get("prompt") or "")
        title = "Création vidéo"
        task_id = f"video-{time.monotonic_ns()}"
        self._task_card(task_id, f"{title} en cours", prompt[:200] or "Génération Azure Sora…", "running")

        def progress(status: str) -> None:
            # Le signal Qt est thread-safe : pas besoin de repasser par la boucle.
            self._task_card(task_id, f"{title} en cours", f"Sora : {status}…", "running")

        worker = asyncio.create_task(
            asyncio.to_thread(generate_video, args, self.ui, progress),
            name="azure-video-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=25.0)
            except asyncio.TimeoutError:
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT VIDÉO] Dis uniquement en français : « Monsieur, "
                        "la vidéo est toujours en cours de génération. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération vidéo Azure échouée : {exc}"

        result = str(result or "La génération vidéo n'a rien renvoyé.").strip()
        failed = _looks_like_failure(result)
        self._task_card(task_id, f"{title} — échec" if failed else f"{title} terminée",
                        _task_result_excerpt(result), "error" if failed else "done")
        self._ui_card("show_card", "result", title, result)
        delivered = await self._submit_text_turn(
            "[RÉSULTAT DE GÉNÉRATION VIDÉO]\n"
            f"Demande : {prompt}\nRésultat : {result}\n\n"
            "Annonce uniquement le résultat en français, avec le ton Majeur. "
            "Si la vidéo est créée, confirme qu'elle est prête et indique son chemin. "
            "Si elle a échoué, explique la raison sans relancer la génération.",
            timeout_s=35.0,
        )
        if not delivered:
            self.ui.write_log("WARN: vidéo prête ; annonce vocale différée (carte disponible).")

    def _task_card(self, task_id: str, title: str, body: str, status: str) -> None:
        """Carte de tâche ; sans interface compatible, retombe sur show_card."""
        ui = getattr(self, "ui", None)
        if ui is None:
            return
        try:
            if hasattr(ui, "task_card"):
                ui.task_card(task_id, title, body, status)
            elif status == "running" and hasattr(ui, "show_card"):
                ui.show_card("task", title, body)
        except Exception as exc:
            print(f"[Dispatcher] Carte de tâche ignorée : {type(exc).__name__}: {exc}")

    def _ui_card(self, method: str, *args) -> None:
        """Appelle `show_card` / `update_card` / `dismiss_cards` sans jamais
        interrompre l'outil : une carte est décorative, mais son échec doit
        laisser une trace lisible plutôt que disparaître en silence."""
        try:
            getattr(self.ui, method)(*args)
        except Exception as exc:
            print(f"[Dispatcher] Carte UI ignorée ({method}) : {type(exc).__name__}: {exc}")

    def _safe_update_card(self, card_type: str, title: str, body: str) -> None:
        self._ui_card("update_card", card_type, title, body)

    def _start_video_generation(self, args: dict) -> bool:
        """Une vidéo Azure à la fois : Sora est lent et coûteux."""
        tasks = getattr(self, "_video_generation_tasks", None)
        if tasks is None:
            tasks = self._video_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_video_generation(dict(args)),
            name="azure-video-background", ui=self.ui, registry=tasks,
        )
        return True

    async def _deliver_decision_simulation(self, decision: str, options: list[str]) -> None:
        """Produit une carte complète et ne prononce que la recommandation."""
        from core.decision_simulator import DecisionSimulationError, run_simulation

        title = "Simulation stratégique"
        try:
            self.ui.show_card("task", title, "Préparation des scénarios…")

            def progress(message: str) -> None:
                self.ui.update_card("task", title, message)

            simulation = await asyncio.to_thread(
                run_simulation, decision, options, progress=progress
            )
        except asyncio.CancelledError:
            self.ui.dismiss_cards("task", title)
            raise
        except DecisionSimulationError as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, str(exc))
            return
        except Exception as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, f"Simulation interrompue : {str(exc)[:400]}")
            return

        self.ui.dismiss_cards("task", title)
        body = (
            f"**Décision :** {simulation.decision}\n\n"
            f"**Options simulées :** {', '.join(simulation.options)}\n"
            f"**Archivée :** {simulation.saved_at}\n\n{simulation.synthesis}"
        )
        self.ui.show_card("result", title, body[:12_000])
        # Le résultat détaillé appartient à la carte. Cette instruction assure
        # une restitution vocale courte, sans relancer un autre outil.
        try:
            await self._submit_text_turn(
                "[SIMULATION STRATÉGIQUE TERMINÉE] Résumé vocal en deux phrases "
                "maximum : annonce seulement la recommandation principale et son "
                "risque décisif, sans outil ni préambule.\n\n"
                + simulation.synthesis[:3_500]
            )
        except Exception:
            self.ui.write_log("SYS : simulation terminée ; le détail est dans la carte.")

    def _start_decision_simulation(self, decision: str, options: list[str] | None = None) -> bool:
        tasks = getattr(self, "_decision_simulation_tasks", None)
        if tasks is None:
            tasks = self._decision_simulation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_decision_simulation(decision, list(options or [])),
            name="decision-simulation-background", ui=self.ui, registry=tasks,
        )
        return True
