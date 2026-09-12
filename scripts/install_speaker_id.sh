#!/usr/bin/env bash
# Installe la reconnaissance du locuteur d'ANO-GPT (CAM++ VoxCeleb, ONNX).
#
# Le modèle tourne entièrement en local, sur onnxruntime, en un seul fil :
# aucune voix ne part sur le réseau pour être identifiée, et le deuxième cœur
# reste à l'audio. Le fichier pèse 29 Mo et n'est pas versionné.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/models/speaker_campplus.onnx"
URL="https://huggingface.co/Wespeaker/wespeaker-voxceleb-campplus-LM/resolve/main/voxceleb_CAM%2B%2B_LM.onnx"

if [ -f "${DEST}" ]; then
    echo "==> Modèle déjà présent : ${DEST}"
else
    echo "==> Téléchargement du modèle d'empreinte vocale (29 Mo)"
    mkdir -p "${ROOT}/models"
    tmp="$(mktemp)"
    trap 'rm -f "${tmp}"' EXIT
    curl -L --fail -o "${tmp}" "${URL}"
    mv "${tmp}" "${DEST}"
    trap - EXIT
    echo "    Installé dans ${DEST}"
fi

python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import onnxruntime  # noqa: F401
except ImportError:
    print("⚠️  onnxruntime manque :  pip install --break-system-packages onnxruntime")
    sys.exit(1)
PY

echo
echo "✅ Reconnaissance du locuteur prête."
echo "   Apprends-lui ta voix : parle-lui deux ou trois phrases, puis dis"
echo "   « apprends ma voix »."
