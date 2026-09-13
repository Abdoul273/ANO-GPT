"""Relais téléphone ANO-Remote : dashboard, GPS, audio, caméra.

Concurrence
-----------
* **asyncio** — ``DashboardServer.serve`` (FastAPI/uvicorn), relais audio
  téléphone, commandes dashboard.
* **thread FastAPI** — le serveur HTTP vit hors de la boucle Live ; les
  frames JPEG / packets PCM arrivent par files asyncio.
* **Qt** — ``CameraStudio`` (OpenCV) et overlay caméra restent sur le
  thread principal ; le relais ne fait que pousser des JPEG.
"""
from __future__ import annotations

import asyncio
import collections
import re
import time
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from actions.screen_processor import _capture_camera
from core.audio_engine import _END_SILENCE_S
from core import human_confirmation
from ui.paths import BASE_DIR


class PhoneHost(Protocol):
    ui: Any
    session: Any
    _dashboard: Any
    _camera: Any
    _phone_active: bool
    _loop: Any
    _is_speaking: bool
    _model_turn_active: bool
    _activity_open: bool

    def interrupt(self) -> None: ...
    def _activity_start(self) -> None: ...
    def _activity_end(self) -> None: ...
    def _enqueue_out(self, msg: dict) -> None: ...
    async def _submit_text_turn(self, text: str, timeout_s: float = 90.0) -> bool: ...
    def _wake_up(self, reason: str = "hotkey") -> str: ...


