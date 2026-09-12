"""Simulation stratégique bornée : mémoire, données récentes, débat et trace.

Le module est volontairement séparé du moteur Live : une simulation peut
prendre quelques minutes, mais ne doit jamais monopoliser la conversation ni
appeler ANO-GPT en cascade depuis un agent délégué.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from actions.web_search import web_search
from core import knowledge_graph, memory_store
from core.llm_client import BRAIN_UNCONFIGURED, think_deep

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config" / "api_keys.json"
_MAX_OPTIONS = 4
_MAX_CONTEXT = 6_000
_MAX_EVIDENCE = 4_000


class DecisionSimulationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionSimulation:
    decision: str
    options: tuple[str, ...]
    context: str
    evidence: dict[str, str]
    arguments: dict[str, str]
    synthesis: str
    saved_at: str


def _config() -> dict:
    try:
        raw = json.loads(_CONFIG.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def simulation_timeout_seconds() -> int:
    """Borne globale configurable, jamais illimitée."""
    value = _config().get("decision_simulation_timeout_seconds", 180)
    try:
        return max(30, min(600, int(value)))
    except (TypeError, ValueError):
        return 180


def extract_options(description: str, options: list[str] | None = None) -> list[str]:
    supplied = [" ".join(str(item).split()) for item in (options or []) if str(item).strip()]
    if len(supplied) >= 2:
        return supplied[:_MAX_OPTIONS]
    text = " ".join(str(description or "").split())
    # « A vs B », « A versus B » et « A ou B » sont les formes vocales les
    # plus fréquentes. La description entière reste disponible comme contexte.
    parts = re.split(r"\s+(?:vs\.?|versus|ou)\s+", text, maxsplit=1, flags=re.I)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return [parts[0].strip(" :-"), parts[1].strip(" :-")]
    return [text] if text else []


def _memory_context(decision: str) -> str:
    rows: list[str] = []
    try:
        for item in knowledge_graph.search(decision, limit=8):
            rows.append(f"- [{item.kind}] {item.title}: {item.content[:500]}")
    except Exception:
        pass
    try:
        for item in memory_store.search(decision, limit=6):
            rows.append(f"- [mémoire] {item.get('key') or item.get('kind')}: {item.get('value', '')}")
    except Exception:
        pass
    result = "\n".join(rows)
    return result[:_MAX_CONTEXT] or "Aucun contexte personnel pertinent n'a été retrouvé."


def _research(option: str, decision: str) -> str:
    query = (
        f"{option} — informations actuelles pour décider : coûts, délais, "
        f"contraintes administratives, risques et avis. Contexte : {decision}"
    )
    result = web_search({"query": query, "mode": "research"})
    return str(result or "Aucune donnée web exploitable.")[:_MAX_EVIDENCE]


def _reason(prompt: str, context: str, timeout: int) -> str:
    """Réutilise deep_think ; l'agent CLI porte déjà LOOP_GUARD_ENV."""
    from core import agent_brain

    if agent_brain.available():
        try:
            return agent_brain.think(prompt, context=context, timeout=timeout)
        except Exception:
            pass
    answer = think_deep(prompt, context=context, timeout=timeout)
    if answer == BRAIN_UNCONFIGURED:
        raise DecisionSimulationError(
            "Aucun moteur de réflexion n'est configuré pour la simulation. "
            "Configurez un fournisseur Brain ou l'agent agy."
        )
    return answer


def run_simulation(
    decision: str,
    options: list[str] | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> DecisionSimulation:
    """Exécute au plus quatre scénarios et un arbitrage, dans une durée bornée."""
    decision = " ".join(str(decision or "").split())
    choices = extract_options(decision, options)
    if len(choices) < 2:
        raise DecisionSimulationError("Donnez au moins deux options, par exemple « A vs B ».")
    timeout = simulation_timeout_seconds()
    per_agent = max(20, timeout // (len(choices) + 2))
    if progress:
        progress("Consultation du Second Brain et de la mémoire longue durée…")
    context = _memory_context(decision)

    if progress:
        progress("Recherche web ciblée pour chaque option…")
    evidence: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(choices)) as pool:
        jobs = {pool.submit(_research, option, decision): option for option in choices}
        for job, option in ((job, jobs[job]) for job in jobs):
            try:
                evidence[option] = job.result(timeout=per_agent)
            except Exception as exc:
                evidence[option] = f"Recherche indisponible : {exc}"

    arguments: dict[str, str] = {}
    for option in choices:
        if progress:
            progress(f"Analyse contradictoire : défense de « {option} »…")
        arguments[option] = _reason(
            "Tu es l'avocat d'une option dans une décision stratégique. Défends "
            f"uniquement l'option « {option} », sans inventer de faits. Distingue "
            "faits, hypothèses et inconnues. Donne bénéfices, coûts/délais, risques "
            "et conditions de réussite en français structuré.",
            f"DÉCISION\n{decision}\n\nCONTEXTE PERSONNEL\n{context}\n\n"
            f"DONNÉES WEB RÉCENTES POUR {option}\n{evidence[option]}",
            per_agent,
        )[:_MAX_CONTEXT]

    if progress:
        progress("Arbitrage final des scénarios…")
    dossier = "\n\n".join(
        f"OPTION : {option}\nDONNÉES WEB : {evidence[option]}\n"
        f"ARGUMENTAIRE : {arguments[option]}"
        for option in choices
    )
    synthesis = _reason(
        "Tu es l'arbitre indépendant d'une simulation stratégique. Compare les "
        "options à partir du dossier, sans masquer les incertitudes. Réponds en "
        "Markdown sous ces rubriques exactes : ## Recommandation, ## Trade-offs, "
        "## Risques par option, ## Hypothèses à vérifier, ## Prochain pas. La "
        "recommandation doit être conditionnelle si les preuves sont insuffisantes.",
        f"DÉCISION\n{decision}\n\nCONTEXTE PERSONNEL\n{context}\n\nDOSSIER\n{dossier[:16_000]}",
        per_agent,
    )[:10_000]
    saved_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    simulation = DecisionSimulation(
        decision, tuple(choices), context, evidence, arguments, synthesis, saved_at
    )
    _save(simulation)
    return simulation


def _save(simulation: DecisionSimulation) -> None:
    """Archive le dossier entier dans le Second Brain, daté et retrouvable."""
    content = (
        f"Décision : {simulation.decision}\nOptions : {', '.join(simulation.options)}\n"
        f"Horodatage : {simulation.saved_at}\n\n{simulation.synthesis}\n\n"
        + "\n\n".join(
            f"### {option}\nWeb : {simulation.evidence[option]}\n"
            f"Débat : {simulation.arguments[option]}"
            for option in simulation.options
        )
    )
    knowledge_graph.ingest_note(
        title=f"Simulation décisionnelle — {simulation.decision[:90]}",
        content=content[:12_000],
        tags="simulation décision stratégique what-if",
    )
