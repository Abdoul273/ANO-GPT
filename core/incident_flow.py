"""core/incident_flow.py — Une erreur vécue : la montrer, proposer, respecter la réponse.

Parcours voulu par l'utilisateur :

1. l'erreur est dite à voix haute ET affichée dans une carte dédiée
   (outil, fichier, ligne, type, extrait de code) ;
2. une carte de confirmation demande « Je corrige ? » (clic, texte ou un
   simple « oui » / « non » à la voix) ;
3. oui → l'agent de code répare (``core.auto_fix``) ;
   non → rien n'est touché : un rapport donne les lignes en cause et
   pourquoi ça a calé, avec un prompt prêt à confier à un agent IA (copié
   dans le presse-papiers et enregistré dans un fichier).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core import action_kit as kit
from core import incident_log

CARD_ERROR = "error"
_REPORT_DIR = incident_log._path().parent / "incident_reports"

_ui: Any = None
_speak: Optional[Callable[[str], Any]] = None


def bind(ui: Any, speak: Optional[Callable[[str], Any]]) -> None:
    global _ui, _speak
    _ui, _speak = ui, speak


def bound() -> bool:
    return _ui is not None


# ── Lecture du code en cause ─────────────────────────────────────────────
def _project_frames(inc: incident_log.Incident, limit: int = 4) -> list[tuple[str, int, str]]:
    """Cadres de la pile appartenant au projet, du plus profond au plus haut :
    (fichier relatif, ligne, fonction)."""
    root = str(incident_log._repo_root())
    frames = []
    for m in re.finditer(r'File "([^"]+)", line (\d+), in (\S+)', inc.traceback or ""):
        path = m.group(1)
        if path.startswith(root) and "/site-packages/" not in path and "/.venv/" not in path:
            frames.append((str(Path(path).relative_to(root)), int(m.group(2)), m.group(3)))
    frames.reverse()
    out, seen = [], set()
    for f in frames:
        if (f[0], f[1]) not in seen:
            seen.add((f[0], f[1]))
            out.append(f)
    return out[:limit]


def code_excerpt(file: str, line: int, radius: int = 3) -> str:
    """Lignes autour de l'erreur, numérotées, la ligne fautive marquée ▶."""
    if not file or line <= 0:
        return ""
    path = incident_log._repo_root() / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start, end = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(end))
    return "\n".join(
        f"{'▶' if n == line else ' '} {n:>{width}} │ {lines[n - 1]}" for n in range(start, end + 1)
    )


# ── Pourquoi ça a calé ───────────────────────────────────────────────────
def why(inc: incident_log.Incident) -> str:
    """Explication en une ou deux phrases, sans modèle : rapide et fiable."""
    kind = inc.kind or ""
    msg = inc.message or ""
    args = (inc.extra or {}).get("arg_keys")
    if kind == "ActionValidationError" or "Arguments invalides" in msg:
        sent = f" Le modèle a envoyé : {', '.join(args)}." if args else ""
        return ("Le modèle a appelé l'outil avec des paramètres que son schéma refuse : "
                "la déclaration de l'outil (core/tool_dispatcher.py) ne correspond pas à ce que "
                f"le modèle envoie, ou un champ requis manque.{sent}")
    if kind in {"TimeoutError", "ActionQueueTimeout"}:
        return ("L'action n'a pas répondu dans le délai qui lui est accordé "
                "(core/action_runtime.py) : un appel réseau ou un programme externe a bloqué.")
    if kind == "ActionCircuitOpen":
        return "L'outil a échoué plusieurs fois de suite : il est mis en pause pour protéger la session."
    if kind in {"KeyError", "IndexError"}:
        return f"Le code lit une donnée absente ({msg}) : une réponse externe n'a pas la forme attendue."
    if kind in {"AttributeError", "TypeError"} and "NoneType" in msg:
        return "Une valeur attendue est vide (None) : le code l'utilise sans vérifier qu'elle existe."
    if kind in {"ModuleNotFoundError", "ImportError"}:
        return f"Une dépendance Python manque ou a changé de nom : {msg}"
    if kind in {"FileNotFoundError", "PermissionError"}:
        return f"Accès fichier impossible : {msg}"
    if kind in {"ConnectionError", "HTTPError"} or "HTTP" in msg:
        return f"Le service distant a refusé ou n'a pas répondu : {msg}"
    return f"{kind} : {msg}" if msg else kind


# ── Cartes ───────────────────────────────────────────────────────────────
def _show(card_type: str, title: str, body: str) -> None:
    if _ui is None:
        return
    try:
        _ui.show_card(card_type, title, body)
    except Exception:
        pass


