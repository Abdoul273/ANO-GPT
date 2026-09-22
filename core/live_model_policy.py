"""Catalogue des modèles Gemini et sélection du modèle Live avec repli.

Source unique de vérité : **aucun identifiant de modèle ne doit être écrit en
dur ailleurs**. Ils étaient dispersés sur 48 sites dans ``core/`` et
``actions/`` ; une montée de version demandait donc 48 modifications, et il
suffisait d'en oublier une pour qu'un module continue d'appeler un modèle
retiré — panne silencieuse, découverte à l'usage.

Les rôles priment sur les noms : un appel demande « le rapide » ou « celui qui
raisonne », pas une version. Bumper une version se fait ici, une fois.

Ce module ne dépend que de la bibliothèque standard : il peut être importé de
n'importe où, y compris des actions, sans risque de cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Session vocale Live ──────────────────────────────────────────────────────
# Modèle principal en streaming bidirectionnel temps réel à ultra-faible latence.
DEFAULT_PRIMARY_MODEL = "models/gemini-3.8-live"
DEFAULT_FALLBACK_MODEL = "models/gemini-2.5-flash-native-audio-latest"
LIVE_EXTENDED_THINKING_MODEL = "models/gemini-3.8-live-extended-thinking"

# ── Rôles texte et vision ────────────────────────────────────────────────────
#: Extraction, classification, reformulation courte. Le plus appelé, de loin :
#: il doit rendre la main vite, la voix attend derrière.
FAST_MODEL = "gemini-flash-lite-latest"
#: Rédaction, synthèse, planification légère.
BALANCED_MODEL = "gemini-flash-latest"
#: Raisonnement long ou analyse visuelle exigeante.
REASONING_MODEL = "gemini-pro-latest"
#: Aperçu de génération suivante, essayé avant REASONING_MODEL en vision.
REASONING_PREVIEW_MODEL = "gemini-3-pro-preview"

# ── Versions épinglées ───────────────────────────────────────────────────────
# Les alias « -latest » suivent les montées de version tout seuls ; ceux-ci
# sont figés et sont donc les premiers à vieillir. Ce sont eux qu'il faut
# revoir en priorité lors d'une mise à jour.
PINNED_PRO_MODEL = "gemini-2.5-pro"
PINNED_FLASH_MODEL = "gemini-2.5-flash"
#: Analyse d'écran par session Live audio-natif.
SCREEN_LIVE_MODEL = "models/gemini-3.8-live"
#: Seconde passe de transcription pour les commandes sensibles.
TRANSCRIBE_MODEL = "models/gemini-3.8-live"

#: Rôle → identifiant, pour les usages dynamiques (diagnostic, configuration).
MODEL_CATALOG: dict[str, str] = {
    "live_primary": DEFAULT_PRIMARY_MODEL,
    "live_fallback": DEFAULT_FALLBACK_MODEL,
    "live_thinking": LIVE_EXTENDED_THINKING_MODEL,
    "fast": FAST_MODEL,
    "balanced": BALANCED_MODEL,
    "reasoning": REASONING_MODEL,
    "reasoning_preview": REASONING_PREVIEW_MODEL,
    "pinned_pro": PINNED_PRO_MODEL,
    "pinned_flash": PINNED_FLASH_MODEL,
    "screen_live": SCREEN_LIVE_MODEL,
    "transcribe": TRANSCRIBE_MODEL,
}


def model_for(role: str) -> str:
    """Identifiant du rôle demandé. Un rôle inconnu retombe sur le rapide."""
    return MODEL_CATALOG.get(str(role or "").strip().casefold(), FAST_MODEL)

_MODEL_FAILURE_MARKERS = (
    "model not found", "model is not found", "not supported for bidirectional",
    "not supported for live", "unsupported model", "unknown model",
    "models/ is not found", "requested entity was not found",
)


@dataclass
class LiveModelPolicy:
    primary: str = DEFAULT_PRIMARY_MODEL
    fallback: str = DEFAULT_FALLBACK_MODEL
    active: str = ""

    def __post_init__(self) -> None:
        self.primary = str(self.primary or DEFAULT_PRIMARY_MODEL).strip()
        self.fallback = str(self.fallback or DEFAULT_FALLBACK_MODEL).strip()
        self.active = str(self.active or self.primary).strip()

    @property
    def current(self) -> str:
        return self.active

    @property
    def using_fallback(self) -> bool:
        return self.active == self.fallback and self.fallback != self.primary

    def should_fallback(self, error: BaseException | str) -> bool:
        if self.using_fallback or self.primary == self.fallback:
            return False
        message = " ".join(str(error).casefold().split())
        return any(marker in message for marker in _MODEL_FAILURE_MARKERS)

    def activate_fallback(self) -> str:
        self.active = self.fallback
        return self.active
