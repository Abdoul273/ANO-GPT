"""Supervision, stockage et catalogue de modèles : ce qui tient dans la durée."""

import ast
import configparser
import re
import sqlite3
from pathlib import Path

from core import storage_maintenance as maintenance
from core.live_model_policy import MODEL_CATALOG, FAST_MODEL, model_for

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "config" / "systemd" / "anogpt.service"


# ── 7. Supervision ───────────────────────────────────────────────────────────

def test_le_superviseur_interne_ne_couvre_que_les_crashs_natifs():
    """Il relève SIGABRT/SIGBUS/SIGSEGV — et laisse tout le reste mourir."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "native_crashes = {-6, -7, -11}" in source
    assert "_supervise_native_process()" in source


def _unit() -> configparser.ConfigParser:
    """Lit l'unité en INI : un découpage textuel attrape les commentaires."""
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser


def test_l_unite_systemd_couvre_ce_que_le_superviseur_laisse_passer():
    unit = _unit()
    service, section = unit["Service"], unit["Unit"]
    assert service["Restart"] == "on-failure"
    # Depuis systemd 229 ces deux clés appartiennent à [Unit] ; placées dans
    # [Service], elles sont silencieusement ignorées et le plafond disparaît.
    assert section["StartLimitBurst"].isdigit()
    assert section["StartLimitIntervalSec"].isdigit()
    # Application graphique : sans la session, Qt ne peut pas créer sa fenêtre.
    assert "graphical-session.target" in section["After"]
    assert "graphical-session.target" in section["PartOf"]
    # Un MemoryMax tuerait l'assistant en pleine phrase ; MemoryHigh freine.
    assert "MemoryMax" not in service and "MemoryHigh" in service


def test_l_installation_rappelle_l_environnement_wayland():
    script = (ROOT / "scripts" / "install-systemd-unit.sh").read_text(encoding="utf-8")
    assert "import-environment" in script, (
        "sans import-environment, le service démarre sans WAYLAND_DISPLAY"
    )
    assert "systemctl --user enable" in script


# ── 8. Stockage ──────────────────────────────────────────────────────────────

def test_le_rapport_lit_sans_jamais_ecrire(tmp_path, monkeypatch):
    base = tmp_path / "essai.db"
    with sqlite3.connect(base) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(2000)])
    empreinte = base.stat().st_mtime_ns

    monkeypatch.setattr(maintenance, "MEMORY_DIR", tmp_path)
    entries = maintenance.report()
    assert [e.name for e in entries] == ["essai.db"]
    assert entries[0].total_bytes > 0
    assert base.stat().st_mtime_ns == empreinte, "un rapport ne doit rien modifier"


def test_le_compactage_ne_se_declenche_pas_pour_rien(tmp_path, monkeypatch):
    base = tmp_path / "pleine.db"
    with sqlite3.connect(base) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("x" * 200,) for _ in range(500)])

    monkeypatch.setattr(maintenance, "MEMORY_DIR", tmp_path)
    fait, message = maintenance.compact(base)
    assert fait is False and "inutile" in message


def test_le_compactage_recupere_reellement_l_espace(tmp_path):
    base = tmp_path / "trouee.db"
    with sqlite3.connect(base) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)",
                         [("x" * 2000,) for _ in range(30000)])
        conn.commit()
        conn.execute("DELETE FROM t WHERE id % 10 != 0")  # 90 % supprimé
        conn.commit()

    avant = maintenance.inspect(base)
    assert avant.should_compact, "une base vidée à 90 % doit être détectée"
    fait, message = maintenance.compact(base)
    apres = maintenance.inspect(base)
    assert fait is True, message
    assert apres.total_bytes < avant.total_bytes
    assert apres.free_ratio < 0.05


def test_l_entretien_leger_est_branche_hors_du_chemin_de_la_voix():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "startup_maintenance" in source
    bloc = source.split("_maintain_storage")[1][:1200]
    assert '"disk-io"' in bloc, "l'entretien doit passer par le pool disque"
    # Un VACUUM au démarrage réécrirait 275 Mo pendant que l'utilisateur parle.
    assert "VACUUM" not in (ROOT / "main.py").read_text(encoding="utf-8")


def test_l_index_n_est_pas_charge_en_memoire_au_demarrage():
    """Le moteur d'embeddings est paresseux : 87 Mo d'ONNX à la demande."""
    source = (ROOT / "core" / "vector_memory.py").read_text(encoding="utf-8")
    moteur = source.split("class EmbeddingEngine")[1].split("\nclass ")[0]
    init = moteur.split("def __init__")[1].split("\n    def ")[0]
    assert "InferenceSession(" not in init, "le modèle est chargé dès la construction"
    assert "self._session" in init and "None" in init


# ── 9. Catalogue de modèles ──────────────────────────────────────────────────

_MODEL_LITERAL = re.compile(r'["\']((?:models/)?gemini-[0-9][a-z0-9.\-]*|'
                            r'(?:models/)?gemini-(?:flash|pro)[a-z0-9.\-]*)["\']')


def test_aucun_identifiant_de_modele_hors_du_catalogue():
    catalogue = (ROOT / "core" / "live_model_policy.py").resolve()
    faults = []
    for path in sorted(ROOT.glob("**/*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if (relative.startswith(("tests/", "build/", "mobile/", "scripts/"))
                or "__pycache__" in relative or path.resolve() == catalogue):
            continue
        for numero, ligne in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _MODEL_LITERAL.search(ligne):
                faults.append(f"{relative}:{numero}  {ligne.strip()[:80]}")
    assert not faults, (
        "identifiant de modèle en dur — une montée de version l'oublierait :\n  "
        + "\n  ".join(faults)
    )


def test_le_catalogue_couvre_tous_les_roles_utilises():
    assert model_for("fast") == FAST_MODEL
    assert model_for("role_inexistant") == FAST_MODEL, "un rôle inconnu doit retomber sur le rapide"
    assert set(MODEL_CATALOG) >= {
        "live_primary", "live_fallback", "fast", "balanced",
        "reasoning", "transcribe", "screen_live",
    }
    for role, identifiant in MODEL_CATALOG.items():
        assert identifiant.strip() and "gemini" in identifiant, role


def test_le_catalogue_reste_importable_de_partout():
    """Aucune dépendance lourde : les actions doivent pouvoir l'importer."""
    tree = ast.parse((ROOT / "core" / "live_model_policy.py").read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) else
        node.names[0].name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert imported <= {"__future__", "dataclasses"}, (
        f"le catalogue tire {imported} : risque de cycle d'import depuis actions/"
    )
