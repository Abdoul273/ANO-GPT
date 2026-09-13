"""Contrôle fiable et borné de ZapZap (WhatsApp Web pour Linux).

ZapZap ne fournit pas une API WhatsApp : c'est un conteneur QtWebEngine pour
WhatsApp Web.  Son interface publique utile est l'ouverture d'un lien de
conversation ``https://web.whatsapp.com/send``.  Ce module s'appuie
exclusivement sur cette interface, sans injecter de JavaScript dans WhatsApp
ni lire la session/cookies de l'utilisateur.

Le résultat est volontairement précis : WhatsApp ne confirme pas la livraison
à ZapZap, donc une demande d'envoi ne sera jamais annoncée comme une livraison
confirmée.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from core import action_kit as kit


MAX_MESSAGE_LENGTH = 65_536
_MIN_E164_DIGITS = 7
_MAX_E164_DIGITS = 15


class ZapZapError(ValueError):
    """Erreur sûre à communiquer à l'utilisateur."""


@dataclass(frozen=True)
class ZapZapStatus:
    installed: bool
    running: bool
    input_ready: bool


def _is_wayland() -> bool:
    import os
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get(
        "XDG_SESSION_TYPE", ""
    ).lower() == "wayland"


def _zapzap_window() -> dict | None:
    for client in kit.hypr_clients():
        identity = " ".join(
            str(client.get(key, "")) for key in ("class", "initialClass", "title")
        ).casefold()
        if "zapzap" in identity:
            return client
    return None


def _input_ready() -> bool:
    return (_is_wayland() and kit.have("wtype")) or kit.have("xdotool")


def status() -> ZapZapStatus:
    return ZapZapStatus(
        installed=shutil.which("zapzap") is not None,
        running=_zapzap_window() is not None,
        input_ready=_input_ready(),
    )


def normalize_phone(value: str) -> str:
    """Normalise un numéro E.164 sans deviner de pays."""
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ZapZapError("Le numéro WhatsApp est vide.")
    if not (_MIN_E164_DIGITS <= len(digits) <= _MAX_E164_DIGITS):
        raise ZapZapError("Le numéro WhatsApp doit contenir entre 7 et 15 chiffres avec l'indicatif pays.")
    # Un numéro national sans + est ambigu. Les contacts existants peuvent
    # contenir un numéro local ; ne l'envoyons jamais à un mauvais destinataire.
    if not text.startswith(("+", "00")):
        raise ZapZapError("Utilisez un numéro international avec son indicatif pays (ex. +224…).")
    if text.startswith("00"):
        # 00 est une notation internationale ; WhatsApp attend uniquement les chiffres.
        digits = digits[2:]
    return digits


def chat_url(phone: str, message: str = "") -> str:
    normalized = normalize_phone(phone)
    message = str(message or "")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ZapZapError(f"Le message dépasse la limite de {MAX_MESSAGE_LENGTH} caractères.")
    query = {"phone": normalized}
    if message:
        query["text"] = message
    return "https://web.whatsapp.com/send?" + urlencode(query)


def _focus(window: dict) -> bool:
    address = str(window.get("address") or "")
    if not address:
        return False
    kit.hypr("dispatch", "focuswindow", f"address:{address}")
    return kit.wait_until(
        lambda: kit.hypr_activewindow().get("address") == address,
        timeout=1.5,
        interval=0.08,
    )


def open_chat(phone: str, message: str = "", *, wait_s: float = 8.0) -> str:
    """Ouvre ZapZap sur une conversation, avec texte éventuellement prérempli."""
    if not status().installed:
        return "ZapZap n'est pas installé ou introuvable dans PATH."
    url = chat_url(phone, message)
    # ZapZap relaie l'URL à son instance unique par QLocalSocket.  Aucun shell
    # n'est utilisé : le contenu du message ne peut pas devenir une commande.
    if kit.spawn(["zapzap", url]) is None:
        return "Impossible de démarrer ZapZap."
    if not kit.wait_until(lambda: _zapzap_window() is not None,
                          timeout=wait_s, interval=0.15):
        return "ZapZap a été lancé, mais sa fenêtre n'est pas devenue disponible à temps."
    window = _zapzap_window()
    if window is None:  # fenêtre refermée entre le dernier sondage et la lecture
        return "ZapZap a été fermé avant l'ouverture de la conversation."
    _focus(window)
    if message:
        return "Conversation ouverte dans ZapZap avec le message prérempli."
    return "Conversation ouverte dans ZapZap."


def _press_enter() -> bool:
    if _is_wayland() and kit.have("wtype"):
        return bool(kit.run(["wtype", "-k", "Return"], timeout=2))
    if kit.have("xdotool"):
        return bool(kit.run(["xdotool", "key", "Return"], timeout=2))
    return False


def request_send(phone: str, message: str) -> str:
    """Demande l'envoi dans ZapZap après que l'appelant a obtenu confirmation.

    L'état de livraison n'est intentionnellement pas inféré : ZapZap ne le
    rend pas disponible hors de son interface Web.
    """
    if not message:
        raise ZapZapError("Le message WhatsApp est vide.")
    result = open_chat(phone, message)
    if not result.startswith("Conversation ouverte"):
        return result
    # Le deep-link de WhatsApp place le texte dans le composeur. La courte
    # attente laisse WhatsApp Web finir de sélectionner la conversation ; elle
    # ne dépend pas d'une position de pixel ou d'un titre traduit.
    time.sleep(0.7)
    window = _zapzap_window()
    if not window or not _focus(window) or not _press_enter():
        return ("Message prérempli dans ZapZap, mais l'envoi automatique n'a pas pu "
                "être déclenché. Vérifiez le texte puis appuyez sur Entrée.")
    return ("Demande d'envoi transmise à ZapZap. Vérifiez l'indicateur d'envoi "
            "dans WhatsApp : ZapZap ne fournit pas de confirmation de livraison à ANO-GPT.")


def resolve_phone(receiver: str) -> str:
    """Résout un contact ANO-GPT ou valide directement un numéro international."""
    try:
        from core.contacts import ContactError, get_contacts_book
        resolved = get_contacts_book().resolve(receiver, "whatsapp")
    except ContactError as exc:
        raise ZapZapError(str(exc)) from exc
    return normalize_phone(resolved.value if resolved else receiver)


def control(action: str, receiver: str = "", message: str = "") -> str:
    """API synchrone de l'outil ``whatsapp_control``."""
    action = str(action or "status").strip().casefold()
    if action == "status":
        current = status()
        return (
            f"ZapZap : {'installé' if current.installed else 'absent'} ; "
            f"{'ouvert' if current.running else 'fermé'} ; "
            f"saisie clavier {'disponible' if current.input_ready else 'indisponible'}."
        )
    if action not in {"open", "compose", "send"}:
        return "Action WhatsApp inconnue. Utilisez status, open, compose ou send."
    if not receiver:
        return "Précisez le contact ou le numéro WhatsApp international."
    try:
        phone = resolve_phone(receiver)
        if action == "open":
            return open_chat(phone)
        if action == "compose":
            return open_chat(phone, message)
        # Cet outil ne possède pas le jeton de confirmation de l'interface.
        # L'envoi effectif suit exclusivement send_message, qui affiche la
        # carte de prévisualisation et attend le clic humain.
        return ("Pour envoyer, utilisez send_message : ANO-GPT affichera d'abord "
                "l'aperçu et attendra votre confirmation. Aucun message n'a été envoyé.")
    except ZapZapError as exc:
        return str(exc)
