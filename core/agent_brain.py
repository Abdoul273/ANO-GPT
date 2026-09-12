"""core/agent_brain.py — Déléguer la réflexion à un agent en ligne de commande.

Quand l'utilisateur demande quelque chose qui exige de *penser* plutôt que
d'agir — analyser, comparer, planifier, chercher, expliquer — la voix de
Gemini Live n'est pas le bon outil. ANO-GPT confie alors la question à un agent
complet lancé en mode « une passe » : Antigravity (`agy`) par défaut.

L'intérêt tient en une phrase : `agy` s'authentifie avec l'abonnement de
l'utilisateur, pas avec une clé d'API facturée au jeton. La réflexion lourde
ne coûte donc rien de plus, et l'agent peut lui-même rappeler les outils
d'ANO-GPT par MCP — la carte s'ouvre pendant qu'il répond.

Deux protections comptent ici :

* **la boucle.** `agy` a ANO-GPT déclaré en MCP. Si l'agent appelait
  `ask_assistant`, sa réponse repartirait vers Gemini Live, qui pourrait
  redemander une réflexion… sans fin. La variable `ANOGPT_MCP_NO_ASSISTANT`
  transmise au processus fils coupe ce chemin de retour ;
* **le temps.** Une voix qui attend est une voix cassée. L'appel est borné, et
  l'échec est une phrase compréhensible, jamais un silence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from core.context_probe import ambient_context

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config" / "api_keys.json"

# Emplacements habituels, testés dans l'ordre quand le PATH ne suffit pas.
_FALLBACK_BINARIES = (
    Path.home() / ".local/bin/agy",
    Path.home() / ".local/bin/antigravity",
)

# Au-delà, l'utilisateur a depuis longtemps cessé d'attendre une réponse.
DEFAULT_TIMEOUT = 90

# Variable lue par anogpt_mcp.py : elle désarme `ask_assistant` dans la
# session fille, seul chemin par lequel une boucle pourrait se refermer.
LOOP_GUARD_ENV = "ANOGPT_MCP_NO_ASSISTANT"


class AgentUnavailable(RuntimeError):
    """Aucun agent en ligne de commande utilisable sur cette machine."""


def _lower_priority() -> None:
    """Rétrograde le processus fils, juste après le fork.

    `nice` n'échoue jamais vers le bas et ne demande aucun privilège. En cas de
    refus inattendu on continue quand même : mieux vaut un agent prioritaire
    qu'une réflexion qui n'a pas lieu.
    """
    try:
        os.nice(10)
    except Exception:
        pass


def _config() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def agent_binary() -> str | None:
    """Chemin de l'agent à utiliser, ou None s'il n'y en a pas."""
    configured = str(_config().get("agent_command") or "").strip()
    if configured:
        found = shutil.which(configured) or (
            configured if Path(configured).is_file() else None
        )
        if found:
            return found

    found = shutil.which("agy")
    if found:
        return found
    for candidate in _FALLBACK_BINARIES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def available() -> bool:
    return agent_binary() is not None


