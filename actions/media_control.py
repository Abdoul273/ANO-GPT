"""
🎬 MEDIA CONTROL — Contrôleur multimédia robuste pour Hyprland/Wayland.
Contrôle YouTube, Spotify, VLC et tout lecteur via la meilleure stratégie :
  1. MPRIS/playerctl (fonctionne même si le lecteur n'est pas focalisé) ;
  2. hyprctl pour focaliser la fenêtre navigateur si besoin ;
  3. wtype (Wayland) ou xdotool (X11) pour les raccourcis clavier.

Corrections par rapport à l'ancienne version :
    - xdotool est X11-only et ne contrôle pas les fenêtres Wayland :
      tout le routage clavier passe désormais par wtype sous Hyprland,
      et MPRIS est privilégié chaque fois que possible ;
    - les commandes étaient construites en f-string avec shell=True
      (injection possible) : remplacées par des listes d'arguments ;
    - le volume YouTube était réglé par des appuis de flèches à l'aveugle :
      playerctl volume d'abord, sinon chiffres 0-9 (0-90%) + flèches ;
    - les touches étaient envoyées à la fenêtre focalisée sans vérifier
      qu'il s'agissait d'un navigateur : vérification + focus automatique ;
    - le volume système utilisait uniquement pactl : wpctl (PipeWire)
      est privilégié et le volume réel est relu après réglage.

Ajouts : now_playing, contrôles génériques play/pause/next/previous/stop,
volume_up/down avec pas réglable, recherche navigateur via wtype.
"""
import os
import platform
import re
from core import action_kit as kit
import time
from typing import Optional, List, Tuple

_OS = platform.system()  # "Linux" | "Darwin" | "Windows"
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))

# Tokens de classes de fenêtres / noms MPRIS considérés comme "navigateur"
_CHROME_TOKENS = (
    "google-chrome-stable", "google-chrome", "chrome", "chromium",
)
_BROWSER_TOKENS = (
    "google-chrome-stable", "google-chrome", "chrome", "chromium",
    "firefox", "librewolf", "brave",
    "msedge", "microsoft-edge", "opera", "vivaldi", "zen-browser", "thorium",
)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _have(cmd: str) -> bool:
    return kit.have(cmd)


def _run_list(argv: List[str], timeout: float = 3.0) -> bool:
    """Exécute une commande en liste (pas de shell) et retourne le succès."""
    return kit.run(argv, timeout=timeout).ok


def _run_out(argv: List[str], timeout: float = 3.0) -> Tuple[bool, str]:
    r = kit.run(argv, timeout=timeout)
    return r.ok, r.out.strip()


def _hypr_env() -> dict:
    """Environnement avec XDG_RUNTIME_DIR et HYPRLAND_INSTANCE_SIGNATURE
    restaurés (indispensable quand l'assistant tourne hors session)."""
    env = {**os.environ}
    if not env.get("XDG_RUNTIME_DIR"):
        try:
            cand = f"/run/user/{os.getuid()}"
            if os.path.isdir(cand):
                env["XDG_RUNTIME_DIR"] = cand
        except Exception:
            pass
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            rd = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            hd = os.path.join(rd, "hypr")
            if os.path.isdir(hd):
                inst = sorted(
                    (os.path.join(hd, d) for d in os.listdir(hd)
                     if os.path.isdir(os.path.join(hd, d))),
                    key=lambda p: os.path.getmtime(p), reverse=True)
                if inst:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = os.path.basename(inst[0])
        except Exception:
            pass
    return env


def _hyprctl_json(*args: str):
    """Lecture Hyprland partagée (socle : délai, reprise, cache court).

    La copie locale relançait un processus par question ; le cache du socle
    fusionne les appels d'un même tour de parole entre toutes les actions.
    """
    return kit.hypr_json(*args, default=None)


# ════════════════════════════════════════════════════════════════════════════
# Détection / focus du navigateur
# ════════════════════════════════════════════════════════════════════════════

