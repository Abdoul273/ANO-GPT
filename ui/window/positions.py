from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QDragEnterEvent, QDropEvent


class PositionsMixin:
    def _relayout(self) -> None:
        lay = self.centralWidget().layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()

    def show_nearby_map(self, query: str, center_lat: float, center_lon: float, places: list):
        self._nearby_map_sig.emit(query, float(center_lat), float(center_lon), places)

    def _on_show_nearby_map(self, query: str, center_lat: float, center_lon: float, places: list):
        """Les lieux vont dans LA grande carte, jamais dans un second panneau.

        Le petit panneau flottant ne reste que comme filet de sécurité, quand
        PyQt6-WebEngine manque et que la grande carte ne peut pas s'afficher.
        """
        if self._map_view is not None:
            self._on_show_places(query, center_lat, center_lon, places)
            return
        if hasattr(self, "_nearby_map_panel"):
            self._nearby_map_panel.load_places(query, center_lat, center_lon, places)
            self._position_nearby_map()

    def _position_nearby_map(self):
        if hasattr(self, "_nearby_map_panel"):
            cw = self.centralWidget()
            w = min(540, cw.width() - 32)
            h = min(440, cw.height() - 140)
            self._nearby_map_panel.setGeometry(cw.width() - w - 16, 74, w, h)
            self._nearby_map_panel.raise_()

    def show_music_download(self, payload: dict):
        """Carte de téléchargement musical, mise à jour depuis un fil yt-dlp."""
        self._download_card_sig.emit(dict(payload or {}))

    def _on_music_download(self, payload: dict):
        if hasattr(self, "_card_stack"):
            self._card_stack.upsert_download_card(payload or {})
            self._position_card_stack()

    def show_card(self, card_type: str, title: str, body: str, actions: list[dict] = None):
        """API unifiée pour afficher une carte riche flottante à droite,
        empilée juste sous le bandeau d'en-tête (et sous le lecteur
        musique quand il est visible).
        Types : message | result | task | confirmation | info
        Exemple : ui.show_card("message", "Nouveau message", "Contenu Markdown...", [{"label": "Répondre", "primary": True}])
        """
        self._card_sig.emit(card_type, title, body, actions or [])

    def _on_show_card(self, card_type: str, title: str, body: str, actions: list):
        if hasattr(self, "_card_stack"):
            self._card_stack.add_card(card_type, title, body, actions)
            self._position_card_stack()
        if card_type == "confirmation" or "confirm" in str(card_type).lower():
            if hasattr(self, "_card_scroll"):
                self._card_scroll.show()
                self._card_scroll.raise_()

    def _on_update_card(self, card_type: str, title: str, body: str):
        if hasattr(self, "_card_stack"):
            self._card_stack.update_card(card_type, title, body)

    def dismiss_cards(self, card_type: str = "", title: str = ""):
        """Demande thread-safe de fermeture des cartes temporaires."""
        self._dismiss_cards_sig.emit(card_type, title)

    def _on_dismiss_cards(self, card_type: str, title: str):
        if hasattr(self, "_card_stack"):
            self._card_stack.dismiss_cards(card_type, title)

    def _right_column_top(self) -> int:
        """Ancre les notifications sous l'horloge ou le lecteur musique."""
        header = getattr(self, "_header_panel", None)
        top = header.y() + header.height() + 10 if header is not None else 80
        music = getattr(self, "_music_player_panel", None)
        if music is not None and music.isVisible():
            top = max(top, music.y() + music.height() + 10)
        return top

    def _position_card_stack(self):
        if hasattr(self, "_card_scroll"):
            self._relayout()
            return
        if hasattr(self, "_card_stack"):
            cw = self.centralWidget()
            w = self._card_stack.width()
            top = self._right_column_top()
            h = min(cw.height() - top - 96, self._card_stack.sizeHint().height() or 400)
            self._card_stack.setGeometry(cw.width() - w - 16, top, w, h)
            self._card_stack.raise_()

    def _on_music_status(self, status: dict):
        self._music_player_panel.update_status(status)
        self._position_music_panel()
        self._position_card_stack()

    def _position_music_panel(self):
        """Carte lecteur musique : colonne de droite, sous le bandeau
        d'en-tête, au-dessus des notifications."""
        if not hasattr(self, "_music_player_panel"):
            return
        cw = self.centralWidget()
        W = cw.width()
        pw = self._music_player_panel.width()
        ph = self._music_player_panel.sizeHint().height() or 180
        self._music_player_panel.setGeometry(W - pw - 16, 74, pw, ph)
        self._music_player_panel.raise_()

    def _position_speech_overlay(self):
        """Bulle de transcription vocale : ancrée en bas, juste au-dessus
        de la barre de saisie. Hauteur toujours fixe ; seule la largeur
        s'ajuste à la longueur du texte (une ligne, jamais de retour à la
        ligne — voir CenterSpeechOverlay.fit_to_width)."""
        if not hasattr(self, "_speech_overlay"):
            return
        cw = self.centralWidget()
        W, H = cw.width(), cw.height()

        max_w = min(int(W * 0.82), W - 48)
        ow = self._speech_overlay.fit_to_width(max_w)
        oh = self._speech_overlay.FIXED_HEIGHT

        cmd_top = self._cmd_panel.y() if hasattr(self, "_cmd_panel") else H - 80
        oy = cmd_top - oh - 14
        ox = (W - ow) // 2

        self._speech_overlay.animate_to(QRect(ox, oy, ow, oh))

    def _position_thought_overlay(self):
        """Bulle de pensée en cours : ancrée sous l'orbe central."""
        if not hasattr(self, "_thought_overlay"):
            return
        cw = self.centralWidget()
        W, H = cw.width(), cw.height()
        tw = min(540, W - 48)
        th = getattr(self._thought_overlay, "FIXED_HEIGHT", 56)
        orb_cy = H / 2.0
        orb_r = min(W, H) * 0.185
        thought_y = int(orb_cy + orb_r + 14)
        thought_x = (W - tw) // 2
        self._thought_overlay.animate_to(QRect(thought_x, thought_y, tw, th))
        self._thought_overlay.raise_()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            if hasattr(self, "_drop_overlay"):
                cw = self.centralWidget()
                self._drop_overlay.setGeometry(0, 0, cw.width(), cw.height())
                self._drop_overlay.show()
                self._drop_overlay.raise_()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        if hasattr(self, "_drop_overlay"):
            self._drop_overlay.hide()

    def dropEvent(self, e: QDropEvent):
        if hasattr(self, "_drop_overlay"):
            self._drop_overlay.hide()
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path and Path(path).is_file():
                self._on_file_selected(path)
