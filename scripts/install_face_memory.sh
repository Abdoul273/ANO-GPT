#!/usr/bin/env bash
# Installe la mémoire des visages d'ANO-GPT (YuNet + SFace, OpenCV Zoo).
#
# Les deux réseaux tournent en local dans OpenCV, en un seul fil : aucun
# visage ne part sur le réseau pour être reconnu, et le deuxième cœur reste
# à la voix. 37 Mo au total, non versionnés. ANO-GPT les télécharge aussi
# tout seul à la première demande « c'est qui ? » ; ce script sert à le
# faire à l'avance.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"
mkdir -p "${ROOT}/models"

fetch() {
    local dest="${ROOT}/models/$1" url="$2" min="$3"
    if [ -f "${dest}" ] && [ "$(stat -c %s "${dest}")" -ge "${min}" ]; then
        echo "==> Déjà présent : ${dest}"
        return
    fi
    echo "==> Téléchargement de $1"
    tmp="$(mktemp)"
    curl -L --fail -o "${tmp}" "${url}"
    mv "${tmp}" "${dest}"
}

fetch face_detection_yunet_2023mar.onnx "${ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx" 200000
fetch face_recognition_sface_2021dec.onnx "${ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx" 30000000

python3 - <<'PY'
import cv2
assert hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF"), "OpenCV trop ancien (>= 4.8 requis)"
print("✅ Mémoire des visages prête (OpenCV", cv2.__version__ + ").")
print("   Montre quelqu'un à la caméra et demande « c'est qui ? ».")
PY