def _is_browser_class(cls: str) -> bool:
    c = (cls or "").lower()
    return any(t in c for t in _BROWSER_TOKENS)


def _active_window_class() -> str:
    win = _hyprctl_json("activewindow")
    if isinstance(win, dict):
        return (win.get("class") or "").lower()
    return ""


def _browser_is_active() -> bool:
    return _is_browser_class(_active_window_class())


def _focus_browser() -> bool:
    """Focalise la première fenêtre navigateur trouvée, avec priorité à Chrome/Chromium. True si réussie."""
    clients = _hyprctl_json("clients") or []
    # Passe 1 : priorité absolue aux fenêtres Google Chrome / Chromium
    for c in clients:
        cls = c.get("class") or c.get("initialClass") or ""
        addr = c.get("address")
        if addr and any(t in cls.lower() for t in _CHROME_TOKENS):
            if _focus_address(addr):
                return True

    # Passe 2 : autres navigateurs (ex. Firefox, Brave)
    for c in clients:
        cls = c.get("class") or c.get("initialClass") or ""
        addr = c.get("address")
        if _is_browser_class(cls) and addr:
            if _focus_address(addr):
                return True
    return False


def _focus_address(addr: str) -> bool:
    """Focus par adresse, puis relecture : hyprctl répond « ok » même sans effet."""
    if not kit.hypr("dispatch", "focuswindow", f"address:{addr}", timeout=2.0):
        return False
    return kit.wait_until(
        lambda: kit.hypr_activewindow().get("address") == addr,
        timeout=0.6, interval=0.05)


def _ensure_browser() -> Optional[str]:
    """Garantit qu'un navigateur a le focus avant d'envoyer des touches.
    Retourne None si OK, sinon un message d'erreur à renvoyer à l'utilisateur."""
    if _browser_is_active():
        return None
    if _focus_browser():
        return None
    return "❓ Aucun navigateur ouvert — impossible d'envoyer la commande."


# ════════════════════════════════════════════════════════════════════════════
# Clavier : wtype (Wayland) puis xdotool (X11)
# ════════════════════════════════════════════════════════════════════════════

def _press(key: str) -> bool:
    if _WAYLAND and _have("wtype"):
        return _run_list(["wtype", "-k", key])
    if _have("xdotool"):
        return _run_list(["xdotool", "key", key])
    return False


def _press_n(key: str, n: int) -> bool:
    ok = True
    for _ in range(max(0, int(n))):
        ok = _press(key) and ok
        time.sleep(0.05)
    return ok


def _hotkey(*keys: str) -> bool:
    keys = [k.lower() for k in keys if k]
    if not keys:
        return False
    if _WAYLAND and _have("wtype"):
        mods = [k for k in keys if k in ("ctrl", "shift", "alt", "super", "meta")]
        nonmods = [k for k in keys if k not in mods]
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", "ctrl" if m in ("ctrl", "meta") else m]
        for k in nonmods or ["space"]:
            cmd += ["-k", k]
        for m in reversed(mods):
            cmd += ["-m", "ctrl" if m in ("ctrl", "meta") else m]
        return _run_list(cmd)
    if _have("xdotool"):
        return _run_list(["xdotool", "key", "+".join(keys)])
    return False


