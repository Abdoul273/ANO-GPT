#!/usr/bin/env bash
# Installe l'unité systemd --user d'ANO-GPT.
#
# Rien n'est démarré par ce script : il pose l'unité, l'active, et laisse
# l'utilisateur lancer lui-même. Un service graphique qu'on démarre à l'aveugle
# depuis un terminal échoue de façon déroutante.
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/anogpt.service"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd absent : rien à installer." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
# %h ne pointe vers ce dépôt que s'il est bien dans ~/OUTILS/ANO-GPT ; sinon on
# réécrit les chemins pour l'emplacement réel.
sed "s|%h/OUTILS/ANO-GPT|$SOURCE|g" "$SOURCE/config/systemd/anogpt.service" > "$UNIT"
echo "Unité écrite : $UNIT"

systemctl --user daemon-reload
systemctl --user enable anogpt.service
echo "Unité activée (démarrage avec la session graphique)."

cat <<'EOF'

Hyprland n'exporte pas tout seul son environnement à systemd. Sans cette ligne
dans ~/.config/hypr/hyprland.conf, le service démarrera sans écran :

    exec-once = systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE

Commandes utiles :
    systemctl --user start anogpt        # démarrer maintenant
    systemctl --user status anogpt       # état et derniers redémarrages
    journalctl --user -u anogpt -f       # sortie brute en direct
    systemctl --user disable --now anogpt

Le journal structuré d'ANO-GPT reste à part, dans logs/anogpt.jsonl.
EOF
