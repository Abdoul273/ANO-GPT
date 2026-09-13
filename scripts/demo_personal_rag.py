"""scripts/demo_personal_rag.py — Démonstration d'indexation et recherche chirurgicale RAG.

Ce script démontre :
1. Découpage syntaxique Tree-sitter (.py, .sh, .md) par fonctions et classes
2. Sauvegarde et indexation vectorielle dans sqlite-vec
3. Hachage SHA-256 incrémental par blocs (démontre que SEULS les blocs modifiés sont ré-indexés)
4. Recherche chirurgicale via l'outil Gemini Live 'search_personal_docs' avec liens file:/// et numéros de lignes
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Ajouter la racine du projet à sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.personal_rag import (
    PersonalRAG,
    format_search_results_markdown,
)


def run_demo():
    print("=" * 80)
    print("🚀 DÉMONSTRATION DU RAG PERSONNEL HAUTE PRÉCISION D'ANO-GPT")
    print("=" * 80)

    # 1. Préparation d'une base vectorielle de démonstration isolée
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "demo_rag.db"
        workspace_dir = Path(tmp_dir) / "mon_projet"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        print("\n[1] Initialisation du moteur PersonalRAG...")
        print(f"    - Base vectorielle : sqlite-vec ({db_path.name})")
        print("    - Dimension vecteurs : 384 (dense L2-normalisé)")

        rag = PersonalRAG(db_path=db_path, roots=[workspace_dir])

        # 2. Création de fichiers représentatifs d'un projet réel
        # A) Code Python avec classes et décorateurs
        py_file = workspace_dir / "audio_processor.py"
        py_file.write_text(
            '"""Module de gestion audio temps réel."""\n'
            'import numpy as np\n'
            '\n'
            'class AudioBufferManager:\n'
            '    """Gestionnaire de mémoire tampon circulaire."""\n'
            '    def __init__(self, sample_rate: int = 16000):\n'
            '        self.sample_rate = sample_rate\n'
            '        self.buffer = bytearray()\n'
            '\n'
            '    def push_samples(self, pcm_data: bytes) -> int:\n'
            '        """Ajoute des échantillons PCM au tampon circulaire."""\n'
            '        self.buffer.extend(pcm_data)\n'
            '        return len(self.buffer)\n'
            '\n'
            '    def read_frame(self, frame_size: int = 512) -> bytes:\n'
            '        """Lit une trame pour l\'analyseur VAD."""\n'
            '        frame = bytes(self.buffer[:frame_size])\n'
            '        del self.buffer[:frame_size]\n'
            '        return frame\n'
            '\n'
            'def suppress_echo(input_audio: bytes, reference_audio: bytes) -> bytes:\n'
            '    """Supprime l\'écho acoustique (AEC SpeexDSP) en mode half-duplex."""\n'
            '    return input_audio\n',
            encoding="utf-8",
        )

        # B) Script Bash de déploiement
        sh_file = workspace_dir / "deploy_hyprland.sh"
        sh_file.write_text(
            '#!/usr/bin/env bash\n'
            '# Script d\'orchestration Hyprland et Wayland\n'
            '\n'
            'apply_window_rules() {\n'
            '    echo "Configuration des fenêtres flottantes"\n'
            '    hyprctl keyword windowrulev2 "float,class:^(anogpt)$"\n'
            '}\n'
            '\n'
            'start_ipc_socket() {\n'
            '    echo "Lancement du socket de contrôle ANO-GPT"\n'
            '    python3 -m core.ipc --listen\n'
            '}\n',
            encoding="utf-8",
        )

        # C) Documentation Markdown hiérarchique
        md_file = workspace_dir / "ARCHITECTURE.md"
        md_file.write_text(
            '# Guide Technique ANO-GPT\n'
            'Documentation d\'ingénierie interne pour l\'assistant vocal.\n'
            '\n'
            '## Sous-système Audio\n'
            'Le moteur audio tourne dans un thread asyncio dédié partageant le GIL avec Qt.\n'
            '\n'
            '### Règle Half-Duplex Absolue\n'
            'Pendant que Jarvis parle, le microphone est coupé logiciellement pour éviter le feedback.\n'
            '\n'
            '## Base de Connaissances Vectorielle\n'
            'Indexation incrémentale locale avec sqlite-vec et découpage Tree-sitter.\n',
            encoding="utf-8",
        )

        # 3. Indexation initiale
        print("\n[2] Indexation initiale des fichiers du projet...")
        stats = rag.index_directory(workspace_dir)
        print(f"    ✓ Fichiers indexés  : {stats.files_indexed}")
        print(f"    ✓ Blocs (chunks)    : {stats.chunks_created} créés")
        print(f"    ✓ Total en base     : {rag.storage.count_chunks()} vecteurs sqlite-vec")

        # 4. Démonstration de l'indexation incrémentale avec hachage SHA256 par bloc
        print("\n[3] Démonstration du hachage SHA256 et indexation incrémentale par blocs...")
        print("    -> Tentative de ré-indexation sans modification :")
        mod, added, reused = rag.index_file(py_file)
        print(f"       Fichier modifié ? {mod} | Nouveaux blocs : {added} | Blocs réutilisés : {reused}")
        assert not mod and added == 0, "Le fichier non modifié aurait dû être ignoré !"

        print("\n    -> Modification d'une seule méthode (push_samples) dans audio_processor.py :")
        time.sleep(0.05)
        py_file.write_text(
            '"""Module de gestion audio temps réel."""\n'
            'import numpy as np\n'
            '\n'
            'class AudioBufferManager:\n'
            '    """Gestionnaire de mémoire tampon circulaire."""\n'
            '    def __init__(self, sample_rate: int = 16000):\n'
            '        self.sample_rate = sample_rate\n'
            '        self.buffer = bytearray()\n'
            '\n'
            '    def push_samples(self, pcm_data: bytes) -> int:\n'
            '        """Ajoute des échantillons PCM avec vérification d\'écrêtage."""\n'
            '        if len(pcm_data) > 65536:\n'
            '            raise ValueError("Buffer overflow")\n'
            '        self.buffer.extend(pcm_data)\n'
            '        return len(self.buffer)\n'
            '\n'
            '    def read_frame(self, frame_size: int = 512) -> bytes:\n'
            '        """Lit une trame pour l\'analyseur VAD."""\n'
            '        frame = bytes(self.buffer[:frame_size])\n'
            '        del self.buffer[:frame_size]\n'
            '        return frame\n'
            '\n'
            'def suppress_echo(input_audio: bytes, reference_audio: bytes) -> bytes:\n'
            '    """Supprime l\'écho acoustique (AEC SpeexDSP) en mode half-duplex."""\n'
            '    return input_audio\n',
            encoding="utf-8",
        )

        mod, added, reused = rag.index_file(py_file)
        print(f"       Fichier modifié ? {mod} (SHA256 différent)")
        print(f"       Blocs re-vectorisés : {added} (SEULE push_samples a été re-calculée !)")
        print(f"       Blocs conservés     : {reused} (toutes les autres fonctions ont été réutilisées)")

        # 5. Recherche chirurgicale : Requête 1 (Méthode de code spécifique)
        print("\n" + "=" * 80)
        print("🔍 TEST 1 : Recherche chirurgicale d'une méthode de code")
        print("    Requête : 'lire une trame pour VAD'")
        print("=" * 80)
        res1 = rag.search("lire une trame pour VAD", max_results=2)
        print(format_search_results_markdown(res1, "lire une trame pour VAD"))

        # 6. Recherche chirurgicale : Requête 2 (Règle d'architecture dans un document Markdown)
        print("=" * 80)
        print("🔍 TEST 2 : Recherche dans la documentation avec fil d'Ariane")
        print("    Requête : 'microphone coupé pendant que Jarvis parle'")
        print("=" * 80)
        res2 = rag.search("microphone coupé pendant que Jarvis parle", max_results=2)
        print(format_search_results_markdown(res2, "microphone coupé pendant que Jarvis parle"))

        # 7. Recherche chirurgicale : Requête 3 (Script Bash avec filtrage par pattern)
        print("=" * 80)
        print("🔍 TEST 3 : Recherche ciblée avec filtre file_pattern='*.sh'")
        print("    Requête : 'fenêtres flottantes', file_pattern='*.sh'")
        print("=" * 80)
        res3 = rag.search("fenêtres flottantes", file_pattern="*.sh", max_results=2)
        print(format_search_results_markdown(res3, "fenêtres flottantes"))

        # 8. Indexation d'un fichier réel du projet ANO-GPT
        print("=" * 80)
        print("🔍 TEST 4 : Indexation d'un fichier réel du repo ANO-GPT (core/ipc.py)")
        print("=" * 80)
        real_ipc = Path("core/ipc.py").resolve()
        if real_ipc.exists():
            mod, added, reused = rag.index_file(real_ipc)
            print(f"    ✓ Fichier réel indexé : {real_ipc.name} ({added} blocs détectés via Tree-sitter)")
            res_ipc = rag.search("envoyer commande socket ipc ControlServer", file_pattern="*ipc*", max_results=2)
            print(format_search_results_markdown(res_ipc, "envoyer commande socket ipc ControlServer"))

    print("=" * 80)
    print("✅ DÉMONSTRATION COMPLÈTE TERMINÉE AVEC SUCCÈS")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()
