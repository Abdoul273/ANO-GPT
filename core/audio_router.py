"""
audio_router.py — Sélection intelligente du microphone pour ANO-GPT.

Politique : en l'absence de choix manuel dans le panneau Audio, ANO-GPT
suit la source d'entrée par défaut de PipeWire/PulseAudio. Le système peut
donc basculer vers un casque Bluetooth, un micro USB ou le micro interne et
ANO-GPT suit ce choix au prochain hot-plug, sans imposer sa propre préférence.

Un casque Bluetooth classique (non LE-Audio) ne peut être QUE dans un seul
profil à la fois : A2DP (sortie stéréo haute qualité, pas de micro) OU HFP/HSP
(micro + sortie mono dégradée dans les deux sens). Utiliser son micro implique
donc mécaniquement que la sortie bascule aussi sur lui, tant qu'il est utilisé
comme micro — c'est une limitation matérielle, pas un bug à contourner.

Important : ANO-GPT ne force aucun changement automatique de profil de carte
ni de sortie par défaut (plus de `set-card-profile` forcé). Un choix explicite
dans le panneau Audio peut toutefois fixer une sortie et déplacer les flux
ANO-GPT déjà ouverts. Pour le micro automatique, il se contente de
pointer `pactl set-default-source` vers le micro choisi ; la sortie audio
reste entièrement gérée par le système/PipeWire — ANO-GPT la suit, il ne la
pilote pas sans action humaine. C'est ce qui empêche la sortie de "sauter" sans cesse entre
haut-parleurs et casque : plus aucun code ici ne touche aux sinks/profils.

Piège déjà rencontré sur cette machine : la source virtuelle créée par
`module-echo-cancel` (voir enable_echo_cancel) apparaissait elle-même comme
un micro candidat au prochain rafraîchissement, se faisant élire "meilleur
micro" à la place du matériel réel — ce qui provoquait une boucle de
réouverture permanente du flux micro (et, par ricochet, des allers-retours
de profil Bluetooth). `list_input_devices()` exclut donc explicitement les
sources internes d'ANO-GPT (préfixe `anogpt_`) de la sélection.

sounddevice/PortAudio (hostapi "pulse"/"pipewire" sous Linux) ne permet pas
de choisir une source PulseAudio précise par index de périphérique — il
suit toujours la source par défaut du serveur son. La stratégie retenue est
donc : `pactl set-default-source <source>` pour orienter PipeWire, puis
ouvrir sd.InputStream sur le device par défaut (aucun `device=` explicite) ;
PortAudio relit la source par défaut à chaque *ouverture* de flux (pas en
continu), d'où la nécessité de fermer/rouvrir le flux à chaque changement.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from core import action_kit as kit

_PACTL = "pactl"


# ═══════════════════════════════════════════════════════════════════════════
# Modèles
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InputDevice:
    name: str                      # nom PulseAudio/PipeWire (node.name)
    description: str
    card_index: Optional[int]
    sample_rate: int
    kind: str                      # "bluetooth" | "usb" | "internal" | "other"
    bt_quality: str = "n/a"        # "wideband" | "narrowband" | "n/a"
    state: str = ""


@dataclass
class ChosenDevice:
    device: Optional[InputDevice]
    reason: str


@dataclass
class OutputDevice:
    name: str
    description: str
    kind: str
    state: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Bas niveau : pactl
# ═══════════════════════════════════════════════════════════════════════════

def _pactl_json(*args: str, timeout: float = 4.0) -> Optional[list]:
    try:
        r = kit.run([_PACTL, "-f", "json", *args], timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def _pactl(*args: str, timeout: float = 4.0) -> bool:
    try:
        r = kit.run([_PACTL, *args], timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Cartes Bluetooth : mSBC/LC3 (large bande) vs CVSD (mains-libres 8/16 kHz)
# ═══════════════════════════════════════════════════════════════════════════

_WIDEBAND_PROFILE_HINTS = ("msbc", "lc3", "bap", "swb")   # sous-chaînes suffisent
_NARROWBAND_PROFILE_HINTS = ("headset-head-unit", "hfp", "hsp")


def _bt_mic_quality(card: dict) -> str:
    """'wideband' si un profil casque large bande existe, 'narrowband' si
    seul le HFP/HSP classique (CVSD, 8 kHz) est disponible, 'n/a' si la
    carte n'a aucun profil avec micro."""
    profiles = card.get("profiles", {}) or {}
    has_narrowband = False
    for name in profiles:
        low = name.lower()
        is_wideband_hint = any(h in low for h in _WIDEBAND_PROFILE_HINTS)
        if is_wideband_hint and ("input" in low or low.startswith(("headset", "handsfree", "hfp"))):
            return "wideband"
        if any(h in low for h in _NARROWBAND_PROFILE_HINTS):
            has_narrowband = True
    return "narrowband" if has_narrowband else "n/a"