def _type_text(text: str) -> bool:
    if _WAYLAND and _have("wtype"):
        return _run_list(["wtype", text], timeout=max(2, len(text) // 20 + 2))
    if _have("xdotool"):
        return _run_list(["xdotool", "type", "--clearmodifiers", text])
    return False


# ════════════════════════════════════════════════════════════════════════════
# MPRIS / playerctl (la voie royale : marche sans focus)
# ════════════════════════════════════════════════════════════════════════════

def _playerctl_players() -> List[str]:
    ok, out = _run_out(["playerctl", "-l"])
    if not ok:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def _browser_mpris() -> Optional[str]:
    """Nom du lecteur MPRIS correspondant à un navigateur (pour YouTube), priorité Chrome/Chromium."""
    players = _playerctl_players()
    # Priorité aux lecteurs Chrome / Chromium
    for p in players:
        p_lower = p.lower()
        if any(t in p_lower for t in _CHROME_TOKENS):
            return p
    # Repli sur les autres navigateurs
    for p in players:
        p_lower = p.lower()
        if any(t in p_lower for t in _BROWSER_TOKENS):
            return p
    return None


# ════════════════════════════════════════════════════════════════════════════
# 🎵 YouTube / navigateur : lecture
# ════════════════════════════════════════════════════════════════════════════

def pause_youtube() -> str:
    bp = _browser_mpris()
    if bp and _run_list(["playerctl", "--player", bp, "pause"]):
        return "✋ Vidéo mise en pause"
    err = _ensure_browser()
    if err:
        return err
    if not _press("space"):
        return "⚠️ Outil clavier indisponible (installez wtype)."
    return "✋ Vidéo mise en pause"


def play_youtube() -> str:
    bp = _browser_mpris()
    if bp and _run_list(["playerctl", "--player", bp, "play"]):
        return "▶️ Lecture lancée"
    err = _ensure_browser()
    if err:
        return err
    if not _press("space"):
        return "⚠️ Outil clavier indisponible (installez wtype)."
    return "▶️ Lecture lancée"


def youtube_volume(level: int) -> str:
    level = max(0, min(100, int(level)))
    bp = _browser_mpris()
    if bp and _run_list(["playerctl", "--player", bp, "volume", f"{level / 100:.2f}"]):
        return f"🔊 Volume YouTube : {level}%"
    err = _ensure_browser()
    if err:
        return err
    # Astuce clavier YouTube : les chiffres 0-9 règlent 0-90 %,
    # puis les flèches ajustent par pas de 5 %.
    base = min(9, level // 10)
    if not _press(str(base)):
        return "⚠️ Outil clavier indisponible (installez wtype)."
    diff = level - base * 10
    steps = round(diff / 5)
    if steps > 0:
        _press_n("Up", steps)
    elif steps < 0:
        _press_n("Down", -steps)
    return f"🔊 Volume YouTube : ~{level}%"


def youtube_seek(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds == 0:
        return "⏱️ Aucun décalage demandé."
    err = _ensure_browser()
    if err:
        return err
    key10 = "l" if seconds > 0 else "j"
    key5 = "Right" if seconds > 0 else "Left"
    if not _press_n(key10, abs(seconds) // 10):
        return "⚠️ Outil clavier indisponible (installez wtype)."
    _press_n(key5, (abs(seconds) % 10) // 5)
    return f"⏩ Décalage de {seconds}s"


def _yt_key(key: str, ok_msg: str) -> str:
    err = _ensure_browser()
    if err:
        return err
    if not _press(key):
        return "⚠️ Outil clavier indisponible (installez wtype)."
    return ok_msg


def youtube_fullscreen() -> str:
    return _yt_key("f", "📺 Mode plein écran")


def youtube_subtitles() -> str:
    return _yt_key("c", "📝 Sous-titres activés/désactivés")


def youtube_theater_mode() -> str:
    return _yt_key("t", "🎭 Mode cinéma activé")


def youtube_next() -> str:
    err = _ensure_browser()
    if err:
        return err
    return "⏭️ Vidéo suivante" if _hotkey("shift", "n") else "⚠️ Commande clavier indisponible."


def youtube_previous() -> str:
    err = _ensure_browser()
    if err:
        return err
    return "⏮️ Vidéo précédente" if _hotkey("shift", "p") else "⚠️ Commande clavier indisponible."


def youtube_mute() -> str:
    return _yt_key("m", "🔇 Son YouTube activé/désactivé")


def youtube_miniplayer() -> str:
    return _yt_key("i", "🖥️ Mini-lecteur activé/désactivé")


def youtube_restart() -> str:
    return _yt_key("0", "⏮️ Vidéo reprise depuis le début")


def youtube_toggle() -> str:
    return _yt_key("space", "⏯️ Lecture/pause basculée")


def youtube_back_10s() -> str:
    return _yt_key("j", "⏪ Reculé de 10s")


def youtube_forward_10s() -> str:
    return _yt_key("l", "⏩ Avancé de 10s")


def youtube_speed(speed: float) -> str:
    speed = float(speed or 1.0)
    err = _ensure_browser()
    if err:
        return err
    if speed > 1.0:
        n, combo_key = int(round((speed - 1.0) * 4)), "period"   # '>'
    elif speed < 1.0:
        n, combo_key = int(round((1.0 - speed) * 4)), "comma"     # '<'
    else:
        return "⚡ Vitesse normale (1x)."
    for _ in range(max(1, n)):
        if _WAYLAND and _have("wtype"):
            _run_list(["wtype", "-M", "shift", "-k", combo_key, "-m", "shift"])
        elif _have("xdotool"):
            _run_list(["xdotool", "key", f"shift+{combo_key}"])
        time.sleep(0.08)
    return f"⚡ Vitesse : {speed}x"


# ════════════════════════════════════════════════════════════════════════════
# 🔊 Volume système (wpctl / pactl)
# ════════════════════════════════════════════════════════════════════════════

def _volume_backend() -> Optional[str]:
    if _have("wpctl"):
        return "wpctl"
    if _have("pactl"):
        return "pactl"
    return None


def get_current_volume() -> Optional[int]:
    backend = _volume_backend()
    if backend == "wpctl":
        ok, out = _run_out(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        m = re.search(r"Volume:\s*([0-9.]+)", out)
        if ok and m:
            return round(float(m.group(1)) * 100)
    elif backend == "pactl":
        ok, out = _run_out(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        m = re.search(r"(\d+)%", out)
        if ok and m:
            return int(m.group(1))
    return None


def system_volume(level: int) -> str:
    level = max(0, min(100, int(level)))
    backend = _volume_backend()
    if not backend:
        return "❌ Aucun backend audio (installez wireplumber ou pulseaudio-utils)."
    if backend == "wpctl":
        _run_list(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])
    else:
        _run_list(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    cur = get_current_volume()
    return f"🔊 Volume système : {cur if cur is not None else level}%"


def system_volume_up(amount: int = 5) -> str:
    amount = max(1, min(50, int(amount or 5)))
    cur = get_current_volume()
    if cur is not None:
        return system_volume(cur + amount)
    backend = _volume_backend()
    if backend == "wpctl":
        _run_list(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{amount}%+"])
    elif backend == "pactl":
        _run_list(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{amount}%"])
    else:
        return "❌ Aucun backend audio."
    return f"🔊 Volume ↑ (+{amount}%)"


def system_volume_down(amount: int = 5) -> str:
    amount = max(1, min(50, int(amount or 5)))
    cur = get_current_volume()
    if cur is not None:
        return system_volume(cur - amount)
    backend = _volume_backend()
    if backend == "wpctl":
        _run_list(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{amount}%-"])
    elif backend == "pactl":
        _run_list(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{amount}%"])
    else:
        return "❌ Aucun backend audio."
    return f"🔊 Volume ↓ (-{amount}%)"


def system_mute() -> str:
    backend = _volume_backend()
    if backend == "wpctl":
        _run_list(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
    elif backend == "pactl":
        _run_list(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
    else:
        return "❌ Aucun backend audio."
    return "🔇 Muet activé/désactivé"


# ════════════════════════════════════════════════════════════════════════════
# 🎮 Contrôles navigateur (raccourcis via wtype/xdotool)
# ════════════════════════════════════════════════════════════════════════════

def chrome_new_tab() -> str:
    _hotkey("ctrl", "t")
    return "📑 Nouvel onglet"


def chrome_close_tab() -> str:
    _hotkey("ctrl", "w")
    return "❌ Onglet fermé"


def chrome_reload() -> str:
    _hotkey("ctrl", "r")
    return "🔄 Page rechargée"


def chrome_search(query: str) -> str:
    if not query:
        return "❓ Aucune recherche fournie."
    _hotkey("ctrl", "f")
    time.sleep(0.2)
    if not _type_text(query):
        return "⚠️ Saisie impossible (installez wtype)."
    time.sleep(0.2)
    _press("Return")
    return f"🔍 Recherche : {query}"


def chrome_zoom_in() -> str:
    _hotkey("ctrl", "plus")
    return "🔍 Zoom +"


def chrome_zoom_out() -> str:
    _hotkey("ctrl", "minus")
    return "🔍 Zoom -"


def chrome_zoom_reset() -> str:
    _hotkey("ctrl", "0")
    return "🔍 Zoom réinitialisé"


def chrome_back() -> str:
    _hotkey("alt", "Left")
    return "⬅️ Retour"


def chrome_forward() -> str:
    _hotkey("alt", "Right")
    return "➡️ Suivant"


# ════════════════════════════════════════════════════════════════════════════
# 🎶 Contrôles génériques MPRIS (tout lecteur : Spotify, VLC, navigateur…)
# ════════════════════════════════════════════════════════════════════════════

def _player_statuses() -> List[Tuple[str, str]]:
    """[(lecteur, Playing|Paused|Stopped)] pour tous les lecteurs MPRIS."""
    ok, out = _run_out(["playerctl", "-a", "metadata", "--format",
                        "{{playerName}}\t{{status}}"])
    if not ok:
        return []
    pairs = []
    for line in out.splitlines():
        name, _, status = line.partition("\t")
        if name.strip():
            pairs.append((name.strip(), status.strip() or "Stopped"))
    return pairs


def _active_player(prefer_playing: bool = True) -> Optional[str]:
    """Le lecteur qui joue vraiment — `playerctl` sans `--player` vise le
    premier de sa liste, souvent un onglet en pause pendant que Spotify joue."""
    statuses = _player_statuses()
    if not statuses:
        return None
    if prefer_playing:
        for name, status in statuses:
            if status == "Playing":
                return name
    for name, status in statuses:
        if status == "Paused":
            return name
    return statuses[0][0]


def _mpris(action: str) -> Optional[str]:
    if not _have("playerctl"):
        return None
    target = _active_player(prefer_playing=action != "play")
    argv = ["playerctl"] + (["--player", target] if target else []) + [action]
    ok = _run_list(argv)
    if not ok and target:
        ok = _run_list(["playerctl", action])
    return "ok" if ok else None


def media_play() -> str:
    if _mpris("play"):
        return "▶️ Lecture lancée"
    return "❌ Aucune lecture MPRIS en cours (installez playerctl si besoin)."


def media_pause() -> str:
    if _mpris("pause"):
        return "✋ Lecture en pause"
    return "❌ Aucune lecture MPRIS en cours."


def media_stop() -> str:
    if _mpris("stop"):
        return "⏹️ Lecture arrêtée"
    return "❌ Aucune lecture MPRIS en cours."


def media_next() -> str:
    if _mpris("next"):
        return "⏭️ Piste suivante"
    return "❌ Aucune lecture MPRIS en cours."


def media_previous() -> str:
    if _mpris("previous"):
        return "⏮️ Piste précédente"
    return "❌ Aucune lecture MPRIS en cours."


def now_playing() -> str:
    if not _have("playerctl"):
        return "❓ playerctl n'est pas installé."
    target = _active_player()
    argv = ["playerctl"] + (["--player", target] if target else [])
    ok, out = _run_out(argv + ["metadata", "--format",
                               "{{status}}\t{{artist}}\t{{title}}\t{{duration(position)}}\t{{duration(mpris:length)}}"])
    if ok and out.strip():
        status, artist, title, position, length = (out.strip().split("\t") + [""] * 5)[:5]
        icon = {"Playing": "🎵", "Paused": "⏸️"}.get(status, "⏹️")
        label = " — ".join(part for part in (artist, title) if part) or "titre inconnu"
        where = f" ({position}/{length})" if position and length else ""
        source = f" sur {target}" if target else ""
        state = {"Playing": "En cours", "Paused": "En pause"}.get(status, "Arrêté")
        return f"{icon} {state}{source} : {label}{where}"
    return "❓ Aucune lecture détectée."


# ════════════════════════════════════════════════════════════════════════════
# 🎮 ROUTAGE PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

@kit.action("media_control")
def media_control(parameters: dict, player=None, **kwargs) -> str:
    """
    Contrôleur multimédia robuste.
    Actions :
      - youtube_pause, youtube_play, youtube_toggle, youtube_mute, youtube_restart
      - youtube_miniplayer, youtube_volume(0-100), youtube_seek(s)
      - youtube_fullscreen, youtube_subtitles, youtube_theater
      - youtube_next, youtube_previous, youtube_back_10s, youtube_forward_10s
      - youtube_speed(0.5-2.0)
      - volume(0-100), volume_up, volume_down, mute
      - play, pause, stop, next, previous, now_playing (MPRIS générique)
      - chrome_new_tab, chrome_close_tab, chrome_reload, chrome_search(query)
      - chrome_zoom_in, chrome_zoom_out, chrome_zoom_reset,
        chrome_back, chrome_forward
    """
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    value = params.get("value")

    if player:
        try:
            player.write_log(f"[media] {action}")
        except Exception:
            pass

    # YouTube / navigateur
    if action == "youtube_pause":
        return pause_youtube()
    if action == "youtube_play":
        return play_youtube()
    if action == "youtube_toggle":
        return youtube_toggle()
    if action == "youtube_mute":
        return youtube_mute()
    if action == "youtube_miniplayer":
        return youtube_miniplayer()
    if action == "youtube_restart":
        return youtube_restart()
    if action == "youtube_volume":
        return youtube_volume(int(value) if value is not None else 50)
    if action == "youtube_seek":
        return youtube_seek(int(value) if value is not None else 0)
    if action == "youtube_fullscreen":
        return youtube_fullscreen()
    if action == "youtube_subtitles":
        return youtube_subtitles()
    if action == "youtube_theater":
        return youtube_theater_mode()
    if action == "youtube_next":
        return youtube_next()
    if action == "youtube_previous":
        return youtube_previous()
    if action == "youtube_back_10s":
        return youtube_back_10s()
    if action == "youtube_forward_10s":
        return youtube_forward_10s()
    if action == "youtube_speed":
        return youtube_speed(float(value) if value is not None else 1.0)

    # Volume système
    if action == "volume":
        return system_volume(int(value) if value is not None else 50)
    if action == "volume_up":
        return system_volume_up(int(value) if value is not None else 5)
    if action == "volume_down":
        return system_volume_down(int(value) if value is not None else 5)
    if action == "mute":
        return system_mute()

    # MPRIS générique
    if action == "play":
        return media_play()
    if action == "pause":
        return media_pause()
    if action == "stop":
        return media_stop()
    if action == "next":
        return media_next()
    if action == "previous":
        return media_previous()
    if action in ("now_playing", "status"):
        return now_playing()

    # Navigateur : onglets / navigation
    if action == "chrome_new_tab":
        return chrome_new_tab()
    if action == "chrome_close_tab":
        return chrome_close_tab()
    if action == "chrome_reload":
        return chrome_reload()
    if action == "chrome_search":
        return chrome_search(str(value or ""))
    if action == "chrome_zoom_in":
        return chrome_zoom_in()
    if action == "chrome_zoom_out":
        return chrome_zoom_out()
    if action == "chrome_zoom_reset":
        return chrome_zoom_reset()
    if action == "chrome_back":
        return chrome_back()
    if action == "chrome_forward":
        return chrome_forward()

    return f"❓ Action inconnue : {action}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(media_control({"action": sys.argv[1], "value": sys.argv[2]}))
    elif len(sys.argv) == 2:
        print(media_control({"action": sys.argv[1]}))
    else:
        print("Usage: python media_control.py <action> [valeur]")