def error_card_body(inc: incident_log.Incident) -> str:
    where = f"`{inc.file}` ligne **{inc.line}**" if inc.file else "emplacement inconnu"
    body = [
        f"**{inc.kind}** dans **{inc.source}** — {where}",
        "",
        inc.message,
        "",
        f"**Pourquoi :** {why(inc)}",
    ]
    excerpt = code_excerpt(inc.file, inc.line)
    if excerpt:
        body += ["", "```", excerpt, "```"]
    return "\n".join(body)


def agent_prompt(inc: incident_log.Incident) -> str:
    """Prompt autonome à coller dans Claude Code, Codex ou un autre agent."""
    from core.auto_fix import build_prompt
    return build_prompt(inc)


def report(inc: incident_log.Incident) -> tuple[str, Optional[Path]]:
    """Rapport complet (Markdown) + fichier enregistré pour le confier à un agent."""
    frames = _project_frames(inc) or ([(inc.file, inc.line, "?")] if inc.file else [])
    parts = [
        f"# Erreur {inc.kind} — {inc.source}",
        "",
        incident_log.describe(inc),
        "",
        "## Pourquoi ça a calé",
        why(inc),
        "",
        "## Lignes en cause",
    ]
    for file, line, func in frames:
        parts.append(f"- `{file}:{line}` — `{func}()`")
        excerpt = code_excerpt(file, line, radius=2)
        if excerpt:
            parts += ["", "```python", excerpt, "```", ""]
    if not frames:
        parts.append("(aucun fichier du projet dans la pile d'appel)")
    if inc.traceback.strip():
        parts += ["", "## Pile d'appel", "```", inc.traceback.strip()[-2500:], "```"]
    parts += ["", "## Prompt pour un agent IA", "", agent_prompt(inc)]
    text = "\n".join(parts)

    path: Optional[Path] = None
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(inc.ts))
        slug = re.sub(r"[^a-z0-9]+", "-", inc.source.casefold()).strip("-")[:30] or "erreur"
        path = _REPORT_DIR / f"{stamp}-{slug}.md"
        path.write_text(text, encoding="utf-8")
    except OSError:
        path = None
    return text, path


def _copy(text: str) -> bool:
    if not kit.have("wl-copy"):
        return False
    return kit.run(["wl-copy"], stdin=text, timeout=3, capture=False).returncode == 0


# ── Parcours ─────────────────────────────────────────────────────────────
def propose(incidents: list[incident_log.Incident]) -> Optional[str]:
    """Carte d'erreur + carte de confirmation. Rend la phrase à dire, ou None
    si la confirmation n'a pas pu s'afficher (l'annonce classique s'applique)."""
    if _ui is None or not incidents:
        return None
    from core import human_confirmation

    inc = incidents[-1]
    title = f"🐞 Erreur — {inc.source}"
    if len(incidents) > 1:
        title += f" (+{len(incidents) - 1} autre{'s' if len(incidents) > 2 else ''})"
    _show(CARD_ERROR, title, error_card_body(inc))

    where = f"{Path(inc.file).name} ligne {inc.line}" if inc.file else inc.source
    answer = human_confirmation.request(
        "auto-fix",
        f"Corriger l'erreur de {inc.source} ?",
        f"{inc.kind} dans {where}. Oui : un agent de code corrige et commite. "
        "Non : je ne touche à rien et je te donne les lignes et la cause.",
        lambda: _accept(inc),
        on_decline=lambda: _decline(inc),
        voice_ok=True,
    )
    if not answer.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE] L'action"):
        # Une autre décision occupe déjà l'écran : on se rabat sur « corrige ».
        return None
    return (f"Une erreur vient de se produire dans {inc.spoken()}. "
            f"{why(inc)} Je la corrige ? Dis oui ou non.")


def _accept(inc: incident_log.Incident) -> str:
    from core import auto_fix
    return auto_fix.repair(inc, player=_ui, speak=_speak)


def _decline(inc: incident_log.Incident) -> str:
    text, path = report(inc)
    copied = _copy(agent_prompt(inc))
    frames = _project_frames(inc)
    lines = ", ".join(f"{Path(f).name} ligne {n}" for f, n, _ in frames[:3]) or \
        (f"{Path(inc.file).name} ligne {inc.line}" if inc.file else "aucune ligne du projet")
    footer = []
    if path:
        footer.append(f"Rapport : `{path}`")
    if copied:
        footer.append("Prompt pour un agent IA copié dans le presse-papiers.")
    body = text.split("## Prompt pour un agent IA")[0].rstrip()
    if footer:
        body += "\n\n---\n" + "\n\n".join(footer)
    _show(CARD_ERROR, f"📋 Rapport — {inc.source}", body)
    spoken = f"D'accord, je ne touche à rien. En cause : {lines}. {why(inc)}"
    if copied:
        spoken += " Le prompt pour un agent est copié dans ton presse-papiers."
    return spoken
