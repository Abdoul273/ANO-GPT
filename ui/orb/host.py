"""Hôte de l'orbe : ``window.hud`` ne change jamais, l'orbe dedans si.

Garanties :

* **Un seul orbe vivant.** Changer de style crée le nouveau, puis détruit
  l'ancien (minuteries arrêtées, ``shutdown``, ``deleteLater``) : son CPU,
  sa mémoire et son éventuel contexte GPU sont rendus. Les styles jamais
  choisis ne sont même pas importés (voir ``ui.orb.registry``).
* **Aucune coupure visible.** L'hôte tient un ``OrbSnapshot`` à jour et le
  rejoue sur le nouvel orbe avant de l'afficher : état, volume, muet,
  couleur d'accent, veille, calque photo, vision continue.
* **Jamais d'écran vide.** Si un style échoue à naître, l'orbe actuel reste
  en place ; au démarrage, repli sur l'orbe principal.
* **Compatibilité.** Tout attribut inconnu de l'hôte est lu sur l'orbe actif
  (``_ws``, ``_volume``, ``_PALETTES``… pour MiniOrb, Companion, réglages
  audio) : le code existant fonctionne sans modification.
"""
from __future__ import annotations

import sys
import traceback

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from ui.orb import registry
from ui.orb.contract import OrbSnapshot


def _log(message: str) -> None:
    print(f"[Orbe] {message}", file=sys.stderr)