def list_cards() -> List[dict]:
    return _pactl_json("list", "cards") or []


def find_card(index: int, cards: Optional[List[dict]] = None) -> Optional[dict]:
    cards = cards if cards is not None else list_cards()
    for c in cards:
        if c.get("index") == index:
            return c
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Sources (micros)
# ═══════════════════════════════════════════════════════════════════════════

def _classify_bus(props: dict) -> str:
    bus = (props.get("device.bus") or "").lower()
    form = (props.get("device.form_factor") or "").lower()
    if bus == "bluetooth" or "bluez" in (props.get("device.api") or "").lower():
        return "bluetooth"
    if bus == "usb" or form in ("headset", "headphone", "microphone") and bus == "usb":
        return "usb"
    if bus in ("pci", "isa") or form == "internal":
        return "internal"
    return "other"


def list_input_devices() -> List[InputDevice]:
    """Sources micro utilisables (les moniteurs de sortie sont exclus : ce
    ne sont pas des micros, juste un renvoi de ce qui est en train de jouer).

    EasyEffects expose aussi ``easyeffects_source``. C'est la sortie traitée
    vers laquelle PipeWire redirige les consommateurs du micro matériel. Elle
    ne doit toutefois pas devenir la source *par défaut* de PulseAudio : le
    pont PortAudio ``pulse`` de sounddevice peut alors ouvrir un flux muet.
    ANO-GPT sélectionne donc le micro matériel ; EasyEffects applique toujours
    son traitement lors du raccordement du flux.
    """
    cards = list_cards()
    raw = _pactl_json("list", "sources") or []
    out: List[InputDevice] = []
    for s in raw:
        props = s.get("properties", {}) or {}
        name = s.get("name", "")
        if not name or name.endswith(".monitor") or props.get("device.class") == "monitor":
            continue
        if name == "easyeffects_source":
            continue
        if name.startswith("anogpt_"):
            # Nos propres sources virtuelles (echo-cancel...) ne sont pas
            # des micros candidats : les inclure les fait s'élire "meilleur
            # micro" au tour suivant et boucler indéfiniment (voir docstring
            # du module).
            continue
        kind = _classify_bus(props)
        card_index = None
        try:
            card_index = int(props.get("device.id")) if props.get("device.id") is not None else None
        except (TypeError, ValueError):
            card_index = None
        bt_quality = "n/a"
        if kind == "bluetooth" and card_index is not None:
            card = find_card(card_index, cards)
            if card:
                bt_quality = _bt_mic_quality(card)
        rate = 48000
        spec = s.get("sample_specification", "")
        try:
            for tok in spec.split():
                if tok.endswith("Hz"):
                    rate = int(tok[:-2])
        except Exception:
            pass
        desc = s.get("description") or ""
        if not desc or desc == "(null)":
            # pactl -f json sérialise l'absence de description PulseAudio
            # par la chaîne littérale "(null)" — jamais un vrai JSON null.
            desc = props.get("node.nick") or props.get("alsa.card_name") or name
        out.append(InputDevice(
            name=name,
            description=desc,
            card_index=card_index,
            sample_rate=rate,
            kind=kind,
            bt_quality=bt_quality,
            state=s.get("state", ""),
        ))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Politique de choix
# ═══════════════════════════════════════════════════════════════════════════

def _rank(dev: InputDevice) -> int:
    # Un casque Bluetooth n'est prioritaire que s'il expose un micro à bande
    # large (mSBC). En mains-libres classique (HFP/CVSD), il retombe à 8 kHz
    # étouffé : c'est le PIRE micro disponible, il passe donc en dernier
    # recours — après le micro interne — au lieu d'être choisi d'office.
    if dev.kind == "bluetooth":
        return 0 if dev.bt_quality == "wideband" else 4
    if dev.kind == "usb":
        return 1
    if dev.kind == "other":
        return 2
    if dev.kind == "internal":
        return 3
    return 5


