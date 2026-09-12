"""core/semantic_enricher.py — Enrichissement sémantique à l'écriture pour la mémoire et les fichiers.

Principe clé (ANO-GPT §4) :
- Enrichir à l'écriture, JAMAIS à la lecture.
- La recherche reste une requête SQLite FTS5 ultra-rapide (< 1 ms), sans installer
  de base vectorielle ni faire tourner de modèle d'embedding sur le CPU audio.
- Au moment où un souvenir ou document est enregistré, on génère 5 à 10 mots-clés
  élargis (synonymes, entités, thème générique, hypernymes).
- Deux niveaux :
  1. Lexique sémantique local ultra-rapide (< 0.1 ms) disponible en permanence et hors-ligne.
  2. Enrichissement profond via agent_brain (Antigravity en processus de fond basse priorité)
     quand disponible.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("anogpt.semantic_enricher")

def _normalize_word(w: str) -> str:
    """Retire les accents pour comparaison uniforme."""
    return "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c)).lower()


# Lexique de grappes sémantiques françaises pour l'enrichissement instantané
_SEMANTIC_CLUSTERS: dict[str, list[str]] = {
    "véhicule": [
        "voiture", "auto", "automobile", "véhicule", "vehicule", "bagnole",
        "garage", "mécanicien", "mecanicien", "panne", "réparation", "reparation", "vidange",
        "moteur", "pneu", "frein", "batterie", "peugeot", "renault", "citroen",
        "toyota", "mercedes", "bmw", "hyundai", "permis", "conduite", "essence", "diesel",
    ],
    "santé": [
        "santé", "sante", "médecin", "medecin", "docteur", "soin", "soins",
        "hôpital", "hopital", "clinique", "pharmacie", "médicament", "medicament",
        "ordonnance", "malade", "fièvre", "fievre", "douleur", "dentiste", "analyse", "traitement",
    ],
    "technologie": [
        "technologie", "informatique", "ordinateur", "machine", "pc", "serveur", "linux",
        "code", "programme", "script", "python", "javascript", "typescript", "rust",
        "api", "base de données", "database", "sqlite", "sql", "réseau", "reseau",
        "architecture", "routeur", "switch", "wifi", "ethernet", "ip", "vlan",
        "git", "github", "bug", "terminal", "bash", "docker",
    ],
    "logement": [
        "logement", "maison", "appartement", "loyer", "bail", "propriétaire", "proprietaire",
        "locataire", "facture", "électricité", "electricite", "eau", "edg", "seg",
        "contrat", "déménagement", "demenagement", "adresse", "quartier",
    ],
    "travail": [
        "travail", "boulot", "job", "bureau", "projet", "client", "réunion", "reunion",
        "rendez-vous", "rdv", "devis", "facture", "rapport", "présentation", "presentation",
        "livraison", "mission", "salaire", "contrat", "équipe", "equipe", "collègue", "collegue",
    ],
    "relations": [
        "famille", "ami", "amie", "pote", "collègue", "collegue", "patron", "chef",
        "mari", "femme", "épouse", "epouse", "enfant", "fils", "fille", "frère", "frere",
        "soeur", "sœur", "parent", "père", "pere", "mère", "mere", "anniversaire", "fête", "fete",
    ],
    "achats": [
        "achat", "acheter", "magasin", "boutique", "supermarché", "supermarche", "prix", "coût",
        "cout", "payer", "dépense", "depense", "argent", "banque", "carte", "virement", "commande", "colis",
    ],
    "voyage": [
        "voyage", "trajet", "vol", "avion", "aéroport", "aeroport", "train", "gare",
        "hôtel", "hotel", "vacances", "séjour", "sejour", "passeport", "visa", "destination",
    ],
}

# Index inversé terme -> grappes ordonnées
_TERM_TO_CLUSTERS: dict[str, list[str]] = {}
for cluster_name, terms in _SEMANTIC_CLUSTERS.items():
    for term in terms:
        norm_term = _normalize_word(term)
        if norm_term not in _TERM_TO_CLUSTERS:
            _TERM_TO_CLUSTERS[norm_term] = []
        for t in terms:
            if t not in _TERM_TO_CLUSTERS[norm_term]:
                _TERM_TO_CLUSTERS[norm_term].append(t)
            norm_t = _normalize_word(t)
            if norm_t not in _TERM_TO_CLUSTERS[norm_term]:
                _TERM_TO_CLUSTERS[norm_term].append(norm_t)


def extract_local_aliases(text: str, category: str = "", key: str = "") -> str:
    """Génère instantanément des alias et concepts associés par analyse lexicale locale."""
    combined = f"{key} {category} {text}".lower()
    words = re.findall(r"[\w-]{3,}", combined, flags=re.UNICODE)
    
    aliases: list[str] = []
    seen = set(words) | {_normalize_word(w) for w in words}
    
    # 1. Ajout de termes spécifiques et entités identifiés
    for w in words:
        norm_w = _normalize_word(w)
        if norm_w in _TERM_TO_CLUSTERS:
            for related_term in _TERM_TO_CLUSTERS[norm_w]:
                if related_term not in seen:
                    aliases.append(related_term)
                    seen.add(related_term)
                
    # 2. Détection de motifs temporels ou contextuels
    norm_combined = _normalize_word(combined)
    if any(d in norm_combined for d in ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")):
        for tag in ("planning", "agenda", "calendrier", "jour"):
            if tag not in seen:
                aliases.append(tag)
                seen.add(tag)
    if any(m in norm_combined for m in ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre", "novembre", "decembre")):
        for tag in ("date", "calendrier", "échéance", "echeance"):
            if tag not in seen:
                aliases.append(tag)
                seen.add(tag)
        
    return " ".join(aliases[:25])


# ── File d'attente d'enrichissement asynchrone ────────────────────────────────

class _DaemonEnrichmentWorker:
    """File bornée à un worker qui ne retient jamais l'arrêt du programme.

    ``ThreadPoolExecutor`` crée des threads non-daemon et son hook interne les
    rejoint avant les hooks ``atexit`` de l'application. Une analyse agent un
    peu longue suffisait donc à bloquer la fermeture d'ANO-GPT et de pytest.
    """

    def __init__(self, max_pending: int = 128) -> None:
        self._queue: queue.Queue[tuple[Callable, tuple] | None] = queue.Queue(
            maxsize=max_pending
        )
        self._thread = threading.Thread(
            target=self._run,
            name="ano-semantic-enricher",
            daemon=True,
        )
        self._thread.start()

    def submit(self, func: Callable, *args) -> bool:
        try:
            self._queue.put_nowait((func, args))
            return True
        except queue.Full:
            logger.warning("File d'enrichissement pleine : tâche ignorée")
            return False

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                func, args = task
                func(*args)
            except Exception:
                logger.exception("Tâche d'enrichissement sémantique en échec")
            finally:
                self._queue.task_done()

    def shutdown(self, *, wait: bool = False) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Le thread est daemon : une file saturée ne doit pas bloquer la
            # fermeture. Il s'arrêtera avec le processus.
            return
        if wait:
            self._thread.join(timeout=5.0)


_EXECUTOR: Optional[_DaemonEnrichmentWorker] = None
_EXEC_LOCK = threading.Lock()


def _get_executor() -> _DaemonEnrichmentWorker:
    global _EXECUTOR
    with _EXEC_LOCK:
        if _EXECUTOR is None:
            # Un seul thread de fond pour ne pas surcharger les 2 cœurs
            _EXECUTOR = _DaemonEnrichmentWorker()
        return _EXECUTOR


def shutdown_semantic_enricher(*, wait: bool = False) -> None:
    """Arrête explicitement le worker, principalement pour tests/redémarrage."""
    global _EXECUTOR
    with _EXEC_LOCK:
        worker = _EXECUTOR
        _EXECUTOR = None
    if worker is not None:
        worker.shutdown(wait=wait)


def enrich_memory_record(
    db_path: Path | str,
    memory_id: int,
    value: str,
    category: str = "",
    key: str = "",
    use_agent: bool = True,
) -> None:
    """Enrichit un enregistrement de mémoire dans SQLite avec ses alias sémantiques."""
    try:
        # 1. Alias locaux rapides
        local_aliases = extract_local_aliases(value, category=category, key=key)
        
        # 2. Enrichissement agent_brain si disponible
        agent_aliases = ""
        if use_agent:
            try:
                from core import agent_brain
                if agent_brain.available():
                    agent_aliases = agent_brain.generate_memory_aliases(value, category=category)
            except Exception as exc:
                logger.debug("agent_brain non disponible pour enrichissement : %s", exc)
                
        # Combinaison des alias sans doublons
        combined_set = set(local_aliases.split()) | set(agent_aliases.split())
        final_aliases = " ".join(sorted(combined_set))[:300]
        
        if not final_aliases:
            return
            
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "UPDATE memories SET aliases = ? WHERE id = ?",
                (final_aliases, memory_id)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Échec enrichissement mémoire #%d: %s", memory_id, exc)


def schedule_memory_enrichment(
    db_path: Path | str,
    memory_id: int,
    value: str,
    category: str = "",
    key: str = "",
) -> None:
    """Programme l'enrichissement en arrière-plan sans bloquer l'appelant."""
    executor = _get_executor()
    executor.submit(
        enrich_memory_record,
        db_path,
        memory_id,
        value,
        category,
        key,
        True,
    )


# ── Enrichissement des documents / fichiers ──────────────────────────────────

def extract_document_summary(filename: str, content: str) -> str:
    """Génère un résumé textuel et des mots-clés conceptuels pour l'index de fichiers."""
    content_clean = (content or "").strip()
    if not content_clean:
        return ""
        
    lines = [l.strip() for l in content_clean.splitlines() if l.strip()]
    
    # 1. Extraction d'en-têtes et premières lignes informatives
    summary_parts = []
    for line in lines[:6]:
        if line.startswith(("#", "//", "/*", "'''", '"""', "--")):
            cleaned = re.sub(r"^[#/\\*'-]+\s*", "", line).strip()
            if cleaned and len(cleaned) > 5:
                summary_parts.append(cleaned)
        elif len(summary_parts) < 3 and len(line) > 10:
            summary_parts.append(line[:120])
            
    # 2. Enrichissement thématique local
    local_tags = extract_local_aliases(content_clean[:1000], key=filename)
    
    base_summary = " · ".join(summary_parts[:3])
    if local_tags:
        base_summary = f"{base_summary} [Thèmes: {local_tags}]" if base_summary else f"[Thèmes: {local_tags}]"
        
    return base_summary[:400]
