from __future__ import annotations

import math
import threading


from PyQt6.QtCore import (
    QEvent, Qt,
    QUrl,
)
from PyQt6.QtGui import (
    QPixmap,
)

from ui.core.qtflags import _OS
from ui.paths import CONFIG_DIR


class MediaHostMixin:
    def _show_camera_frame(self, img_bytes: bytes):
        # CameraStudio est le flux demandé par « ouvre la caméra ». L'ancien
        # chemin l'envoyait vers _CameraPreview, une vignette fixe de 244 px,
        # alors que le conteneur plein cadre existait déjà juste à côté.
        if not self._cam_cont.isVisible():
            self._on_cam_stream(True)
        self._on_cam_frame(img_bytes)

    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._dismiss_interactive_overlays()
            if hasattr(self, "_image_gallery"):
                self._image_gallery.dismiss_now()
            self._cam_cont.setGeometry(self.centralWidget().rect())
            self._cam_preview.hide()
            self._cam_cont.show()
            self._cam_cont.raise_()
        else:
            self._cam_cont.hide()
            self._cam_live_lbl.clear()
        self._sync_fullscreen_orb()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    def _close_camera_view(self) -> None:
        """Ferme immédiatement l'overlay puis libère la source en arrière-plan."""
        self._cam_stop.set()
        self._cam_cont.hide()
        self._cam_live_lbl.clear()
        self._relayout()
        self._sync_fullscreen_orb()
        callback = self.on_camera_close
        if callback:
            # CameraStudio attend brièvement la fin de son worker. Le bouton
            # doit malgré tout répondre tout de suite dans le thread Qt.
            threading.Thread(
                target=callback, daemon=True, name="camera-ui-close"
            ).start()

    # ── LA carte : une seule, plein cadre ────────────────────────────────────
    # Il n'en existe qu'une dans toute l'application. Auparavant « montre ma
    # position » chargeait un iframe OpenStreetMap dans le grand conteneur
    # pendant qu'une recherche de lieux ouvrait la carte cyberpunk dans un
    # petit panneau flottant : deux styles, deux tailles, pour la même chose.
    def _on_show_map(self, title: str, lat: float, lon: float, radius_km: float,
                     view: str = "") -> None:
        # Une vue explicitement demandée impose son mode ; sinon on garde
        # celui déjà affiché s'il y en a un (bascule persistante entre deux
        # appels), Leaflet par défaut sinon.
        mode = view or getattr(self, "_map_mode", None) or "leaflet"
        self._render_map(title, lat, lon, radius_km, places=[],
                         center_label=title or "Votre position", mode=mode)

    def _on_show_places(self, query: str, lat: float, lon: float,
                        places: list) -> None:
        """Résultats de recherche épinglés dans cette même carte."""
        # Cadrer sur le plus éloigné, avec un peu d'air : un rayon fixe laisse
        # soit des lieux hors écran, soit une carte vide autour du centre.
        radius = max(1.0, max((p.get("dist_km") or 0) for p in places) * 1.4) \
            if places else 3.0
        self._render_map(query, lat, lon, radius, places=places,
                         center_label="Votre position", count=len(places))

    # ── Deux rendus, une seule carte ─────────────────────────────────────────
    # « world monitor » (globe.gl) est l'aperçu par défaut : position et
    # résultats de recherche. Il ne sait pas guider rue par rue — dès qu'un
    # guidage démarre (_on_start_navigation), la page est rechargée en
    # Leaflet, seul rendu qui a les tuiles de rue et le moteur OSRM.
    def _render_map(self, title: str, lat: float, lon: float, radius_km: float,
                    *, places: list, center_label: str, count: int = 0,
                    mode: str = "leaflet") -> None:
        self._dismiss_interactive_overlays()
        if hasattr(self, "_image_gallery"):
            self._image_gallery.dismiss_now()
        header = f"◈  CARTE — {title}"
        if count:
            header += f"  ·  {count} lieux"
        self._map_title.setText(header[:60])
        # Mémorisé pour pouvoir recharger la même vue en Leaflet si un
        # guidage démarre pendant que le globe est affiché.
        self._map_last_args = {
            "title": title, "lat": lat, "lon": lon, "radius_km": radius_km,
            "places": places, "center_label": center_label,
        }
        if self._map_view is not None:
            if mode == "leaflet":
                from core.map_render import render_map
                html = render_map(
                    title, (lat, lon), places=places, radius_km=radius_km,
                    center_label=center_label, mark_center=True,
                )
            else:
                from core.map_render import render_globe
                html = render_globe(
                    title, (lat, lon), places=places, center_label=center_label,
                )
            self._map_mode = mode
            if hasattr(self, "_map_toggle_btn") and self._map_toggle_btn is not None:
                self._map_toggle_btn.setText(
                    "🌐  GLOBE" if mode == "leaflet" else "🗺️  CARTE"
                )
            # Une base distante est indispensable : sans elle la page est jugée
            # locale et le navigateur refuse Leaflet/globe.gl et leurs tuiles.
            self._map_view.setHtml(html, QUrl("https://unpkg.com/"))
        else:
            # Repli fonctionnel : ne jamais annoncer une carte affichée alors
            # que WebEngine manque. OSM s'ouvre dans le navigateur système.
            zoom = max(3, min(19, int(round(
                15 - math.log2(max(radius_km, 0.2))
            ))))
            from core.browser_policy import open_chrome
            open_chrome(
                f"https://www.openstreetmap.org/?mlat={lat:.7f}&mlon={lon:.7f}"
                f"#map={zoom}/{lat:.7f}/{lon:.7f}"
            )
        self._map_cont.setGeometry(self.centralWidget().rect())
        self._map_cont.show()
        self._map_cont.raise_()
        self._sync_fullscreen_orb()

    def _on_close_map(self) -> None:
        self._map_cont.hide()
        # Réapplique l'ordre des couches du HUD : le bandeau (et donc
        # l'engrenage) repasse au-dessus de toute surface qui vient de fermer.
        self._relayout()
        self._sync_fullscreen_orb()

    def _fullscreen_surface_visible(self) -> bool:
        """Point unique pour les vues qui remplacent tout le HUD central."""
        return any(
            panel is not None and panel.isVisible()
            for panel in (
                getattr(self, "_cam_cont", None),
                getattr(self, "_map_cont", None),
                getattr(self, "_image_gallery", None),
                getattr(self, "_video_hub", None),
            )
        )

    # ── bulle compagnon ─────────────────────────────────────────────────────

    def _show_speech(self, text: str, speaker: str) -> None:
        """Une seule phrase, deux destinations selon ce qui est à l'écran."""
        self._speech_overlay.show_speech(text, speaker=speaker)
        # Fenêtre absente : c'est la bulle du compagnon qui porte la réponse.
        # Seule la voix de l'assistant y passe — relire sa propre phrase dans
        # une bulle n'apprend rien à personne.
        companion = getattr(self, "_companion", None)
        if companion is not None and companion.isVisible() and speaker != "user":
            companion.say(text)

    def _on_focus_changed(self, mine: bool) -> None:
        self._focus_is_ours = bool(mine)
        self._sync_companion()

    def _companion_should_show(self) -> bool:
        """La bulle prend le relais dès que l'utilisateur est ailleurs.

        Sur un gestionnaire en mosaïque, « la fenêtre n'est plus là » ne veut
        pas dire « réduite » : elle est sur un autre bureau, ou une autre
        fenêtre a le focus. Hors Hyprland, les évènements d'activation Qt et
        l'état minimisé fournissent le même comportement sans sondage externe.
        """
        if not self.isVisible() or self.isMinimized():
            return True
        if self._focus_watcher is not None:
            return not self._focus_is_ours
        # X11, Windows, macOS ou compositeur Wayland sans socket Hyprland :
        # `ActivationChange` maintient cet état sans aucun sondage système.
        return not self._focus_is_ours

    def _sync_companion(self) -> None:
        companion = getattr(self, "_companion", None)
        if companion is None:
            return
        show = self._companion_should_show()
        if show == companion.isVisible():
            # Filet : un sommeil de calcul qui survivrait au retour de la
            # fenêtre figerait le grand orbe à l'écran, sans rien pour le
            # rattraper. L'état de rendu suit toujours ce qui est visible.
            self.hud.set_low_power(show)
            return
        if show:
            companion.show()
            companion.raise_()
            self._hint_companion_rule()
        else:
            companion.hide()
        # Un seul des deux rendus anime à la fois : c'est la règle sur cette
        # machine, où l'interface et la voix se partagent le GIL.
        self.hud.set_low_power(show)

    def _restore_from_companion(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._sync_companion()

    def _hint_companion_rule(self) -> None:
        """Rappelle une fois la règle Hyprland, sans laquelle rien ne flotte.

        Sous Wayland, une fenêtre ne décide ni de sa place ni de son rang :
        sans la règle, la bulle apparaît comme une fenêtre ordinaire, au
        milieu de l'écran, et le compagnon rate complètement son effet.
        """
        if getattr(self, "_companion_hinted", False):
            return
        self._companion_hinted = True
        try:
            self._log.append_log(
                "SYS : bulle compagnon active. Si elle ne flotte pas dans le "
                "coin, les règles Hyprland ne sont pas chargées — voir "
                "config/hyprland-ano-orb.lua (config Lua/Caelestia) ou "
                "config/hyprland-ano-orb.conf (config .conf classique)."
            )
        except Exception:
            pass

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_companion()
        elif event.type() == QEvent.Type.ActivationChange:
            # Retour dans la fenêtre : Qt le sait avant même que l'évènement
            # Hyprland traverse sa socket. Cacher ici évite une image de
            # miniature par-dessus le HUD. Sur les autres compositeurs, ce
            # chemin sert aussi de suivi de focus complet.
            if self.isActiveWindow():
                self._focus_is_ours = True
                self._sync_companion()
            elif self._focus_watcher is None:
                self._focus_is_ours = False
                self._sync_companion()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_companion()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_companion()

    def _sync_fullscreen_orb(self) -> None:
        """Mode immersif : média plein cadre et seule mini-orbe visible."""
        if not hasattr(self, "hud"):
            return
        cw = self.centralWidget()
        W, H = cw.width(), cw.height()
        compact = self._fullscreen_surface_visible()
        self._set_immersive_hud(compact)
        if compact:
            # Discret : l'orbe indique qu'ANO reste actif sans recouvrir la
            # carte, la caméra ou le lecteur.
            size = max(88, min(112, int(min(W, H) * 0.15)))
            margin = 16
            self._mini_orb.setGeometry(
                margin, H - size - margin, size, size
            )
            self._mini_orb.show()
            self._mini_orb.raise_()
        else:
            self._mini_orb.hide()

    def _set_immersive_hud(self, active: bool) -> None:
        """Masque et restaure exactement les éléments HUD hors média.

        Les grandes surfaces gardent leurs propres commandes (fermer, lecture,
        etc.). Seuls les éléments permanents situés derrière sont concernés.
        """
        if active:
            if getattr(self, "_immersive_hud_hidden", None) is not None:
                return
            hidden: dict[str, bool] = {}
            for name in (
                "hud", "_interface_frame", "_telemetry_panel", "_header_panel",
                "_status_pill", "_cmd_panel", "_content_panel", "_card_scroll",
                "_music_player_panel", "_nearby_map_panel", "_cam_preview",
                "_speech_overlay", "_thought_overlay", "_clipboard_panel",
            ):
                widget = getattr(self, name, None)
                if widget is None:
                    continue
                hidden[name] = widget.isVisible()
                if hidden[name]:
                    widget.hide()
            self._immersive_hud_hidden = hidden
            return
        hidden = getattr(self, "_immersive_hud_hidden", None)
        if hidden is None:
            return
        self._immersive_hud_hidden = None
        for name, was_visible in hidden.items():
            widget = getattr(self, name, None)
            if widget is not None and was_visible:
                widget.show()
        self._relayout()

    def show_map(self, title: str, lat: float, lon: float, radius_km: float = 3.0,
                 view: str | None = None) -> bool:
        """Thread-safe : peut être appelé depuis n'importe quel thread
        (les tools tournent dans un executor, jamais sur le thread Qt).

        ``view`` : "leaflet"/"globe" pour imposer un rendu, "" pour garder
        celui déjà affiché (comportement historique)."""
        self._map_sig.emit(title[:60], float(lat), float(lon), float(radius_km),
                           str(view or ""))
        return True

    def update_live_position(self, lat: float, lon: float, accuracy_m: float | None = None,
                             heading: float | None = None, speed: float | None = None) -> None:
        """Transmet la position GPS en direct à la carte plein écran."""
        self._live_pos_sig.emit(float(lat), float(lon), accuracy_m, heading, speed)

    def _on_update_live_position(self, lat: float, lon: float, accuracy_m: float | None,
                                 heading: float | None, speed: float | None) -> None:
        if self._map_view is not None and self._map_cont.isVisible():
            acc_str = f"{accuracy_m}" if accuracy_m is not None else "null"
            head_str = f"{heading}" if heading is not None else "null"
            speed_str = f"{speed}" if speed is not None else "null"
            js = f"if (window.ANO_UPDATE_GPS) window.ANO_UPDATE_GPS({lat}, {lon}, {acc_str}, {head_str}, {speed_str});"
            self._map_view.page().runJavaScript(js)

    def start_navigation(self, dest_lat: float, dest_lon: float, dest_name: str = "Destination") -> None:
        """Démarre le guidage pas-à-pas vers une destination."""
        self._nav_sig.emit(float(dest_lat), float(dest_lon), dest_name)

    def _on_start_navigation(self, dest_lat: float, dest_lon: float, dest_name: str) -> None:
        if self._map_view is None or not self._map_cont.isVisible():
            return
        escaped_name = dest_name.replace("'", "\\'")
        js = f"if (window.ANO_START_NAVIGATION) window.ANO_START_NAVIGATION({dest_lat}, {dest_lon}, '{escaped_name}');"
        if getattr(self, "_map_mode", "globe") != "leaflet":
            # Le globe affiché ne sait pas guider : on recharge la même vue
            # en Leaflet, puis on lance le guidage une fois la page prête.
            args = getattr(self, "_map_last_args", None) or {
                "title": dest_name, "lat": dest_lat, "lon": dest_lon,
                "radius_km": 3.0, "places": [], "center_label": dest_name,
            }
            self._render_map(
                args["title"], args["lat"], args["lon"], args["radius_km"],
                places=args["places"], center_label=args["center_label"],
                mode="leaflet",
            )

            def _run_once(ok: bool) -> None:
                try:
                    self._map_view.loadFinished.disconnect(_run_once)
                except Exception:
                    pass
                if ok:
                    self._map_view.page().runJavaScript(js)

            self._map_view.loadFinished.connect(_run_once)
        else:
            self._map_view.page().runJavaScript(js)

    def _toggle_map_view(self) -> None:
        """Bouton d'en-tête : bascule entre le globe et la carte de rues.

        Réutilise les derniers paramètres affichés — pas de nouvelle requête
        GPS, juste un autre rendu du même endroit.
        """
        args = getattr(self, "_map_last_args", None)
        if not args or self._map_view is None or not self._map_cont.isVisible():
            return
        next_mode = "globe" if getattr(self, "_map_mode", "leaflet") == "leaflet" else "leaflet"
        self._render_map(
            args["title"], args["lat"], args["lon"], args["radius_km"],
            places=args["places"], center_label=args["center_label"],
            count=len(args["places"]), mode=next_mode,
        )

    def close_map(self) -> None:
        self._map_close_sig.emit()

    def show_image_gallery(self, query: str, images: list[dict]) -> None:
        """Affiche des images déjà validées/téléchargées, sans accès UI réseau."""
        self._image_gallery_sig.emit(query[:120], images[:8])

    def show_generated_image_preview(self, prompt: str, image_bytes: bytes, path: str) -> None:
        """Aperçu court au centre ; ne déclenche jamais la galerie plein écran."""
        self._generated_image_preview_sig.emit(prompt[:120], bytes(image_bytes or b""), str(path))

    def _on_show_generated_image_preview(self, prompt: str, image_bytes: bytes, path: str) -> None:
        preview = self._generated_image_preview
        if preview.set_image(prompt, image_bytes, path):
            self._relayout()
            preview.show()
            preview.raise_()

    def show_last_generated_image(self) -> bool:
        """Ouvre explicitement le visionneur seulement à la demande utilisateur."""
        payload = self._generated_image_preview.payload()
        if not payload:
            return False
        self.show_image_gallery(payload["title"], [payload])
        return True

    def show_generated_artifact_preview(self, kind: str, title: str, path: str) -> None:
        self._generated_artifact_preview_sig.emit(str(kind), str(title), str(path))

    def _on_show_generated_artifact_preview(self, kind: str, title: str, path: str) -> None:
        preview = self._generated_artifact_preview
        preview.show_artifact(kind, title, path)
        self._relayout(); preview.show(); preview.raise_()

    def _on_show_image_gallery(self, query: str, images: list[dict]) -> None:
        # Une seule surface plein écran à la fois. La caméra est réellement
        # libérée ; la carte ne consomme aucune ressource une fois masquée.
        try:
            self._dismiss_interactive_overlays()
            if self._cam_cont.isVisible():
                self._close_camera_view()
            self._map_cont.hide()
            if self._video_hub.isVisible():
                self._video_hub.dismiss_now()
            self._image_gallery.setGeometry(self.centralWidget().rect())
            if self._image_gallery.show_gallery(query, images):
                self._image_gallery.raise_()
            else:
                # Aucune donnée image utilisable : retour au HUD sans surface
                # transparente qui intercepterait les événements.
                self._image_gallery.dismiss_now()
                if hasattr(self, "write_log"):
                    self.write_log("SYS : image reçue invalide ou trop lourde, galerie non ouverte.")
        except Exception as exc:
            # Une erreur de décodage/UI ne doit jamais faire remonter une
            # exception hors d'un slot Qt (PyQt peut alors arrêter le process).
            self._image_gallery.dismiss_now()
            if hasattr(self, "write_log"):
                self.write_log(f"SYS : affichage image ignoré ({type(exc).__name__}).")
        finally:
            self._sync_fullscreen_orb()

    def close_image_gallery(self) -> None:
        self._image_gallery_close_sig.emit()

    def show_video_results(self, query: str, videos: list[dict]) -> None:
        self._video_results_sig.emit(query[:120], videos[:12])

    def _prepare_video_surface(self) -> None:
        self._dismiss_interactive_overlays()
        if self._cam_cont.isVisible():
            self._close_camera_view()
        self._map_cont.hide()
        if self._image_gallery.isVisible():
            self._image_gallery.dismiss_now()
        self._video_hub.setGeometry(self.centralWidget().rect())

    def _on_show_video_results(self, query: str, videos: list[dict]) -> None:
        self._prepare_video_surface()
        self._video_hub.show_results(query, videos)
        self._video_hub.raise_()
        self._sync_fullscreen_orb()

    def _on_video_selected(self, index: int) -> None:
        if 0 <= index < len(self._video_hub._videos):
            selected_video = self._video_hub._videos[index]
            self.play_video(selected_video, self._video_hub._videos)
            if hasattr(self, "write_log"):
                title = selected_video.get("title") or f"vidéo #{index + 1}"
                self.write_log(f"[Vidéo] Lecture directe : {title}")
        elif self.on_text_command:
            if self._video_hub.selection_is_local(index):
                command = f"lance le résultat vidéo local numéro {index + 1}"
            else:
                command = f"ouvre le résultat YouTube numéro {index + 1}"
            threading.Thread(target=self.on_text_command, args=(command,), daemon=True).start()

    def play_video(self, video: dict, playlist: list[dict] | None = None) -> None:
        self._video_play_sig.emit(dict(video), list(playlist or []))

    def _on_play_video(self, video: dict, playlist: list[dict]) -> None:
        self._prepare_video_surface()
        self._video_hub.play_video(video, playlist)
        self._video_hub.raise_()
        self._sync_fullscreen_orb()

    def control_video(self, action: str, value=None) -> None:
        self._video_control_sig.emit(str(action), value)

    def close_video(self) -> None:
        self._video_close_sig.emit()

    def video_status(self) -> str:
        return self._video_hub.status_text()
