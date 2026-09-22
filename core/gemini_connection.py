"""Diagnostic des erreurs de connexion Gemini, et reprise de session.

Deux choses vivent ici : reconnaître ce qui a cassé (plus bas), et décider
comment revenir (`ConnectionState`). La décision est volontairement séparée de
`main.py` — elle n'a besoin ni de Qt, ni du réseau, ni d'audio pour être lue et
vérifiée, alors qu'elle gouverne ce que l'utilisateur ressent quand le Wi-Fi
hoquette.
"""

from __future__ import annotations

import time


_INVALID_KEY_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "api key expired",
    "api key was reported as leaked",
    "permission_denied: consumer",
)

_INVALID_SETUP_MARKERS = (
    "invalid json payload",
    "unknown name",
    "cannot find field",
    "invalid argument",
)

_QUOTA_MARKERS = (
    "quota exceeded", "exceeded your current quota", "resource_exhausted",
    "billing details", "rate_limit_exceeded", "rate limit exceeded",
)


def is_invalid_api_key_error(error: BaseException | str) -> bool:
    """Le code WebSocket 1007 seul ne signifie PAS clé invalide.

    Gemini emploie aussi 1007 pour un champ de configuration inconnu. C'est ce
    qui renvoyait l'utilisateur vers l'écran de clé alors que sa clé fonctionnait.
    """
    message = str(error).lower()
    return any(marker in message for marker in _INVALID_KEY_MARKERS)


def is_invalid_live_setup_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _INVALID_SETUP_MARKERS)


def is_quota_exhausted_error(error: BaseException | str) -> bool:
    """True pour un refus durable de quota/facturation Gemini.

    Un code WebSocket 1011 seul reste ambigu. Seul le texte explicite du
    serveur active cette voie afin de ne pas ralentir une vraie panne réseau.
    """
    message = str(error).casefold()
    return any(marker in message for marker in _QUOTA_MARKERS)


def safe_error_summary(error: BaseException | str, limit: int = 320) -> str:
    """Message compact pour l'UI, sans lignes de pile ni texte illimité."""
    message = " ".join(str(error).split())
    return message[:limit] or type(error).__name__


class ConnectionState:
    """Ce qu'il faut savoir entre deux sessions Gemini Live.

    Trois responsabilités, et rien d'autre :

    * **le jeton de reprise** — Gemini envoie régulièrement une poignée
      (`session_resumption_update`) qui, renvoyée à la connexion suivante,
      redonne la conversation là où elle s'est arrêtée. Elle était demandée
      dans la configuration mais n'était jamais recueillie : chaque coupure
      repartait donc d'un modèle amnésique ;
    * **le rythme des tentatives** — une seconde, puis deux, quatre… plafonné à
      trente. Repartir toutes les trois secondes indéfiniment tape sur un
      serveur qui vient de dire non ; attendre soixante secondes après un
      hoquet de Wi-Fi rend l'assistant absent pour rien ;
    * **ce que l'utilisateur doit voir** — une coupure de trois secondes se
      rattrape sans rien dire. Elle ne mérite ni carte d'erreur, ni excuse, ni
      souvenir de « fin de conversation ». Seule une vraie absence se raconte.
    """

    FIRST_DELAY = 1.0
    MAX_DELAY = 30.0

    # Au-delà, le serveur a de toute façon oublié la session : renvoyer une
    # poignée périmée coûte un aller-retour et un refus.
    HANDLE_TTL = 900.0

    # En dessous, l'utilisateur n'a rien eu le temps de remarquer : on se tait.
    SILENT_OUTAGE = 12.0

    # Au-delà, la conversation est vraiment finie : on en écrit le souvenir et
    # on repart d'une page neuve plutôt que de faire semblant de continuer.
    LOST_CONVERSATION = 180.0

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._handle: str | None = None
        self._handle_at: float = 0.0
        self._attempts: int = 0
        self._down_since: float | None = None
        self._was_connected: bool = False
        self.planned_disconnect: bool = False

    # ── évènements ────────────────────────────────────────────────────────

    def on_connected(self) -> None:
        """Session ouverte : le compteur de tentatives repart de zéro."""
        self._attempts = 0
        self._down_since = None
        self._was_connected = True
        self.planned_disconnect = False

    def on_handle(self, handle: str | None) -> None:
        if handle:
            self._handle = handle
            self._handle_at = self._clock()

    def on_go_away(self) -> None:
        """Gemini annonce qu'il ferme : la coupure est prévue, pas subie."""
        self.planned_disconnect = True

    def on_disconnected(self) -> None:
        if self._down_since is None:
            self._down_since = self._clock()
        self._attempts += 1

    def forget_session(self) -> None:
        """Oublie la reprise : la configuration a été refusée, ou trop de temps
        a passé. Mieux vaut une session neuve qu'une poignée que le serveur
        rejettera à chaque essai."""
        self._handle = None
        self._handle_at = 0.0

    # ── décisions ─────────────────────────────────────────────────────────

    def resume_handle(self) -> str | None:
        if not self._handle:
            return None
        if self._clock() - self._handle_at > self.HANDLE_TTL:
            self.forget_session()
            return None
        return self._handle

    def next_delay(self) -> float:
        """1, 2, 4, 8, 16, 30, 30… Une coupure annoncée se rattrape tout de
        suite : le serveur n'est pas en panne, il tourne la page."""
        if self.planned_disconnect:
            return 0.0
        if self._attempts <= 1:
            return self.FIRST_DELAY
        return min(self.FIRST_DELAY * (2 ** (self._attempts - 1)), self.MAX_DELAY)

    def outage_seconds(self) -> float:
        if self._down_since is None:
            return 0.0
        return self._clock() - self._down_since

    def is_silent_outage(self) -> bool:
        """Coupure à traverser sans rien dire : brève, ou annoncée par le
        serveur — dans ce dernier cas elle est parfaitement normale."""
        return (self.planned_disconnect
                or self.outage_seconds() < self.SILENT_OUTAGE)

    def should_apologize(self) -> bool:
        """L'absence s'est vue : la reprise se dit, elle ne se cache pas."""
        return (self._was_connected
                and not self.planned_disconnect
                and self.outage_seconds() >= self.SILENT_OUTAGE)

    def conversation_lost(self) -> bool:
        """Trop long, ou plus de reprise possible : c'est une autre séance."""
        return (self.resume_handle() is None
                or self.outage_seconds() >= self.LOST_CONVERSATION)
