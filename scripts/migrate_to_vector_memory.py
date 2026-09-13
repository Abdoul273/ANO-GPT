#!/usr/bin/env python3
"""scripts/migrate_to_vector_memory.py — Migration des données SQLite et JSON vers la mémoire vectorielle.

Ce script migre :
1. Les souvenirs structurés de memory/memory.db (table memories : profil, fait, épisode)
2. Les tours de conversation et projets de memory.db (table kg_nodes)
3. Les données du profil hérité memory/long_term.json
Vers memory/vector_memory.db avec :
- Calcul des embeddings ONNX 384 dimensions par lots
- Indexation dans sqlite-vec (vec0)
- Extraction automatique des entités et triplets RDF (avec détection de contradictions)
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

# Ajout du dossier racine au sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.vector_memory import (
    DB_PATH,
    LEGACY_DB_PATH,
    LEGACY_JSON,
    KIND_EPISODE,
    KIND_FACT,
    KIND_PROFILE,
    KIND_TURN,
    EmbeddingEngine,
    VectorMemory,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_vector_memory")


def migrate(
    source_db: Path | str | None = None,
    target_db: Path | str | None = None,
    legacy_json: Path | str | None = None,
    batch_size: int = 32,
    dry_run: bool = False,
    include_kg_conversations: bool = True,
) -> dict[str, int]:
    """Exécute la migration complète et retourne les statistiques."""
    src = Path(source_db) if source_db else LEGACY_DB_PATH
    tgt = Path(target_db) if target_db else DB_PATH
    l_json = Path(legacy_json) if legacy_json else LEGACY_JSON

    stats = {
        "memories_read": 0,
        "memories_migrated": 0,
        "kg_conversations_migrated": 0,
        "kg_projects_migrated": 0,
        "json_entries_migrated": 0,
        "vectors_generated": 0,
        "triples_extracted": 0,
    }

    start_time = time.time()
    logger.info("Démarrage de la migration...")
    logger.info("Source SQLite : %s", src)
    logger.info("Cible vectorielle : %s", tgt)
    logger.info("Source JSON : %s", l_json)

    if dry_run:
        logger.info("[DRY-RUN] Aucun enregistrement ne sera écrit sur le disque.")

    # 1. Initialisation de la cible
    vm: VectorMemory | None = None
    engine = EmbeddingEngine.get_instance()
    if not dry_run:
        vm = VectorMemory(db_path=tgt, model=engine)

    # 2. Migration des souvenirs principaux (table memories)
    if src.is_file():
        logger.info("Lecture de la table 'memories' depuis %s...", src)
        conn_src = sqlite3.connect(src, timeout=10.0)
        conn_src.row_factory = sqlite3.Row
        try:
            mem_rows = conn_src.execute(
                """SELECT id, kind, key, value, category, aliases, created, updated, happened, hits
                   FROM memories ORDER BY id ASC"""
            ).fetchall()
            stats["memories_read"] = len(mem_rows)
            logger.info("Nombre de souvenirs trouvés : %d", len(mem_rows))

            if not dry_run and vm:
                for chunk_idx in range(0, len(mem_rows), batch_size):
                    chunk = mem_rows[chunk_idx : chunk_idx + batch_size]
                    for r in chunk:
                        kind = r["kind"]
                        if kind == "profil":
                            target_kind = KIND_PROFILE
                        elif kind == "episode":
                            target_kind = KIND_EPISODE
                        else:
                            target_kind = KIND_FACT

                        meta = {"source": "legacy_sqlite", "original_id": r["id"]}
                        if r["aliases"]:
                            meta["aliases"] = r["aliases"]

                        vm.save(
                            value=r["value"],
                            kind=target_kind,
                            key=r["key"],
                            category=r["category"],
                            happened=r["happened"] or r["updated"] or r["created"],
                            confidence=1.0,
                            metadata=meta,
                        )
                        stats["memories_migrated"] += 1
                        stats["vectors_generated"] += 1

            # Migration des conversations et projets du Second Brain (kg_nodes)
            if include_kg_conversations:
                try:
                    conv_nodes = conn_src.execute(
                        """SELECT id, title, content, happened, metadata
                           FROM kg_nodes WHERE kind = 'conversation' ORDER BY id ASC"""
                    ).fetchall()
                    logger.info("Nombre de conversations KG trouvées : %d", len(conv_nodes))

                    if not dry_run and vm:
                        for row in conv_nodes:
                            content = str(row["content"] or "")
                            str(row["title"] or "")
                            happened = str(row["happened"] or "")
                            if content:
                                vm.save(
                                    value=content,
                                    kind=KIND_TURN,
                                    key=f"turn_{row['id']}",
                                    category="conversation",
                                    happened=happened,
                                    metadata={"source": "kg_conversation", "original_id": row["id"]},
                                )
                                stats["kg_conversations_migrated"] += 1
                                stats["vectors_generated"] += 1

                    proj_nodes = conn_src.execute(
                        """SELECT id, title, content, aliases, happened, path
                           FROM kg_nodes WHERE kind = 'project' ORDER BY id ASC"""
                    ).fetchall()
                    logger.info("Nombre de projets KG trouvés : %d", len(proj_nodes))

                    if not dry_run and vm:
                        for row in proj_nodes:
                            p_title = str(row["title"] or "")
                            p_content = str(row["content"] or "")
                            if p_title:
                                vm.save(
                                    value=f"Projet {p_title} : {p_content[:200]}",
                                    kind=KIND_FACT,
                                    key=f"projet_{p_title.lower()}",
                                    category="projets",
                                    happened=str(row["happened"] or ""),
                                    metadata={"path": str(row["path"] or "")},
                                )
                                stats["kg_projects_migrated"] += 1
                                stats["vectors_generated"] += 1

                except sqlite3.OperationalError as exc:
                    logger.warning("Tables kg_nodes absentes ou inaccessibles : %s", exc)

        finally:
            conn_src.close()
    else:
        logger.warning("Fichier SQLite source introuvable : %s", src)

    # 3. Migration des données JSON (long_term.json)
    if l_json.is_file():
        logger.info("Vérification et migration de %s...", l_json)
        try:
            raw_data = json.loads(l_json.read_text(encoding="utf-8"))
            if isinstance(raw_data, dict):
                for cat, entries in raw_data.items():
                    if not isinstance(entries, dict):
                        continue
                    for k, item in entries.items():
                        if isinstance(item, dict):
                            val = str(item.get("value", "")).strip()
                            updated = str(item.get("updated", "")).strip()
                        else:
                            val, updated = str(item).strip(), ""

                        if not val:
                            continue

                        k_kind = KIND_PROFILE if cat in {"identity", "preferences", "relationships", "profile"} else KIND_FACT
                        if not dry_run and vm:
                            vm.save(
                                value=val,
                                kind=k_kind,
                                key=k,
                                category=cat,
                                happened=updated or None,
                                metadata={"source": "long_term_json"},
                            )
                            stats["json_entries_migrated"] += 1
                            stats["vectors_generated"] += 1
        except Exception as exc:
            logger.warning("Erreur lors de la lecture de long_term.json : %s", exc)

    # 4. Statistiques finales
    if vm is not None and not dry_run:
        final_stats = vm.stats()
        stats["triples_extracted"] = final_stats.get("triples_total", 0)

    duration = time.time() - start_time
    logger.info("─" * 50)
    logger.info("Migration terminée en %.2f secondes !", duration)
    logger.info("  • Souvenirs migrés : %d", stats["memories_migrated"])
    logger.info("  • Conversations KG migrées : %d", stats["kg_conversations_migrated"])
    logger.info("  • Projets KG migrés : %d", stats["kg_projects_migrated"])
    logger.info("  • Entrées JSON migrées : %d", stats["json_entries_migrated"])
    logger.info("  • Embeddings vectoriels calculés : %d", stats["vectors_generated"])
    logger.info("  • Triplets RDF extraits dans le graphe : %d", stats["triples_extracted"])
    logger.info("─" * 50)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migration de la mémoire ANO-GPT vers la mémoire vectorielle sqlite-vec"
    )
    parser.add_argument(
        "--source",
        type=str,
        default=str(LEGACY_DB_PATH),
        help="Chemin de la base de données source SQLite (défaut: memory/memory.db)",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=str(DB_PATH),
        help="Chemin de la base vectorielle cible (défaut: memory/vector_memory.db)",
    )
    parser.add_argument(
        "--json",
        type=str,
        default=str(LEGACY_JSON),
        help="Chemin du fichier long_term.json (défaut: memory/long_term.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Taille des lots pour le calcul des embeddings (défaut: 32)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simule la lecture sans écriture sur le disque",
    )
    parser.add_argument(
        "--no-kg",
        action="store_true",
        help="Ignore les conversations et projets de kg_nodes",
    )

    args = parser.parse_args()

    try:
        migrate(
            source_db=args.source,
            target_db=args.target,
            legacy_json=args.json,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            include_kg_conversations=not args.no_kg,
        )
    except Exception as exc:
        logger.error("Échec critique de la migration : %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
