"""core/brain_relay.py — Faire penser le fournisseur choisi, pas Gemini.

Gemini Live est une paire d'oreilles et une voix : il entend le micro en
continu et il parle. Rien d'autre ne fait ça aussi bien sur cette machine.
Mais entendre n'est pas décider. Quand l'utilisateur choisit Azure, DeepSeek
ou Claude comme cerveau, c'est ce modèle-là qui doit comprendre la demande,
choisir les outils et rédiger la réponse — Gemini se contentant de la lire à
voix haute, mot pour mot.

Le chemin est donc : Gemini transcrit et appelle ``consult_brain`` → ce module
tient une vraie boucle d'agent avec le fournisseur choisi (il peut appeler les
outils d'ANO-GPT autant de fois qu'il le faut) → le texte final remonte à
Gemini, qui le prononce.

Deux garde-fous comptent ici :

* **le temps.** Une voix qui attend est une voix cassée. La boucle est bornée
  en tours *et* en secondes, et un échec devient une phrase compréhensible ;
* **la boucle infinie.** ``consult_brain`` n'est jamais offert au cerveau
  externe : il ne peut pas se rappeler lui-même.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from core.llm_client import (
    _load_config, call_brain, main_brain, main_brain_label, relay_active,
)

# Au-delà, l'utilisateur a cessé d'attendre : mieux vaut une phrase honnête.
# Un modèle à raisonnement (GPT-5.x, o-series) peut dépasser largement cette
# durée : ``brain_timeout_s`` permet de l'assumer, au prix du silence.
DEFAULT_TIMEOUT = 45.0


def configured_timeout() -> float:
    """Budget de réflexion, borné pour qu'une voix n'attende jamais sans fin."""
    try:
        value = float(_load_config().get("brain_timeout_s", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(10.0, min(value, 90.0))
# Assez de tours pour enchaîner « regarde l'écran → ouvre l'app → confirme »,
# pas assez pour qu'un modèle qui boucle monopolise la voix.
MAX_TOOL_ROUNDS = 6

# Outils que le cerveau externe ne doit jamais voir : ils n'ont de sens que
# dans le contexte vocal de Gemini, ou refermeraient la boucle sur lui-même.
_EXCLUDED_TOOLS = {"consult_brain", "deep_think"}


class _RelayCall:
    """Sosie minimal d'un ``FunctionCall`` Gemini, pour le répartiteur."""

    __slots__ = ("id", "name", "args")

    def __init__(self, call_id: str, name: str, args: dict) -> None:
        self.id = call_id
        self.name = name
        self.args = args


def _json_type(gemini_type: Any) -> str:
    return str(gemini_type or "STRING").lower()


def _json_schema(schema: dict | None) -> dict:
    """Traduit un schéma Gemini (types en majuscules) en JSON Schema OpenAI."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict = {"type": _json_type(schema.get("type"))}
    if schema.get("description"):
        out["description"] = schema["description"]
    if schema.get("enum"):
        out["enum"] = list(schema["enum"])
    properties = schema.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {
            name: _json_schema(value) for name, value in properties.items()
        }
    if schema.get("items"):
        out["items"] = _json_schema(schema["items"])
    if schema.get("required"):
        out["required"] = list(schema["required"])
    if out["type"] == "object":
        out.setdefault("properties", {})
    return out


def openai_tools(declarations: list[dict]) -> list[dict]:
    """Déclarations ANO-GPT converties au format ``tools`` OpenAI."""
    tools: list[dict] = []
    for decl in declarations or []:
        name = str(decl.get("name") or "").strip()
        if not name or name in _EXCLUDED_TOOLS:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(decl.get("description") or "")[:1024],
                "parameters": _json_schema(decl.get("parameters")),
            },
        })
    return tools


def _tool_text(response: Any) -> str:
    """Extrait le texte utile d'une ``FunctionResponse``, sans jamais lever."""
    payload = getattr(response, "response", None)
    if isinstance(payload, dict):
        result = payload.get("result", payload)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)[:4000]
    return str(payload or "")[:4000]