class OrbHost(QWidget):
    """Conteneur plein cadre d'un unique orbe interchangeable."""

    # Émis après un changement de style effectif : (id du style).
    style_changed = pyqtSignal(str)

    def __init__(self, face_path: str, assistant_name: str = "ANO-GPT",
                 style_id: str | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(300, 300)
        self.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._face_path = face_path
        self._snapshot = OrbSnapshot(assistant_name=assistant_name)
        self._orb: QWidget | None = None
        self._style = ""
        self._swapping = False

        wanted = registry.resolve(style_id)
        if not self.set_style(wanted):
            if wanted == registry.DEFAULT_ORB or not self.set_style(registry.DEFAULT_ORB):
                raise RuntimeError("aucun orbe n'a pu être créé")

    # ══ Changement de style ═════════════════════════════════════════════════
    @property
    def style_id(self) -> str:
        return self._style

    @property
    def orb(self) -> QWidget | None:
        """L'orbe actif (à ne pas conserver : il peut être remplacé)."""
        return self._orb

    def set_style(self, style_id: str) -> bool:
        """Remplace l'orbe actif. False (et rien ne change) en cas d'échec."""
        spec = registry.get(style_id)
        if spec is None or not registry.is_usable(spec):
            _log(f"style ignoré (inconnu ou indisponible) : {style_id!r}")
            return False
        if spec.id == self._style and self._orb is not None:
            return True
        if self._swapping:
            return False
        self._swapping = True
        try:
            try:
                new_orb = registry.create(spec.id, self._face_path,
                                          self._snapshot.assistant_name, self)
            except Exception as exc:
                _log(f"« {spec.id} » n'a pas pu démarrer : {type(exc).__name__}: {exc}")
                traceback.print_exc(file=sys.stderr)
                return False
            try:
                self._replay(new_orb)
            except Exception as exc:
                _log(f"« {spec.id} » a refusé l'état courant : {exc}")
                self._retire(new_orb)
                return False
            old, self._orb, self._style = self._orb, new_orb, spec.id
            self._layout.addWidget(new_orb)
            if self.isVisible():
                new_orb.show()
            if old is not None:
                self._retire(old)
        finally:
            self._swapping = False
        self.style_changed.emit(spec.id)
        return True

    def _replay(self, orb) -> None:
        s = self._snapshot
        orb.muted = s.muted
        orb.speaking = s.speaking
        orb.state = s.state
        # Méthodes facultatives : les anciens moteurs n'ont pas tout.
        for name, args in (
            ("set_assistant_name", (s.assistant_name,)),
            ("set_volume", (s.volume,)),
            ("set_background_image_active", (s.background_active,)),
            ("set_continuous_vision", (s.continuous_vision,)),
            ("set_accent_color", (s.accent_hex, s.custom_palette)
             if (s.accent_hex or s.custom_palette) else None),
            ("set_low_power", (s.low_power,)),
        ):
            method = getattr(orb, name, None)
            if args is not None and callable(method):
                method(*args)

    def _retire(self, orb: QWidget) -> None:
        """Détruit un orbe pour de bon : plus rien ne tourne après ceci."""
        try:
            orb.hide()
            self._layout.removeWidget(orb)
            shutdown = getattr(orb, "shutdown", None)
            if callable(shutdown):
                shutdown()
            # Filet pour les styles sans ``shutdown`` (HudCanvas, GLSL) :
            # aucune minuterie ne doit survivre jusqu'au deleteLater.
            for timer in orb.findChildren(QTimer):
                timer.stop()
            orb.setParent(None)
            orb.deleteLater()
        except Exception as exc:
            _log(f"retrait incomplet de l'ancien orbe : {exc}")

    # ══ Contrat relayé ══════════════════════════════════════════════════════
    def _call(self, name: str, *args) -> None:
        method = getattr(self._orb, name, None)
        if not callable(method):
            return
        try:
            method(*args)
        except Exception as exc:
            _log(f"{self._style}.{name} : {type(exc).__name__}: {exc}")

    @property
    def muted(self) -> bool:
        return self._snapshot.muted

    @muted.setter
    def muted(self, v: bool) -> None:
        self._snapshot.muted = bool(v)
        if self._orb is not None:
            self._orb.muted = self._snapshot.muted

    @property
    def speaking(self) -> bool:
        return self._snapshot.speaking

    @speaking.setter
    def speaking(self, v: bool) -> None:
        self._snapshot.speaking = bool(v)
        if self._orb is not None:
            self._orb.speaking = self._snapshot.speaking

    @property
    def state(self) -> str:
        return self._snapshot.state

    @state.setter
    def state(self, v: str) -> None:
        self._snapshot.state = str(v or "IDLE")
        if self._orb is not None:
            self._orb.state = self._snapshot.state

    @property
    def continuous_vision(self) -> bool:
        return self._snapshot.continuous_vision

    @continuous_vision.setter
    def continuous_vision(self, active: bool) -> None:
        self.set_continuous_vision(active)

    def set_assistant_name(self, name: str) -> None:
        self._snapshot.assistant_name = str(name or "")
        setter = getattr(self._orb, "set_assistant_name", None)
        if callable(setter):
            self._call("set_assistant_name", self._snapshot.assistant_name)
        elif self._orb is not None:
            self._orb._assistant_name = self._snapshot.assistant_name

    def set_volume(self, v: float) -> None:
        try:
            self._snapshot.volume = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return
        self._call("set_volume", v)

    def set_audio_bands(self, bands) -> None:
        self._call("set_audio_bands", bands)

    def set_low_power(self, low: bool) -> None:
        self._snapshot.low_power = bool(low)
        self._call("set_low_power", self._snapshot.low_power)

    def set_background_image_active(self, active: bool) -> None:
        self._snapshot.background_active = bool(active)
        self._call("set_background_image_active", self._snapshot.background_active)

    def set_accent_color(self, accent_hex: str, custom_palette: dict | None = None) -> None:
        self._snapshot.accent_hex = str(accent_hex or "")
        self._snapshot.custom_palette = dict(custom_palette) if custom_palette else None
        self._call("set_accent_color", accent_hex, custom_palette)

    def set_continuous_vision(self, active: bool) -> None:
        self._snapshot.continuous_vision = bool(active)
        self._call("set_continuous_vision", self._snapshot.continuous_vision)

    def show_gesture_feedback(self, icon: str, label: str = "", value: float = 0.0,
                              duration: float = 1.6) -> None:
        self._call("show_gesture_feedback", icon, label, value, duration)

    def show_clock_particles(self, duration: float = 8.5) -> None:
        self._call("show_clock_particles", duration)

    def update(self, *args) -> None:
        super().update(*args)
        if self._orb is not None and not args:
            self._orb.update()

    # ══ Compatibilité ═══════════════════════════════════════════════════════
    def __getattr__(self, name: str):
        # Appelé seulement si l'attribut n'existe pas sur l'hôte.
        orb = self.__dict__.get("_orb")
        if orb is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(orb, name)
