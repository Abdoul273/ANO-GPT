"""Barrière de confirmation émise par l'interface, jamais par le modèle.

Le modèle ne reçoit aucun secret lui permettant d'approuver une opération.
Seul le HUD ou un client AnoRemote authentifié peut renvoyer l'identifiant
aléatoire associé à la demande visible par l'utilisateur.
"""
from __future__ import annotations

import logging

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable


TIMEOUT_SECONDS = 90.0
# Une action déjà exécutée ne doit pas repartir parce que le modèle rappelle
# l'outil : c'est ainsi qu'un même SMS partait trois fois de suite.
REPLAY_WINDOW_SECONDS = 120.0


@dataclass(frozen=True)
class PendingConfirmation:
    token: str
    key: str
    title: str
    detail: str
    callback: Callable[[], str | None]
    created_at: float


_pending: PendingConfirmation | None = None
_recent: dict[tuple[str, str, str], float] = {}
_lock = threading.RLock()
_show: Callable[[PendingConfirmation], None] | None = None
_hide: Callable[[str], None] | None = None
_log: Callable[[str], None] | None = None
_notify: Callable[[str], None] | None = None


def bind(*, show: Callable[[PendingConfirmation], None],
         hide: Callable[[str], None] | None = None,
         log: Callable[[str], None] | None = None,
         notify: Callable[[str], None] | None = None) -> None:
    global _show, _hide, _log, _notify
    _show, _hide, _log, _notify = show, hide, log, notify


def _send_notify(message: str) -> None:
    if _notify:
        try:
            _notify(message)
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans _send_notify")


def _write_log(message: str) -> None:
    if _log:
        try:
            _log(message)
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans _write_log")


def _fingerprint(pending: PendingConfirmation) -> tuple[str, str, str]:
    return (pending.key, pending.title, pending.detail)


def request(key: str, title: str, detail: str,
            callback: Callable[[], str | None]) -> str:
    """Affiche une demande et gare l'action jusqu'au clic humain."""
    global _pending
    if not callable(callback):
        return "Action refusée : aucune opération valide à confirmer."
    if _show is None:
        return "Action refusée : l'interface de confirmation n'est pas disponible."

    pending = PendingConfirmation(
        token=secrets.token_urlsafe(24),
        key=str(key)[:80],
        title=str(title)[:120],
        detail=str(detail)[:500],
        callback=callback,
        created_at=time.monotonic(),
    )
    superseded: str | None = None
    with _lock:
        now = time.monotonic()
        for stale in [f for f, at in _recent.items()
                      if now - at > REPLAY_WINDOW_SECONDS]:
            _recent.pop(stale, None)
        if _fingerprint(pending) in _recent:
            # Exactement la même opération vient d'aboutir : la relancer
            # doublerait l'appel ou le message aux yeux du destinataire.
            return (
                "[ACTION_DEJA_EXECUTEE] Cette opération identique a déjà été "
                "déclenchée. Ne la relance pas : vérifie son résultat avant "
                "d'annoncer qu'elle a réussi."
            )
        current_pending = _pending
        if (current_pending is not None
                and now - current_pending.created_at <= TIMEOUT_SECONDS):
            if _fingerprint(current_pending) == _fingerprint(pending):
                # Le modèle redemande la même chose : garder la carte visible
                # plutôt que d'en empiler une seconde, identique.
                return (
                    "[CONFIRMATION_HUMAINE_EN_ATTENTE] La même demande est déjà "
                    "affichée. N'appelle plus cet outil : attends la décision."
                )
            if current_pending.key != pending.key:
                return (
                    "[CONFIRMATION_HUMAINE_EN_ATTENTE] Une autre décision est déjà "
                    "affichée. Attends sa confirmation ou son annulation."
                )
            # Même nature d'action, contenu corrigé (« pas WhatsApp, un SMS ») :
            # la nouvelle carte remplace l'ancienne au lieu de la bloquer.
            superseded = current_pending.token
        _pending = pending
    if superseded and _hide:
        try:
            _hide(superseded)
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans request")
    try:
        _show(pending)
    except Exception as exc:
        with _lock:
            if _pending is pending:
                _pending = None
        return f"Action refusée : impossible d'afficher la confirmation ({exc})."
    _write_log(f"SYS : confirmation humaine requise — {pending.title}")
    return (
        "[CONFIRMATION_HUMAINE_EN_ATTENTE] L'action n'a pas été exécutée. "
        "Demande brièvement à l'utilisateur d'appuyer sur Confirmer dans le "
        "HUD ou AnoRemote. Ne prétends jamais que l'action est terminée."
    )


def resolve(token: str, accepted: bool, *, source: str = "interface") -> bool:
    """Résout la demande si le jeton provient bien d'une interface autorisée."""
    global _pending
    with _lock:
        pending = _pending
        supplied = str(token).encode("utf-8", errors="surrogatepass")
        expected = pending.token.encode("ascii") if pending is not None else b""
        if pending is None or not secrets.compare_digest(supplied, expected):
            return False
        _pending = None

    if _hide:
        try:
            _hide(pending.token)
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans resolve")
    if time.monotonic() - pending.created_at > TIMEOUT_SECONDS:
        _write_log(f"SYS : confirmation expirée — {pending.title}")
        return False
    if not accepted:
        _write_log(f"SYS : action annulée depuis {source} — {pending.title}")
        from core.personality_modes import user_address
        _send_notify(f"Très bien {user_address()}, l'opération a été annulée.")
        return True

    from core.personality_modes import user_address
    _send_notify(f"Confirmation validée, j'exécute la commande immédiatement, {user_address()}.")

    with _lock:
        _recent[_fingerprint(pending)] = time.monotonic()

    def _run() -> None:
        try:
            detail = pending.callback() or "Aucun résultat confirmé ; vérifiez l'état de l'opération."
            _write_log(f"SYS : action confirmée depuis {source} — {pending.title} : {detail}")
            _send_notify(f"Résultat de l'action : {detail}")
        except Exception as exc:
            _write_log(f"ERR : échec après confirmation — {pending.title} : {exc}")
            _send_notify("L'action n'a pas rendu de résultat confirmé. Vérifiez son état avant de réessayer.")

    threading.Thread(target=_run, daemon=True,
                     name=f"ano-confirm-{pending.key}").start()
    return True


def current() -> PendingConfirmation | None:
    global _pending
    with _lock:
        pending = _pending
        if pending and time.monotonic() - pending.created_at > TIMEOUT_SECONDS:
            _pending = None
            return None
        return pending


def clear() -> None:
    global _pending
    with _lock:
        _pending = None
        _recent.clear()
