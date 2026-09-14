"""core/tool_bridge.py — Le pont entre ANO-GPT et les agents externes.

Un agent (Antigravity `agy`, Claude Code, Codex, Claude Desktop) parle MCP à
un petit serveur séparé, `anogpt_mcp.py`. Ce serveur ne réimplémente rien : il
transmet chaque appel à l'instance d'ANO-GPT déjà lancée, par le socket Unix de
contrôle qui existe depuis toujours pour le raccourci clavier.

Pourquoi ce socket plutôt que le tableau de bord HTTP :

* il est local, en 0600, dans `$XDG_RUNTIME_DIR` — rien à authentifier ;
* il vit aussi longtemps que le processus, pas le temps d'une session vocale ;
* et surtout `/api/command` du tableau de bord fait *parler* l'assistant, donc
  passe par Gemini. Ici c'est l'agent qui réfléchit : il veut exécuter un
  outil, pas demander à un autre modèle de décider s'il faut l'exécuter.

Le protocole d'origine est « une ligne entrante, une ligne sortante ». On le
garde intact : la requête est du JSON compact, qui échappe lui-même les retours
à la ligne, donc un résultat multiligne tient sur une seule ligne réseau.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

# Nom de la commande IPC. Les autres (`toggle`, `ask`, `status`…) continuent de
# fonctionner exactement comme avant.
IPC_COMMAND = "tool"

# Un appel d'outil peut être long : ouvrir la caméra du téléphone et attendre
# sa première image prend plusieurs secondes. La valeur reste bornée pour qu'un
# agent ne se retrouve jamais bloqué sans réponse.
DEFAULT_TIMEOUT = 45.0


class AppUnavailable(RuntimeError):
    """ANO-GPT n'est pas lancé, ou son socket ne répond pas."""


class ToolError(RuntimeError):
    """L'outil a été atteint mais a échoué. Le message vient de l'application."""


def encode_request(name: str, args: Dict[str, Any] | None = None) -> str:
    """Fabrique la ligne à envoyer sur le socket."""
    payload = json.dumps(
        {"name": name, "args": args or {}},
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"{IPC_COMMAND} {payload}"


def decode_reply(reply: str) -> str:
    """Extrait le texte du résultat, ou lève l'erreur qu'il transporte.

    Le serveur de contrôle préfixe les réponses par `OK ` ou `ERR `. On
    distingue trois cas, parce qu'un agent doit pouvoir réagir différemment :
    l'application est absente, l'outil a échoué, ou tout va bien.
    """
    reply = (reply or "").strip()
    if not reply:
        raise AppUnavailable("ANO-GPT n'a rien répondu sur le socket de contrôle.")
    if reply.startswith("ERR "):
        raise ToolError(reply[4:].strip())
    if reply.startswith("OK "):
        reply = reply[3:].strip()
    elif reply == "OK":
        return ""

    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        # Une réponse non-JSON vient d'une version plus ancienne du socket :
        # la rendre telle quelle vaut mieux que d'échouer.
        return reply
    if isinstance(data, dict) and not data.get("ok", True):
        raise ToolError(str(data.get("error") or "échec sans message"))
    if isinstance(data, dict):
        return str(data.get("result", ""))
    return str(data)


def call_app(name: str, args: Dict[str, Any] | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> str:
    """Exécute un outil dans l'ANO-GPT en cours d'exécution.

    Lève `AppUnavailable` si l'application est éteinte : l'appelant peut alors
    choisir un repli hors-ligne plutôt que de faire remonter une panne.
    """
    from core.ipc import send_command

    try:
        reply = send_command(encode_request(name, args), timeout=timeout)
    except ConnectionError as exc:
        raise AppUnavailable(str(exc)) from exc
    except Exception as exc:  # socket coupé en cours de route
        raise AppUnavailable(f"socket de contrôle injoignable : {exc}") from exc
    return decode_reply(reply)


def app_running() -> bool:
    """Vrai si une instance d'ANO-GPT répond au socket."""
    try:
        from core.ipc import send_command

        return send_command("ping", timeout=1.5).startswith("OK")
    except Exception:
        return False


# ── côté application ────────────────────────────────────────────────────────

def dispatch(handlers: Dict[str, Callable[[dict], Any]], raw: str) -> str:
    """Exécute la requête reçue sur le socket et rend la ligne de réponse.

    Vit ici plutôt que dans `main.py` pour être testable sans démarrer Qt, le
    moteur audio et la session Gemini.
    """
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return _fail(f"requête illisible : {exc}")

    if not isinstance(payload, dict):
        return _fail("la requête doit être un objet JSON")

    name = str(payload.get("name") or "").strip()
    args = payload.get("args", {})
    if not isinstance(args, dict):
        return _fail("'args' doit être un objet JSON")
    if not name:
        return _fail("nom d'outil manquant")

    handler = handlers.get(name)
    if handler is None:
        known = ", ".join(sorted(handlers)) or "aucun"
        return _fail(f"outil inconnu : {name} (disponibles : {known})")

    try:
        result = handler(args)
    except Exception as exc:
        # L'agent lit ce texte : il doit dire ce qui s'est passé, pas
        # seulement le type de l'exception.
        return _fail(f"{type(exc).__name__}: {exc}")

    return json.dumps(
        {"ok": True, "result": "" if result is None else str(result)},
        ensure_ascii=False, separators=(",", ":"),
    )


def _fail(message: str) -> str:
    return json.dumps(
        {"ok": False, "error": message},
        ensure_ascii=False, separators=(",", ":"),
    )