_SELECTION_PATH = Path(__file__).resolve().parent.parent / "config" / "audio_devices.json"


def _load_selections() -> dict:
    try:
        value = json.loads(_SELECTION_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_selections(**updates) -> None:
    data = _load_selections()
    data.update(updates)
    try:
        _SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _SELECTION_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(_SELECTION_PATH)
    except Exception:
        pass


_manual_override: Optional[str] = _load_selections().get("input") or None
_output_override: Optional[str] = _load_selections().get("output") or None


def set_manual_override(name: Optional[str]) -> None:
    """Fige le choix sur une source précise (nom pactl), ou None pour
    revenir à la politique automatique. Utilisé par le menu déroulant du
    panneau Audio — sans quoi le prochain hot-plug écraserait le choix
    manuel de l'utilisateur."""
    global _manual_override
    _manual_override = name
    _save_selections(input=name or "")


def get_manual_override() -> Optional[str]:
    return _manual_override


def _system_default_source_name() -> Optional[str]:
    """Nom de la source que l'utilisateur a choisie dans le système sonore."""
    try:
        result = kit.run([_PACTL, "get-default-source"], timeout=4.0)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    # Une source ANO virtuelle ne représente jamais un choix matériel. Elle
    # peut brièvement rester la valeur par défaut pendant son nettoyage.
    return name if name and not name.startswith("anogpt_") else None


def default_source_warning() -> Optional[str]:
    """Détecte un routage Pulse dangereux sans le masquer silencieusement.

    Le routeur le corrige au démarrage, mais le journal doit expliquer pourquoi
    l'application a dû le faire. En particulier, PortAudio/pulse peut capturer
    du silence lorsque ``easyeffects_source`` est la source Pulse par défaut.
    """
    name = _system_default_source_name()
    if name == "easyeffects_source":
        return (
            "source Pulse par défaut = easyeffects_source ; correction vers le "
            "micro matériel (EasyEffects restera appliqué au flux)."
        )
    if name and name.startswith("anogpt_"):
        return "source Pulse par défaut virtuelle ANO-GPT ; correction vers un micro matériel."
    return None


def list_output_devices() -> List[OutputDevice]:
    """Sorties PipeWire/Pulse réelles, sans moniteurs ni sinks internes ANO."""
    result: List[OutputDevice] = []
    for sink in _pactl_json("list", "sinks") or []:
        props = sink.get("properties", {}) or {}
        name = str(sink.get("name") or "")
        if not name or name.startswith("anogpt_"):
            continue
        description = str(sink.get("description") or props.get("node.nick") or name)
        result.append(OutputDevice(
            name=name, description=description,
            kind=_classify_bus(props), state=str(sink.get("state") or ""),
        ))
    return result


def get_output_override() -> Optional[str]:
    return _output_override


def set_output_override(name: Optional[str]) -> bool:
    """Change la sortie et déplace aussi les flux déjà ouverts vers elle."""
    global _output_override
    selected = name or ""
    if selected and selected not in {device.name for device in list_output_devices()}:
        return False
    _output_override = selected or None
    _save_selections(output=selected)
    if not selected:
        return True
    if not _pactl("set-default-sink", selected):
        return False
    for stream in _pactl_json("list", "sink-inputs") or []:
        index = stream.get("index")
        if index is not None:
            _pactl("move-sink-input", str(index), selected)
    return True


def choose_best_device(devices: Optional[List[InputDevice]] = None) -> ChosenDevice:
    devices = devices if devices is not None else list_input_devices()
    if not devices:
        return ChosenDevice(device=None, reason="Aucune source audio détectée.")

    if _manual_override:
        for d in devices:
            if d.name == _manual_override:
                return ChosenDevice(device=d, reason=f"Micro choisi manuellement : {d.description}.")
        # L'appareil choisi manuellement a disparu (débranché) : repli sur
        # la politique automatique plutôt que de rester bloqué sans micro.

    best = min(devices, key=_rank)

    if best.kind == "bluetooth":
        if best.bt_quality == "wideband":
            reason = f"Casque/écouteurs Bluetooth {best.description}."
        else:
            # Seul micro restant : on l'utilise, mais en le disant clairement —
            # en mains-libres la reconnaissance vocale se dégrade nettement.
            reason = (f"Casque Bluetooth {best.description} en mains-libres "
                      f"(qualité réduite) — aucun autre micro disponible.")
    elif best.kind == "usb":
        reason = f"Micro USB/externe {best.description}."
    else:
        reason = f"Micro interne {best.description}."
    return ChosenDevice(device=best, reason=reason)


def choose_system_device(devices: Optional[List[InputDevice]] = None) -> ChosenDevice:
    """Privilégie le micro par défaut du système, sinon un repli sain.

    Un choix explicite dans ANO-GPT reste prioritaire. Hors de ce cas,
    l'utilisateur garde entièrement la main dans les réglages son de son
    bureau : par exemple, choisir le micro d'AirPods dans PipeWire suffit.
    """
    devices = devices if devices is not None else list_input_devices()
    if _manual_override:
        return choose_best_device(devices)
    system_name = _system_default_source_name()
    if system_name:
        for device in devices:
            if device.name == system_name:
                return ChosenDevice(
                    device=device,
                    reason=f"Micro par défaut du système : {device.description}.",
                )
    return choose_best_device(devices)


def _ensure_bluetooth_mic_profile(devices: Optional[List[InputDevice]] = None) -> bool:
    """Un casque Bluetooth classique n'expose une source micro que dans un
    profil mains-libres (HFP/HSP) — jamais en A2DP (sortie seule, haute
    qualité). S'il est connecté mais qu'aucune source micro n'apparaît
    encore pour lui, on bascule *une seule fois* vers un profil qui en
    expose une. Idempotent : ne fait rien si un micro Bluetooth est déjà
    visible pour cette carte — ne ré-applique jamais rien en boucle, c'est
    justement ce qui causait les allers-retours incessants de sortie."""
    devices = devices if devices is not None else list_input_devices()
    has_mic_for_card = {d.card_index for d in devices if d.kind == "bluetooth" and d.card_index is not None}
    changed = False
    for card in list_cards():
        props = card.get("properties", {}) or {}
        bus = (props.get("device.bus") or "").lower()
        api = (props.get("device.api") or "").lower()
        if bus != "bluetooth" and "bluez" not in api:
            continue
        idx = card.get("index")
        if idx in has_mic_for_card:
            continue
        profiles = card.get("profiles", {}) or {}
        candidate = None
        for name in profiles:
            low = name.lower()
            if any(h in low for h in _WIDEBAND_PROFILE_HINTS) and ("input" in low or low.startswith(("headset", "handsfree", "hfp"))):
                candidate = name
                break
        if not candidate:
            for name in profiles:
                if any(h in name.lower() for h in _NARROWBAND_PROFILE_HINTS):
                    candidate = name
                    break
        if candidate and card.get("active_profile") != candidate:
            if _pactl("set-card-profile", str(idx), candidate):
                changed = True
    return changed


# ═══════════════════════════════════════════════════════════════════════════
# Application du choix
# ═══════════════════════════════════════════════════════════════════════════

def apply_choice(chosen: ChosenDevice) -> bool:
    if not chosen.device:
        return False
    return _pactl("set-default-source", chosen.device.name)


def set_default_source(name: str) -> bool:
    return _pactl("set-default-source", name)


def portaudio_input_device(sounddevice) -> Optional[str]:
    """Nom du pont PortAudio qui respecte la source PipeWire sélectionnée.

    Sur cette machine, ``sounddevice`` choisit par défaut le pseudo-périphérique
    ALSA ``default`` (128 canaux), tandis que notre routeur change la source
    Pulse/PipeWire. Ouvrir explicitement ``pulse`` garantit que ce changement
    est réellement suivi. Le repli ``pipewire`` couvre les distributions sans
    plugin Pulse, puis ``None`` laisse PortAudio décider ailleurs.
    """
    try:
        devices = sounddevice.query_devices()
    except Exception:
        return None
    names = {
        str(device.get("name") or "").strip().casefold(): device
        for device in devices
        if int(device.get("max_input_channels") or 0) > 0
    }
    for candidate in ("pulse", "pipewire"):
        if candidate in names:
            return candidate
    return None


def probe_choice() -> ChosenDevice:
    """Retourne le micro que choisirait la politique, sans rien modifier.

    Le watcher l'utilise avant ``refresh_and_apply`` afin qu'un évènement créé
    par notre propre source AEC ne décharge pas cette source inutilement.
    """
    return choose_system_device(list_input_devices())


def refresh_and_apply() -> ChosenDevice:
    """Ré-énumère, choisit, applique (source par défaut uniquement — plus
    aucun profil ni sortie n'est forcé ici). Point d'entrée principal, à
    rappeler à chaque hot-plug.

    Toujours purger les sources virtuelles AEC orphelines *avant* de
    choisir : un `anogpt_mic_aec` auto-câblé (source_master = lui-même)
    reste le défaut Pulse après un crash et livre du silence parfait —
    le micro matériel marche, l'assistant n'entend rien.
    """
    unload_all_echo_cancel()
    devices = list_input_devices()
    # Ne jamais forcer un profil HFP/HSP ici. Sur Bluetooth classique, faire
    # apparaître le micro du casque détruit en même temps la sortie A2DP stéréo
    # et donne une voix mono, étouffée ou grésillante. Un micro Bluetooth déjà
    # exposé reste sélectionnable ; sinon on conserve le profil audio choisi
    # par l'utilisateur et on utilise un micro USB/interne.
    chosen = choose_system_device(devices)
    if chosen.device:
        apply_choice(chosen)
        prepare_source(chosen.device.name)
    return chosen


# ═══════════════════════════════════════════════════════════════════════════
# Annulation d'écho (module-echo-cancel) — opt-in uniquement
# ═══════════════════════════════════════════════════════════════════════════
#
# Sur PipeWire, charger ce module *par défaut* a rendu le micro sourd :
#   1. disable_echo_cancel() ne connaissait que l'id du process courant,
#      donc chaque relance empilait un nouveau module du même nom ;
#   2. un second load avec source_name=anogpt_mic_aec déjà pris se câblait
#      en source_master=anogpt_mic_aec (boucle) → RMS exactement 0 ;
#   3. pactl set-default-source anogpt_mic_aec faisait écouter ce silence.
# Le micro matériel, lui, livrait un signal parfaitement utilisable.
# On ne l'active donc plus automatiquement. L'API reste pour un opt-in
# futur, avec les gardes qui manquaient.

_echo_module_id: Optional[str] = None
_echo_source_name: Optional[str] = None

_AEC_SOURCE = "anogpt_mic_aec"


def _is_virtual_anogpt_source(name: Optional[str]) -> bool:
    if not name:
        return False
    low = name.lower()
    return low.startswith("anogpt_") or "echo-cancel" in low


def _list_echo_cancel_module_ids() -> List[str]:
    """Ids pactl de tous les module-echo-cancel, y compris les orphelins
    laissés par un crash. `pactl -f json list modules` n'a pas d'index
    sous PipeWire — on parse le format short."""
    try:
        r = kit.run([_PACTL, "list", "modules", "short"], timeout=4)
        if r.returncode != 0 or not r.stdout:
            return []
        ids: List[str] = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            mod_id, _name = parts[0], parts[1]
            rest = "\t".join(parts[2:]) if len(parts) > 2 else ""
            # Ne jamais décharger les modules d'annulation d'écho appartenant
            # au système ou à une autre application. Seuls ceux nommés par
            # ANO-GPT sont des orphelins que nous pouvons nettoyer sans faire
            # sauter le routage audio global.
            if _AEC_SOURCE in rest or "anogpt_speaker_aec" in rest:
                ids.append(mod_id)
        return ids
    except Exception:
        return []


def unload_all_echo_cancel() -> int:
    """Décharge *tous* les module-echo-cancel, pas seulement celui de
    cette instance. Indispensable au démarrage : les zombies survivent
    au process et restent la source Pulse par défaut."""
    global _echo_module_id, _echo_source_name
    n = 0
    for mod_id in _list_echo_cancel_module_ids():
        if _pactl("unload-module", mod_id):
            n += 1
    _echo_module_id = None
    _echo_source_name = None
    return n


def prepare_source(name: str) -> None:
    """S'assure que la source choisie est audible sans être saturée.

    Certains pilotes ACP annoncent un ``base_volume`` très bas (10 % sur le
    micro interne de cette machine), mais PipeWire peut conserver la capture à
    100 %. Le résultat est un PCM écrêté presque en permanence : le VAD neuronal
    le classe alors comme du bruit et ANO semble ne rien entendre. On ne touche
    au gain que dans ce cas pathologique et mesurable (base <= 20 %, volume
    >= 80 %). Les micros au gain normal, ainsi que les réglages volontairement
    bas, restent inchangés.
    """
    if not name or _is_virtual_anogpt_source(name):
        return
    _pactl("set-source-mute", name, "0")
    sources = _pactl_json("list", "sources") or []
    source = next((item for item in sources if item.get("name") == name), None)
    if not source:
        return
    try:
        base = int((source.get("base_volume") or {}).get("value") or 0)
        channels = list((source.get("volume") or {}).values())
        current = max(int(channel.get("value") or 0) for channel in channels)
    except (TypeError, ValueError):
        return
    base_percent = base * 100.0 / 65536.0
    current_percent = current * 100.0 / 65536.0
    if 0.0 < base_percent <= 20.0 and current_percent >= 80.0:
        safe_percent = max(30, min(60, round(base_percent * 3)))
        _pactl("set-source-volume", name, f"{safe_percent}%")


def enable_echo_cancel(master_source: str, master_sink: Optional[str] = None) -> Optional[str]:
    """Charge module-echo-cancel en lisant `master_source` (le micro brut
    choisi par la politique) et renvoie le nom de la source virtuelle
    nettoyée à utiliser à la place (ou None si l'appel a échoué —
    l'appelant doit alors continuer avec la source brute).

    Piège pactl : `source_name=` nomme la NOUVELLE source virtuelle créée,
    `source_master=` désigne la source brute à lire. Les confondre crée une
    source dupliquée qui porte le même nom que le micro d'origine sans
    jamais réellement lire dessus.

    Refuse tout master virtuel ANO-GPT : c'est exactement le câblage
    qui produisait un micro sourd (RMS = 0)."""
    global _echo_module_id, _echo_source_name
    if not master_source or _is_virtual_anogpt_source(master_source):
        return None
    if master_source.endswith(".monitor"):
        return None
    unload_all_echo_cancel()
    virtual_source = _AEC_SOURCE
    args = [
        f"source_master={master_source}",
        f"source_name={virtual_source}",
        "source_properties=device.description=ANO-GPT-AEC",
        "aec_method=webrtc",
        "aec_args=\"analog_gain_control=0 digital_gain_control=1\"",
    ]
    if master_sink:
        args.insert(1, f"sink_master={master_sink}")
        args.insert(2, "sink_name=anogpt_speaker_aec")
    try:
        r = kit.run([_PACTL, "load-module", "module-echo-cancel", *args], timeout=5)
        mod_id = (r.stdout or "").strip()
        if r.returncode == 0 and mod_id.isdigit():
            _echo_module_id = mod_id
            _echo_source_name = virtual_source
            _pactl("set-source-mute", virtual_source, "0")
            return virtual_source
    except Exception:
        pass
    return None


def disable_echo_cancel() -> None:
    unload_all_echo_cancel()


# ═══════════════════════════════════════════════════════════════════════════
# Hot-plug : pactl subscribe
# ═══════════════════════════════════════════════════════════════════════════

class HotplugWatcher:
    """Surveille les changements de cartes/sources et prévient l'appelant
    (avec un anti-rebond) sans jamais faire planter l'app si pactl manque."""

    _RELEVANT = ("card", "source")

    def __init__(self, on_change: Callable[[], None], debounce_s: float = 2.5):
        self._on_change = on_change
        self._debounce_s = debounce_s
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="audio-hotplug")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    [_PACTL, "subscribe"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
            except Exception as e:
                print(f"[AudioRouter] pactl subscribe indisponible ({e}) — pas de hot-plug.")
                return
            last_fire = 0.0
            try:
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    if not any(f"on {kind}" in line for kind in self._RELEVANT):
                        continue
                    now = time.monotonic()
                    if now - last_fire < self._debounce_s:
                        continue
                    last_fire = now
                    try:
                        self._on_change()
                    except Exception as e:
                        print(f"[AudioRouter] Erreur callback hot-plug : {e}")
            except Exception:
                pass
            finally:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            if not self._stop.is_set():
                time.sleep(2.0)  # pactl a planté/s'est fermé : on retente


def describe(chosen: ChosenDevice) -> str:
    if not chosen.device:
        return "Micro : aucune source détectée."
    d = chosen.device
    return f"Micro : {d.description} ({d.sample_rate} Hz, {d.kind}) — {chosen.reason}"


if __name__ == "__main__":
    for dev in list_input_devices():
        print(dev)
    print()
    result = refresh_and_apply()
    print(describe(result))