class PhoneRelay:
    """DashboardServer, GPS live, file audio téléphone, CameraStudio.

    Les méthodes sont liées à l'hôte ``JarvisLive``.
    """

    # ── Studio caméra (webcam PC ou caméra du téléphone) ─────────────────────

    @property
    def camera(self):
        """Studio caméra, créé au premier usage.

        L'import d'OpenCV coûte plusieurs centaines de millisecondes : le faire
        au démarrage retarderait la voix pour une fonction souvent inutilisée.
        """
        if self._camera is None:
            from core.camera_studio import CameraStudio
            self._camera = CameraStudio(
                on_frame=self.ui.show_camera_frame,
                on_state=self._on_camera_state,
                save_capture=self._save_capture,
                phone_start=lambda: self._phone_camera_command("start"),
                phone_stop=lambda: self._phone_camera_command("stop"),
                phone_lens=lambda lens: self._phone_camera_command("lens", lens=lens),
                uploads_dir=self._uploads_dir(),
                camera_index=self._camera_index(),
            )
            if self._pending_phone_frame is not None:
                # Le téléphone filmait déjà : ne pas perdre son image en cours.
                self._camera.push_phone_frame(self._pending_phone_frame)
                self._pending_phone_frame = None
        return self._camera

    def _uploads_dir(self) -> Path:
        if self._dashboard is not None:
            return self._dashboard._uploads_dir
        return Path.home() / "Downloads" / "JARVIS Uploads"

    def _camera_index(self) -> int:
        try:
            import json as _json
            cfg = _json.loads((BASE_DIR / "config" / "api_keys.json").read_text())
            return int(cfg.get("camera_index", 0))
        except Exception:
            return 0

    def _save_capture(self, data: bytes, suffix: str, label: str):
        if self._dashboard is not None:
            return self._dashboard.save_capture(data, suffix, label)
        directory = self._uploads_dir()
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / f"ano-{label}-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
        dest.write_bytes(data)
        return dest

    def _on_phone_frame(self, jpeg: bytes) -> None:
        """Image reçue du téléphone : la router vers le studio."""
        if self._camera is not None:
            self._camera.push_phone_frame(jpeg)
        else:
            self._pending_phone_frame = jpeg

    def _phone_camera_command(self, action: str, **fields) -> None:
        """Pilote la caméra du téléphone : allumage, extinction, objectif."""
        if self._dashboard is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._dashboard.send_phone_command(action, **fields), self._loop
        )

    def _on_camera_state(self, state: dict) -> None:
        self.ui.set_camera_state(state)
        controller = getattr(self, "_gesture_controller", None)
        if controller is not None:
            controller.set_camera_active(bool(state.get("active")))

    def _camera_tool(self, action: str, source: str = "", lens: str = "") -> str:
        """Exécute une commande caméra venue de la voix.

        Renvoie une phrase courte : c'est elle que le modèle lira à voix haute,
        donc elle doit décrire le résultat réel, pas l'intention.
        """
        from core.camera_studio import (
            CameraUnavailable, SOURCE_PHONE, SOURCE_PC, normalize_lens,
        )

        try:
            studio = self.camera
            wanted_lens = normalize_lens(lens)
            # « Prends-moi en photo avec la caméra frontale » : seul le
            # téléphone a deux objectifs, la source suit donc l'intention.
            if wanted_lens and not source:
                source = SOURCE_PHONE
            if not source:
                # Sans précision, on suit ce qui est déjà en route, et à froid
                # on privilégie le téléphone s'il est le seul à filmer.
                source = studio.source
                if not studio.active and studio.phone_online():
                    source = SOURCE_PHONE
            wanted = SOURCE_PHONE if source.startswith("ph") else SOURCE_PC

            if action in ("open", "start", "show"):
                message = studio.open(wanted, lens=wanted_lens)
                if wanted == SOURCE_PHONE:
                    message = self._confirm_phone_camera(studio, message)
            elif action in ("lens", "front", "back", "selfie"):
                # Le nom de l'action peut porter l'objectif à lui seul :
                # « caméra frontale » arrive parfois sans paramètre séparé.
                message = studio.set_lens(wanted_lens or lens or action)
                if studio.source == SOURCE_PHONE:
                    message = self._confirm_phone_camera(studio, message)
            elif action in ("flip", "flip_lens", "switch_lens"):
                message = studio.switch_lens()
                message = self._confirm_phone_camera(studio, message)
            elif action == "photo":
                if not studio.active:
                    studio.open(wanted, lens=wanted_lens)
                elif wanted_lens and wanted_lens != studio.lens:
                    studio.set_lens(wanted_lens)
                    # L'image en mémoire vient encore de l'autre objectif : la
                    # photo doit attendre une image réellement neuve.
                    studio.wait_fresh_frame(timeout=5.0)
                path = studio.photo()
                message = f"Photo enregistrée sous {path.name}."
            elif action in ("video_start", "record", "video"):
                if wanted_lens and wanted_lens != studio.lens:
                    studio.set_lens(wanted_lens)
                path = studio.start_video()
                message = f"Enregistrement vidéo lancé, fichier {path.name}."
            elif action in ("video_stop", "stop_video"):
                path = studio.stop_video()
                if path is not None and self._dashboard is not None:
                    self._dashboard.notify_capture(path)
                message = (f"Vidéo enregistrée sous {path.name}." if path
                           else "Aucun enregistrement n'était en cours.")
            elif action == "switch":
                message = studio.switch_source()
                if studio.source == SOURCE_PHONE:
                    message = self._confirm_phone_camera(studio, message)
            elif action in ("close", "stop"):
                message = studio.close()
            else:
                return f"Action caméra inconnue : {action}."
        except CameraUnavailable as exc:
            return f"Caméra indisponible : {exc}"
        except Exception as exc:
            return f"Erreur caméra : {exc}"

        self.ui.write_log(f"SYS: {message}")
        return message

    def _confirm_phone_camera(self, studio, message: str) -> str:
        """Vérifie que le téléphone a réellement allumé sa caméra.

        L'ordre part par le canal de contrôle : sans accusé de réception, un
        téléphone éteint ou déconnecté donnerait un conteneur noir et muet.
        """
        if self._dashboard is None or not self._dashboard._clients:
            return ("Aucun téléphone connecté : ouvrez ANO Remote et appairez-le, "
                    "ou dites « passe sur la caméra du PC ».")
        if studio.latest_frame(timeout=6.0) is None:
            return ("Le téléphone n'a pas envoyé d'image. Vérifiez qu'ANO Remote "
                    "est ouvert et que la caméra y est autorisée.")
        return message

    def _close_camera_quietly(self) -> None:
        """Ferme le studio sans repasser par l'interface, qui ferme déjà."""
        if self._camera is not None and self._camera.active:
            self._camera.close()

    def _on_camera_action(self, action: str) -> str:
        """Boutons du bandeau caméra. Renvoie la phrase affichée dans le log."""
        studio = self.camera
        if action == "photo":
            path = studio.photo()
            message = f"Photo enregistrée : {path.name}"
        elif action == "record":
            if studio.recording:
                path = studio.stop_video()
                if path is not None and self._dashboard is not None:
                    self._dashboard.notify_capture(path)
                message = f"Vidéo enregistrée : {path.name}" if path else "Enregistrement arrêté."
            else:
                path = studio.start_video()
                message = f"Enregistrement en cours : {path.name}"
        elif action == "source":
            message = studio.switch_source()
        elif action == "close":
            message = studio.close()
        else:
            message = f"Action caméra inconnue : {action}"
        self.ui.write_log(f"SYS: {message}")
        return message

    def _grab_camera_still(self) -> tuple[bytes, str]:
        """Image fixe pour la vision, prise sur le flux déjà ouvert.

        Rouvrir OpenCV pendant que le studio tient la webcam rendrait une image
        noire ; et quand la source est le téléphone, c'est bien ce que filme le
        téléphone que la vision doit analyser.
        """
        try:
            studio = self.camera
            if not studio.active:
                studio.open(studio.source)
            frame = studio.latest_frame(timeout=3.0)
            if frame:
                return frame, "image/jpeg"
        except Exception as exc:
            print(f"[Vision] Studio caméra indisponible : {exc}")
        return _capture_camera()

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        ip = self._dashboard.refresh_network_address()
        if not ip:
            self.ui.write_log(
                "SYS: Accès distant indisponible : aucune adresse Wi-Fi/LAN. "
                "Connectez le PC et le téléphone au même réseau, puis réessayez."
            )
            return None
        warning = self._dashboard.firewall_warning()
        if warning:
            # Sans cet avertissement, le téléphone se heurtait au pare-feu et
            # l'utilisateur ne voyait qu'un « serveur introuvable » inexplicable.
            self.ui.write_log(f"SYS: {warning}")
        key    = self._dashboard.new_key()
        try:
            url    = self._dashboard.get_url()
            manual = self._dashboard.get_manual_url()
        except RuntimeError as exc:
            self.ui.write_log(f"SYS: Accès distant indisponible : {exc}.")
            return None
        return url, key, f"{url}/auto-login?key={key}", manual
    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """PCM téléphone → tours complets, indépendants du micro PC.

        Les durées sont mesurées en échantillons : Android et le Wi-Fi peuvent
        livrer plusieurs blocs d'un coup, sans pause sur l'horloge du serveur.
        """
        q = self._dashboard._phone_audio_queue
        preroll = collections.deque(maxlen=7)
        speech_open = False
        attack_ms = silence_ms = total_ms = 0.0
        noise_floor = 0.003

        def finish():
            nonlocal speech_open, attack_ms, silence_ms, total_ms
            if speech_open:
                self._activity_end()
            speech_open = False
            attack_ms = silence_ms = total_ms = 0.0
            preroll.clear()

        def forward(packet):
            """Envoie un bloc au moteur Live, sans en décider la parole.

            Gemini Live possède son propre VAD.  Le seuil local ci-dessous ne
            sert donc qu'à animer/borner l'activité de l'interface et ne doit
            jamais filtrer le PCM.  Certains Android (en particulier avec AGC
            matériel) livrent une voix parfaitement intelligible autour de
            0,003--0,009 RMS : l'ancien filtre la jetait entièrement, donnant
            l'impression que le micro ANO Remote ne partait pas.
            """
            nonlocal total_ms
            pcm = np.frombuffer(packet["data"], dtype="<i2")
            total_ms += pcm.size / 16.0
            clips = getattr(self, "_voice_chunks", None)
            if clips is not None:
                clips.append(pcm.astype(np.float32) / 32768.0)
            self._enqueue_out(packet)

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    finish()
                    self._phone_active = False
                    continue
                if chunk.get("activity") == "phone_stream_end":
                    finish()
                    self._phone_active = False
                    continue
                data = chunk.get("data", b"")
                if not data or len(data) % 2:
                    continue  # Un paquet invalide ne doit pas tuer la session Live.
                if not self._phone_active:
                    if self._activity_open:
                        self._activity_end()
                    finish()
                self._phone_active = True
                with self._speaking_lock:
                    speaking = self._is_speaking
                if speaking:
                    finish()
                    continue

                # Maximum 64 ms par bloc : un seul gros paquet Android peut
                # contenir à lui seul un mot entier suivi de silence.
                for offset in range(0, len(data), 2048):
                    packet = {**chunk, "data": data[offset:offset + 2048]}
                    pcm = np.frombuffer(packet["data"], dtype="<i2")
                    values = pcm.astype(np.float32) / 32768.0
                    duration_ms = pcm.size / 16.0
                    rms = float(np.sqrt(np.mean(values ** 2)))
                    voice_now = rms >= max(0.010, noise_floor * 2.8)
                    # Le flux reste complet, y compris avant les 110 ms de
                    # confirmation locale : le VAD Gemini est l'autorité pour
                    # comprendre la voix distante.  Ne pas attendre le seuil
                    # ici, sinon une voix Android faible n'atteint jamais le
                    # modèle.
                    forward(packet)
                    if not speech_open:
                        preroll.append(packet)
                        if voice_now:
                            attack_ms += duration_ms
                        else:
                            attack_ms = 0.0
                            noise_floor = 0.96 * noise_floor + 0.04 * min(rms, 0.02)
                        if attack_ms < 110.0:
                            continue
                        self._activity_start()
                        self._voice_evidence_ms = attack_ms
                        preroll.clear()
                        speech_open = True
                        continue
                    if voice_now:
                        self._voice_evidence_ms += duration_ms
                        silence_ms = 0.0
                    else:
                        silence_ms += duration_ms
                    if silence_ms >= _END_SILENCE_S * 1000 or total_ms >= 20000:
                        finish()
        finally:
            finish()
            self._phone_active = False

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()
        # Le canal Android vient d'être annoncé : demander immédiatement un
        # relevé, sans attendre la cadence de 30 s du service de localisation.
        if self._dashboard is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self._dashboard.request_fresh_location(timeout=8.0)
                )
            except RuntimeError:
                pass

    def _show_human_confirmation(self, pending) -> None:
        """Présente la même décision au HUD et à AnoRemote, sans passer par l'IA."""
        self.ui.show_card(
            "confirmation", pending.title, pending.detail,
            [
                {
                    "label": "Confirmer", "primary": True,
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, True, source="HUD"
                    ),
                },
                {
                    "label": "Annuler",
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, False, source="HUD"
                    ),
                },
            ],
        )
        if self._loop and self._loop.is_running() and self._dashboard is not None:
            payload = {
                "type": "confirmation_request",
                "token": pending.token,
                "title": pending.title,
                "detail": pending.detail,
            }
            asyncio.run_coroutine_threadsafe(self._dashboard.broadcast(payload), self._loop)

    def _hide_human_confirmation(self, token: str) -> None:
        self.ui.dismiss_cards("confirmation")
        if self._loop and self._loop.is_running() and self._dashboard is not None:
            asyncio.run_coroutine_threadsafe(
                self._dashboard.broadcast({"type": "confirmation_closed", "token": token}),
                self._loop,
            )

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                if re.search(
                    r"\b(o[uù]\s+suis[- ]?je|dans\s+quel\s+pays|quelle\s+(?:est\s+)?ma\s+"
                    r"(?:position|ville|localisation)|ma\s+localisation|mon\s+pays)\b",
                    text.casefold(),
                ):
                    try:
                        await self._dashboard.request_fresh_location(timeout=8.0)
                    except Exception:
                        pass
                    try:
                        from core.geolocation import get_user_location
                        location = await asyncio.to_thread(get_user_location)
                        city = location.get("city") or "ville non résolue"
                        country = location.get("country_name") or "pays non résolu"
                        source = location.get("source") or "inconnue"
                        text = (
                            "[LOCALISATION VÉRIFIÉE — utilise ces données et ne "
                            "déduis jamais le pays de la langue] "
                            f"Ville/zone : {city}; pays : {country}; source : {source}.\n"
                            f"Question : {text}"
                        )
                    except Exception:
                        pass
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self._submit_text_turn(text)
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)
