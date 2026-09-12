#!/usr/bin/env bash
# Install the offline wake-word stack for ANO-GPT (Vosk + small French model).
# The model runs locally, so nothing is streamed to the network while muted.
set -euo pipefail

MODEL_DIR="${HOME}/.cache/anogpt/models"
MODEL_NAME="vosk-model-small-fr-0.22"
MODEL_URL="https://alphacephei.com/vosk/models/${MODEL_NAME}.zip"
DOWNLOAD_DIR="${HOME}/.cache/anogpt/downloads"
MODEL_ARCHIVE="${DOWNLOAD_DIR}/${MODEL_NAME}.zip.part"

echo "==> Installing Python dependencies"
PIP_ARGS=""
# Arch/EndeavourOS marks the system Python as externally managed (PEP 668).
if python3 -c 'import sys; sys.exit(0)' && pip install --dry-run vosk >/dev/null 2>&1; then
    :
else
    PIP_ARGS="--break-system-packages"
fi
pip install $PIP_ARGS vosk webrtcvad-wheels

echo "==> Installing Vosk model into ${MODEL_DIR}"
mkdir -p "${MODEL_DIR}"
if [ -d "${MODEL_DIR}/${MODEL_NAME}" ]; then
    echo "    Already present, skipping download."
else
    mkdir -p "${DOWNLOAD_DIR}"
    echo "    Resumable download: ${MODEL_ARCHIVE}"
    # Le .part reste volontairement dans le cache si le réseau tombe. Une
    # relance reprend au dernier octet reçu au lieu de recommencer les 40 Mo.
    curl -L --fail --retry 5 --retry-delay 3 --continue-at - \
        -o "${MODEL_ARCHIVE}" "${MODEL_URL}"
    unzip -tq "${MODEL_ARCHIVE}" >/dev/null
    unzip -q -o "${MODEL_ARCHIVE}" -d "${MODEL_DIR}"
    rm -f "${MODEL_ARCHIVE}"
    echo "    Installed ${MODEL_NAME}"
fi

echo
echo "✅ Wake word ready."
echo "   Calibrate it on your own voice:  python scripts/calibrate_wake_word.py"
