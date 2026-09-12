"""actions/second_brain.py — Contrôleur d'Action du Second Brain & Graphe de Connaissances.

Gère l'indexation sémantique FTS5, la recherche associative par analogie d'idées,
la traversée de graphe relationnel et la liaison multi-sources (fichiers, projets,
personnes, commandes, notes, conversations).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import knowledge_graph

logger = logging.getLogger("anogpt.actions.second_brain")


# ════════════════════════════════════════════════════════════════════════════
# 1. PARSING VOCAL LOCAL (ZÉRO LATENCE)
# ════════════════════════════════════════════════════════════════════════════

def parse_second_brain_intent(text: str) -> Optional[Dict[str, Any]]:
    """Détecte les intentions d'interrogation ou d'écriture dans le Second Brain."""
    if not text:
        return None
    text_clean = text.lower().strip()
    text_clean = re.sub(
        r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais|fais|lance)\b",
        "", text_clean
    ).strip()

    # 1. État / Statistiques du Second Brain
    if any(k in text_clean for k in ["second brain", "graphe de connaissances", "memoire associative"]):
        if any(k in text_clean for k in ["etat", "état", "status", "statistique", "stats", "combien"]):
            return {"action": "status"}
        if any(k in text_clean for k in ["resynchronise", "reindex", "reindexe", "réindexe", "sync"]):
            return {"action": "reindex"}
        if any(k in text_clean for k in ["graphe", "visualise", "affiche le graphe", "dessine"]):
            return {"action": "visualize", "query": text_clean}

    # 2. Mémorisation explicite de commande
    m_cmd = re.search(r"(?:m[ée]morise|enregistre|garde|retiens)\s+(?:la\s+)?commande\s+(?:suivante\s+)?[:;]?\s*(.+)", text, re.IGNORECASE)
    if m_cmd:
        return {"action": "save_command", "command": m_cmd.group(1).strip()}

    # 3. Indexation d'un projet local
    m_proj = re.search(r"(?:indexe|scanne|analyse)\s+(?:le\s+)?(?:projet|dossier|repo)\s+([a-zA-Z0-9_\-\.\/]+)", text, re.IGNORECASE)
    if m_proj:
        return {"action": "index_project", "project": m_proj.group(1).strip()}

    # 4. Requêtes de recherche associative (retrouve, rappelle, qu'avait-on, etc.)
    triggers = [
        "retrouve", "rappelle-moi", "rappelle moi", "qu'avait-on", "qu avait on",
        "qu'est-ce qu'on avait", "qu est ce qu on avait", "quelle etait", "quelle était",
        "qui etait", "qui était", "cherche dans le second brain", "cherche dans mes notes",
        "la commande pour", "le fichier dont on parlait", "le projet dont", "le mois dernier",
        "la semaine derniere", "la semaine dernière",
    ]
    if any(t in text_clean for t in triggers):
        return {"action": "search", "query": text}

    return None


# ════════════════════════════════════════════════════════════════════════════
# 2. CONTRÔLEUR D'ACTION UNIFIÉ DU SECOND BRAIN
# ════════════════════════════════════════════════════════════════════════════

def second_brain_action(
    parameters: Optional[Dict[str, Any]] = None,
    player=None,
    speak=None,
    **_kwargs
) -> str:
    """
    Action principale du Second Brain ANO-GPT.
    
    Paramètres :
      action    : 'search' | 'status' | 'reindex' | 'save_command' | 'index_project' | 'note' | 'connect' | 'visualize'
      query     : Texte ou question en langage naturel
      limit     : Nombre maximum de résultats (défaut: 8)
      command   : Commande à enregistrer
      project   : Nom ou chemin du projet
      title     : Titre de la note
      content   : Contenu de la note
    """
    params = parameters or {}
    description = (params.get("description") or params.get("query") or params.get("text") or "").strip()
    action = (params.get("action") or "").strip().lower()

    if description and not action:
        intent = parse_second_brain_intent(description)
        if intent:
            action = intent.get("action", "search")
            if "command" in intent:
                params["command"] = intent["command"]
            if "project" in intent:
                params["project"] = intent["project"]
            if "query" in intent:
                params["query"] = intent["query"]
        else:
            action = "search"

    if not action:
        action = "search"

    # ── Action : État / Statistiques ─────────────────────────────────────────
    if action in ("status", "etat", "état", "stats"):
        counts = knowledge_graph.status()
        if not counts:
            return "Le Second Brain est prêt, mais aucune entité n'est encore indexée."
        
        icons = {
            "project": "📁 Projets", "person": "👤 Contacts", "file": "📄 Fichiers",
            "command": "⚡ Commandes", "conversation": "💬 Conversations", "note": "📝 Notes",
            "date": "📅 Dates", "email": "✉️ E-mails", "relations": "🔗 Relations",
        }
        details = []
        for key, count in sorted(counts.items()):
            label = icons.get(key, f"🔹 {key}")
            details.append(f"{label} : {count}")
        
        return "🧠 Second Brain opérationnel :\n" + "\n".join(details)

    # ── Action : Réindexation / Synchronisation ──────────────────────────────
    if action in ("reindex", "resync", "sync", "synchroniser", "réindexer"):
        stats = knowledge_graph.bootstrap(force=True)
        try:
            from core.file_indexer import get_file_indexer
            files = get_file_indexer().scan_directory_incremental()
            f_indexed = files.get("indexed", 0)
        except Exception:
            f_indexed = 0

        return (
            f"🧠 Second Brain entièrement resynchronisé :\n"
            f"• {stats.get('memories', 0)} souvenir(s) consolidé(s)\n"
            f"• {stats.get('files', 0)} fichier(s) dans le graphe ({f_indexed} actualisé(s))\n"
            f"• {stats.get('contacts', 0)} contact(s) relié(s)."
        )

    # ── Action : Enregistrement de Commande ──────────────────────────────────
    if action in ("save_command", "command", "ingest_command"):
        cmd = params.get("command") or params.get("value") or description
        if not cmd:
            return "Veuillez spécifier la commande à mémoriser."
        desc = params.get("title") or params.get("description") or "Commande mémorisée"
        proj = params.get("project") or ""
        tags = params.get("tags") or ""
        
        node_id = knowledge_graph.ingest_command(
            command=cmd,
            description=desc,
            project=proj,
            tags=tags,
        )
        return f"⚡ Commande mémorisée dans le Second Brain [ID {node_id}] :\n`{cmd}`"

    # ── Action : Indexation d'un Projet de Dev ───────────────────────────────
    if action in ("index_project", "project", "ingest_project"):
        proj_path = params.get("project") or params.get("path") or description
        if not proj_path:
            return "Veuillez indiquer le chemin du projet à indexer."
        
        res = knowledge_graph.ingest_project(proj_path)
        if "error" in res:
            return f"❌ {res['error']}"
        
        techs = ", ".join(res.get("technologies", [])) or "Général"
        return (
            f"📁 Projet '{res.get('project')}' indexé avec succès dans le Second Brain :\n"
            f"• Emplacement : {res.get('path')}\n"
            f"• Technologies détectées : {techs}\n"
            f"• Documentation README indexée : {'Oui' if res.get('readme_indexed') else 'Non'}"
        )

    # ── Action : Enregistrement de Note ──────────────────────────────────────
    if action in ("note", "remember", "ingest_note"):
        title = params.get("title") or "Note"
        content = params.get("content") or description
        proj = params.get("project") or ""
        tags = params.get("tags") or ""
        
        node_id = knowledge_graph.ingest_note(
            title=title,
            content=content,
            project=proj,
            tags=tags,
        )
        return f"📝 Note enregistrée dans le Second Brain [ID {node_id}] : « {title} »."

    # ── Action : Visualisation Mermaid ───────────────────────────────────────
    if action in ("visualize", "graph", "mermaid"):
        q = params.get("query") or description or ""
        limit = int(params.get("limit") or 10)
        mermaid_code = knowledge_graph.generate_mermaid_graph(query=q, limit=limit)
        return f"```mermaid\n{mermaid_code}\n```"

    # ── Action par défaut : Recherche Associative par Analogie ───────────────
    query = params.get("query") or description
    if not query:
        return "Précisez ce que vous souhaitez retrouver dans le Second Brain."

    try:
        limit = max(1, min(20, int(params.get("limit") or 8)))
    except (TypeError, ValueError):
        limit = 8

    results = knowledge_graph.search(query, limit=limit)
    return knowledge_graph.format_results(results)