def system_prompt(base_prompt: str) -> str:
    """Consigne du cerveau : il pense et agit, la voix ne fait que lire."""
    return (
        f"{base_prompt}\n\n"
        "── Rôle exact ──\n"
        "Tu es le cerveau d'ANO-GPT. Ta réponse sera lue telle quelle à voix "
        "haute par la voix de l'assistant : écris donc du texte parlé, en "
        "français, sans markdown, sans titre, sans liste à puces, sans "
        "émoji, sans balise. Deux ou trois phrases suffisent le plus souvent.\n"
        "Tu disposes des outils réels de la machine : sers-t'en pour agir "
        "vraiment au lieu de décrire ce que tu ferais. N'annonce jamais une "
        "action que tu n'as pas exécutée par un outil."
    )


async def run_turn(
    dispatcher: Any,
    question: str,
    *,
    context: str = "",
    declarations: list[dict] | None = None,
    base_prompt: str = "",
    history: list[dict] | None = None,
    timeout: float | None = None,
) -> str:
    """Fait traiter un tour complet par le cerveau choisi et rend son texte.

    ``dispatcher`` est l'instance JarvisLive : c'est elle qui exécute réellement
    les outils, avec ses garde-fous habituels (verrous, délais, journal).
    """
    question = str(question or "").strip()
    if not question:
        return "La demande était vide."
    if main_brain() is None:
        raise RuntimeError("Aucun cerveau externe sélectionné.")

    messages: list[dict] = [{"role": "system", "content": system_prompt(base_prompt)}]
    for turn in (history or [])[-6:]:
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    user = f"{context}\n\n{question}".strip() if context else question
    messages.append({"role": "user", "content": user})

    tools = openai_tools(declarations or [])
    deadline = time.monotonic() + (
        configured_timeout() if timeout is None else max(10.0, timeout)
    )

    started_at = time.monotonic()
    print(f"[Relais] → {main_brain_label()} : {question[:120]!r}")
    for _ in range(MAX_TOOL_ROUNDS):
        remaining = deadline - time.monotonic()
        if remaining <= 2:
            break
        result = await asyncio.to_thread(
            call_brain, messages, tools or None, int(remaining)
        )
        calls = result.get("tool_calls") or []
        content = str(result.get("content") or "").strip()
        if not calls:
            answer = content or "Je n'ai rien à ajouter."
            print(
                f"[Relais] ← {main_brain_label()} en "
                f"{time.monotonic() - started_at:.1f}s."
            )
            return answer

        # Le modèle a demandé des outils : on garde sa décision dans
        # l'historique, sinon le tour suivant ne saurait pas à quoi les
        # résultats répondent.
        messages.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {
                    "id": call.get("id") or uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": json.dumps(
                            call["function"].get("arguments") or {}, ensure_ascii=False
                        ),
                    },
                }
                for call in calls
            ],
        })

        fcs = []
        for call in calls:
            args = call["function"].get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            fcs.append(_RelayCall(
                call.get("id") or uuid.uuid4().hex,
                str(call["function"].get("name") or ""),
                args if isinstance(args, dict) else {},
            ))

        responses = await dispatcher._execute_tool_batch(fcs)
        by_name: dict[str, Any] = {}
        for response in responses or []:
            by_name.setdefault(str(getattr(response, "name", "")), response)
        for fc, response in zip(fcs, list(responses or [])):
            messages.append({
                "role": "tool",
                "tool_call_id": fc.id,
                "name": fc.name,
                "content": _tool_text(response or by_name.get(fc.name)),
            })

    # Sortie de boucle : soit le temps, soit trop d'allers-retours d'outils.
    # On redemande une conclusion courte plutôt que de rendre le silence.
    messages.append({
        "role": "user",
        "content": "Conclus maintenant en une ou deux phrases parlées, sans appeler d'outil.",
    })
    try:
        final = await asyncio.to_thread(call_brain, messages, None, 25)
        text = str(final.get("content") or "").strip()
        if text:
            return text
    except Exception as exc:
        print(f"[Relais] conclusion impossible : {exc}")
    return (
        f"{main_brain_label()} a mis trop de temps à conclure. "
        "Reformule ta demande ou réessaie."
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "configured_timeout",
    "MAX_TOOL_ROUNDS",
    "openai_tools",
    "relay_active",
    "run_turn",
    "system_prompt",
]
