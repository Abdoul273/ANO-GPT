"""Surveillance et compactage des bases locales.

Constat mesuré, contre l'intuition : les bases ne sont **pas** ballonnées (0 à
2 % de pages libres) et rien n'est chargé en mémoire — ``EmbeddingEngine`` est
paresseux et sqlite-vec interroge sur disque. Les 337 Mo sont de la donnée
réelle : 76 917 fragments issus de 3 609 fichiers personnels, plus un modèle
ONNX de 87 Mo.

Le risque est donc **latent**, pas actuel, et il tient en une ligne :
``auto_vacuum=0`` partout. Le jour où un gros dossier sort de l'index, SQLite
gardera ses pages libres pour toujours — 275 Mo qui ne reviendront jamais.

D'où ce module, qui fait trois choses et pas une de plus :

* **mesurer** — taille réelle, pages libres, lignes par table ;
* **entretenir à coût nul** — ``PRAGMA optimize`` et point de reprise WAL,
  quelques millisecondes, sans réécrire le fichier ;
* **compacter seulement quand ça vaut le coup** — un ``VACUUM`` réécrit
  intégralement 275 Mo : sur cette machine, c'est plusieurs secondes de disque
  saturé. Jamais pendant que l'assistant parle, et jamais « au cas où ».
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = BASE_DIR / "memory"

#: En deçà, un VACUUM coûte plus qu'il ne rend.
VACUUM_MIN_FREE_RATIO = 0.20
VACUUM_MIN_FREE_BYTES = 20 * 1024 * 1024
USER_ABSENCE_BEFORE_VACUUM_S = 10 * 60

logger = logging.getLogger("anogpt.storage")


@dataclass(frozen=True)
class DatabaseReport:
    path: Path
    total_bytes: int
    free_bytes: int
    auto_vacuum: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def free_ratio(self) -> float:
        return self.free_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def should_compact(self) -> bool:
        return (self.free_ratio >= VACUUM_MIN_FREE_RATIO
                and self.free_bytes >= VACUUM_MIN_FREE_BYTES)

    def __str__(self) -> str:
        verdict = "à compacter" if self.should_compact else "sain"
        return (f"{self.name:24s} {self.total_bytes/1e6:8.1f} Mo | "
                f"libre {self.free_bytes/1e6:7.1f} Mo ({self.free_ratio:5.1%}) | {verdict}")


def databases() -> list[Path]:
    """Bases SQLite du dossier mémoire, du plus gros au plus petit."""
    found = [p for p in MEMORY_DIR.glob("*.db") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_size, reverse=True)


def inspect(path: Path) -> DatabaseReport | None:
    """Mesure sans écrire : ouverture en lecture seule."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0) as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
            auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    except sqlite3.Error as exc:
        logger.warning("base illisible", extra={"db": path.name, "error": str(exc)})
        return None
    return DatabaseReport(
        path=path,
        total_bytes=page_size * page_count,
        free_bytes=page_size * free_pages,
        auto_vacuum=auto_vacuum,
    )


def report() -> list[DatabaseReport]:
    return [r for r in (inspect(p) for p in databases()) if r is not None]


def light_maintenance(path: Path) -> bool:
    """Entretien à quelques millisecondes : statistiques et reprise WAL.

    ``PRAGMA optimize`` remet à jour les statistiques d'index — sans elles, le
    planificateur de requêtes finit par choisir de mauvais plans sur une table
    qui a beaucoup grossi. Le point de reprise WAL évite qu'un ``-wal`` de
    plusieurs dizaines de mégaoctets traîne indéfiniment à côté de la base.
    """
    try:
        with sqlite3.connect(path, timeout=5.0) as conn:
            conn.execute("PRAGMA optimize")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error as exc:
        logger.warning("entretien léger impossible",
                       extra={"db": path.name, "error": str(exc)})
        return False


def compact(path: Path, *, force: bool = False) -> tuple[bool, str]:
    """VACUUM, seulement si l'espace récupérable le justifie.

    Ne jamais appeler pendant que l'assistant parle : la base est verrouillée
    et le fichier entier est réécrit.
    """
    before = inspect(path)
    if before is None:
        return False, f"{path.name} : illisible."
    if not force and not before.should_compact:
        return False, (f"{path.name} : {before.free_ratio:.1%} de pages libres, "
                       "compactage inutile.")
    started = time.monotonic()
    try:
        with sqlite3.connect(path, timeout=30.0) as conn:
            conn.execute("VACUUM")
    except sqlite3.Error as exc:
        logger.error("compactage échoué", extra={"db": path.name, "error": str(exc)})
        return False, f"{path.name} : compactage impossible ({exc})."
    after = inspect(path)
    gained = (before.total_bytes - after.total_bytes) if after else 0
    elapsed = time.monotonic() - started
    logger.info("base compactée", extra={"db": path.name, "gained_bytes": gained,
                                         "seconds": round(elapsed, 2)})
    return True, (f"{path.name} : {gained/1e6:.1f} Mo récupérés en {elapsed:.1f} s.")


def enable_incremental_vacuum(path: Path) -> tuple[bool, str]:
    """Passe la base en ``auto_vacuum=INCREMENTAL`` pour l'avenir.

    C'est le correctif de fond : une base incrémentale peut rendre ses pages
    libres au fil de l'eau, sans jamais réécrire le fichier entier. Le
    basculement, lui, exige un VACUUM complet — d'où le coût unique assumé ici.
    """
    before = inspect(path)
    if before is None:
        return False, f"{path.name} : illisible."
    if before.auto_vacuum == 2:
        return False, f"{path.name} : déjà en mode incrémental."
    try:
        with sqlite3.connect(path, timeout=60.0) as conn:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("VACUUM")  # obligatoire pour appliquer le changement
    except sqlite3.Error as exc:
        return False, f"{path.name} : bascule impossible ({exc})."
    return True, f"{path.name} : passée en compactage incrémental."


def compact_on_shutdown(*, idle_seconds: float) -> list[str]:
    """Compacte les bases réellement trouées à l'arrêt, jamais à chaud.

    Le seuil d'absence protège la fermeture déclenchée juste après une
    conversation : un ``VACUUM`` ne doit pas prolonger celle-ci ni entrer en
    concurrence avec les écritures encore en vol.  Cette fonction est appelée
    après l'arrêt de la boucle Live et des pools, donc aucun lecteur ANO-GPT ne
    conserve une transaction ouverte.
    """
    if idle_seconds < USER_ABSENCE_BEFORE_VACUUM_S:
        logger.info("compactage différé : utilisateur actif récemment",
                    extra={"idle_seconds": round(idle_seconds, 1)})
        return []

    candidates = [entry.path for entry in report() if entry.should_compact]
    messages: list[str] = []
    for path in candidates:
        done, message = compact(path)
        if done:
            messages.append(message)
        else:
            logger.warning("compactage différé", extra={"db": path.name, "reason": message})
    return messages


def startup_maintenance() -> None:
    """Entretien léger au démarrage, jamais de VACUUM.

    Appelé hors du chemin de la voix. Ce qui est coûteux est seulement
    *signalé* : c'est à l'utilisateur de lancer le compactage quand il le
    décide, avec ``scripts/anogpt-maintenance.py``.
    """
    for entry in report():
        light_maintenance(entry.path)
        if entry.should_compact:
            logger.warning(
                "compactage recommandé",
                extra={"db": entry.name,
                       "free_mb": round(entry.free_bytes / 1e6, 1),
                       "free_ratio": round(entry.free_ratio, 3)},
            )