def think(question: str, context: str = "", *,
          timeout: int = DEFAULT_TIMEOUT,
          model: str | None = None,
          effort: str | None = None) -> str:
    """Pose la question à l'agent et rend sa réponse, prête à être prononcée.

    `context` porte ce qu'ANO-GPT sait déjà d'utile (sortie d'une commande,
    contenu d'un fichier, détail de la conversation) : sans lui, l'agent
    repart de zéro et redemande ce que l'utilisateur vient de dire.
    """
    question = (question or "").strip()
    if not question:
        return "La question était vide."

    binary = agent_binary()
    if binary is None:
        raise AgentUnavailable(
            "Aucun agent en ligne de commande trouvé (agy). "
            "Installez-le, ou renseignez 'agent_command' dans config/api_keys.json."
        )

    settings = _config()
    model = model or str(settings.get("agent_model") or "").strip() or None
    effort = effort or str(settings.get("agent_effort") or "").strip() or None

    prompt = _build_prompt(question, context)
    command = [binary, "-p", prompt,
               "--output-format", "text",
               "--print-timeout", f"{timeout}s"]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]

    environment = dict(os.environ)
    environment[LOOP_GUARD_ENV] = "1"

    try:
        completed = subprocess.run(
            command,
            capture_output=True, text=True,
            # Priorité basse : l'agent est un binaire lourd, et cette machine
            # n'a que deux cœurs. Le laisser concurrencer la boucle audio à
            # égalité hache la voix et fait entendre à l'assistant sa propre
            # parole hachée comme si c'était l'utilisateur qui l'interrompait.
            preexec_fn=_lower_priority if os.name == "posix" else None,
            # Marge au-dessus de la borne interne : on veut que l'agent rende
            # son propre message de dépassement plutôt que d'être tué en vol.
            timeout=timeout + 20,
            # Depuis le dossier personnel : lancé dans le dépôt, l'agent se met
            # à l'indexer et la première réponse arrive bien plus tard.
            cwd=str(Path.home()),
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return (f"L'agent n'a pas répondu en {timeout} secondes. "
                "Reformule plus court, ou réponds toi-même.")
    except Exception as exc:
        raise AgentUnavailable(f"Impossible de lancer l'agent : {exc}") from exc

    answer = (completed.stdout or "").strip()
    if answer:
        return answer

    error = (completed.stderr or "").strip()
    if error:
        # Ne pas rendre une trace entière à une voix : la première ligne suffit
        # à comprendre, le reste est déjà dans le terminal.
        return f"L'agent a échoué : {error.splitlines()[0][:200]}"
    return "L'agent n'a rien renvoyé."


def _build_prompt(question: str, context: str) -> str:
    """Cadre la demande pour une réponse destinée à être *dite*, pas lue.

    Sans ces consignes, l'agent rend des listes à puces, des tableaux et des
    blocs de code — illisibles à voix haute, et interminables.
    """
    # Instantané pris ici — juste avant la construction de la commande — pour
    # que chaque tour voie la fenêtre qui est réellement devant l'utilisateur.
    parts = [
        ambient_context(),
        "Tu réponds à un assistant vocal qui va lire ta réponse à voix haute.",
        "Réponds en français, en prose continue, sans titres, sans listes à "
        "puces, sans tableaux et sans blocs de code.",
        "Sois complet mais bref : vise dix lignes au maximum.",
        "Va droit au fait, sans préambule ni formule de politesse.",
    ]
    if context.strip():
        parts.append(f"\nCe que l'assistant sait déjà :\n{context.strip()}")
    parts.append(f"\nQuestion :\n{question}")
    return "\n".join(parts)


def generate_memory_aliases(memory_text: str, category: str = "", *, timeout: int = 15) -> str:
    """Génère 5 à 10 mots-clés, synonymes et entités élargis pour l'indexation sémantique FTS5."""
    import re
    memory_text = (memory_text or "").strip()
    if not memory_text:
        return ""
    binary = agent_binary()
    if binary is None:
        return ""

    prompt = (
        "Génère une liste de 5 à 10 mots-clés, synonymes, concepts associés, thèmes généraux "
        "et entités en français pour enrichir la recherche sémantique du souvenir suivant.\n"
        "Exemple pour 'La Peugeot du garage de Matam a été réparée' -> 'voiture automobile vehicule mecanicien panne reparation garage auto transport Matam Conakry'\n"
        "Ne donne QUE les mots séparés par des espaces, sans ponctuation, sans phrase, sans explication.\n\n"
        f"Souvenir : {memory_text}"
    )
    command = [binary, "-p", prompt, "--output-format", "text", "--print-timeout", f"{timeout}s"]
    environment = dict(os.environ)
    environment[LOOP_GUARD_ENV] = "1"
    try:
        completed = subprocess.run(
            command,
            capture_output=True, text=True,
            preexec_fn=_lower_priority if os.name == "posix" else None,
            timeout=timeout + 5,
            cwd=str(Path.home()),
            env=environment,
        )
        if completed.returncode == 0 and completed.stdout:
            words = re.findall(r"[\w-]+", completed.stdout.lower(), flags=re.UNICODE)
            return " ".join(words[:20])
    except Exception:
        pass
    return ""


def generate_document_summary(filename: str, content_snippet: str, *, timeout: int = 15) -> str:
    """Génère un résumé dense de 2-3 lignes et mots-clés conceptuels pour un document."""
    content_snippet = (content_snippet or "").strip()
    if not content_snippet:
        return ""
    binary = agent_binary()
    if binary is None:
        return ""

    prompt = (
        "Résume le sujet clé, l'architecture et les thèmes principaux de ce document en 2-3 phrases denses "
        "contenant les mots-clés conceptuels et synonymes pertinents pour retrouver ce document par le sens.\n\n"
        f"Fichier : {filename}\n"
        f"Extrait du contenu :\n{content_snippet[:2000]}"
    )
    command = [binary, "-p", prompt, "--output-format", "text", "--print-timeout", f"{timeout}s"]
    environment = dict(os.environ)
    environment[LOOP_GUARD_ENV] = "1"
    try:
        completed = subprocess.run(
            command,
            capture_output=True, text=True,
            preexec_fn=_lower_priority if os.name == "posix" else None,
            timeout=timeout + 5,
            cwd=str(Path.home()),
            env=environment,
        )
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout.strip()
    except Exception:
        pass
    return ""
