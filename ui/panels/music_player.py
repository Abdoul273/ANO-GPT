from __future__ import annotations



from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QFont, QPixmap,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.panels.floating_panel import FloatingPanel
from ui.panels.music_widgets import _CoverArt, _Marquee, _NeonSeek, _Spectrum
from ui.styles.theme import C

class MusicPlayerPanel(FloatingPanel):
    """Carte lecteur de musique flottante intégrée (style JARVIS HUD).
    Rendu : pochette album, titre défilant, artiste, progression cliquable,
    contrôles (précédent, pause/lecture, suivant, volume, shuffle, stop).
    """
    seek_requested = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__("Lecteur Musique", closeable=True, parent=parent)
        self.setFixedWidth(310)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(10)

        self.cover_lbl = _CoverArt()
        top_row.addWidget(self.cover_lbl, alignment=Qt.AlignmentFlag.AlignTop)

        info_col = QVBoxLayout()
        info_col.setSpacing(1)

        self.state_lbl = QLabel("EN ATTENTE")
        self.state_lbl.setFont(Hud.micro_font(6, 2.0))
        self.state_lbl.setStyleSheet(
            "color: rgba(0, 212, 255, 0.55); background: transparent;")
        info_col.addWidget(self.state_lbl)

        self.title_lbl = _Marquee()
        self.title_lbl.setText("Aucune lecture")
        self.title_lbl.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self.title_lbl.setFixedHeight(16)
        self.title_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        info_col.addWidget(self.title_lbl)

        self.artist_lbl = QLabel("ANO-GPT Media")
        self.artist_lbl.setFont(QFont("Inter", 8))
        self.artist_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        info_col.addWidget(self.artist_lbl)

        top_row.addLayout(info_col, stretch=1)
        lay.addLayout(top_row)

        self.spectrum = _Spectrum()
        lay.addWidget(self.spectrum)

        prog_row = QHBoxLayout()
        prog_row.setContentsMargins(0, 0, 0, 0)
        prog_row.setSpacing(8)

        # Les durées sont cadrées par l'alignement, pas par une police à chasse
        # fixe : demander Monospace ferait substituer toute la famille et ces
        # deux libellés jureraient avec le reste du panneau.
        time_font = QFont("Inter", 7, QFont.Weight.Bold)

        self.time_lbl = QLabel("00:00")
        self.time_lbl.setFont(time_font)
        self.time_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        prog_row.addWidget(self.time_lbl)

        self.slider = _NeonSeek()
        self.slider.sliderMoved.connect(self._on_seek_moved)
        prog_row.addWidget(self.slider, stretch=1)

        self.dur_lbl = QLabel("00:00")
        self.dur_lbl.setFont(time_font)
        self.dur_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        prog_row.addWidget(self.dur_lbl)

        lay.addLayout(prog_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 3, 0, 0)
        ctrl_row.setSpacing(6)

        def _btn(icon_name, tip, *, size=15, primary=False, box=(32, 27)):
            b = HudButton(icon=icon_name, size=size, primary=primary,
                          accent=C.PRI if primary else C.TEXT_MED,
                          hover_accent=None if primary else C.PRI)
            b.setFixedSize(*box)
            b.setToolTip(tip)
            return b

        self.btn_shuffle = _btn("shuffle", "Lecture aléatoire", size=13)
        self.btn_prev = _btn("skip-back", "Morceau précédent")
        # La lecture est l'action principale : elle est pleine et plus large,
        # pour être trouvée sans regarder.
        self.btn_play = _btn("play", "Lecture / Pause", size=18, primary=True,
                             box=(52, 27))
        self.btn_next = _btn("skip-forward", "Morceau suivant")
        self.btn_stop = _btn("square", "Arrêter", size=13)
        self.btn_stop.set_hover_accent(C.RED)

        ctrl_row.addStretch()
        for button in (self.btn_shuffle, self.btn_prev, self.btn_play,
                       self.btn_next, self.btn_stop):
            ctrl_row.addWidget(button)
        ctrl_row.addStretch()

        lay.addLayout(ctrl_row)
        self.add_widget(content)

    def update_status(self, status: dict):
        state = status.get("state", "stopped")
        if state == "stopped" and self.isVisible():
            self.fade_out()
            return
        elif state in ("playing", "paused") and not self.isVisible():
            self.fade_in()

        title = status.get("title") or "Lecture"
        artist = status.get("artist") or "Musique"
        # Le titre complet est confié au défilement : le tronquer cacherait
        # justement ce que l'utilisateur cherche à lire.
        if self.title_lbl.text() != title:
            self.title_lbl.setText(title)
        self.artist_lbl.setText(artist[:36])

        pos = status.get("pos", 0.0)
        dur = status.get("duration", 0.0)
        if dur > 0:
            pct = int((pos / dur) * 1000)
            self.slider.setValue(pct)
            m_pos, s_pos = int(pos // 60), int(pos % 60)
            m_dur, s_dur = int(dur // 60), int(dur % 60)
            self.time_lbl.setText(f"{m_pos:02d}:{s_pos:02d}")
            self.dur_lbl.setText(f"{m_dur:02d}:{s_dur:02d}")

        playing = state == "playing"
        self.state_lbl.setText("EN LECTURE" if playing else "EN PAUSE")
        self.state_lbl.setStyleSheet(
            f"color: {C.GREEN if playing else C.NEON_AMBER}; background: transparent;")
        thumb_bytes = status.get("thumbnail_bytes")
        if thumb_bytes:
            pix = QPixmap()
            if pix.loadFromData(thumb_bytes):
                self.cover_lbl.setPixmap(pix)
        elif not status.get("thumbnail"):
            self.cover_lbl.setPixmap(None)
        self.cover_lbl.set_playing(playing)
        self.spectrum.set_playing(playing)
        self.slider.set_live(playing)
        self.btn_play.set_icon_name("pause" if playing else "play")

    def _on_seek_moved(self, val: int):
        pct = val / 1000.0
        self.seek_requested.emit(pct)
