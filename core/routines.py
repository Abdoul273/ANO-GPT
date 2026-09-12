"""core/routines.py — Des enchaînements déclenchés par une phrase, sans modèle.

« Mode travail » ne demande aucune intelligence : la suite d'actions est
toujours la même. La faire passer par le modèle coûte un aller-retour réseau,
une génération, et parfois une interprétation créative de ce qui devrait être
mécanique. Ici, la phrase est reconnue localement et les étapes s'exécutent
dans l'ordre écrit — c'est instantané et c'est reproductible.

Les routines vivent dans `config/routines.yaml`, relu automatiquement à chaque
modification : l'utilisateur ajoute la sienne sans redémarrer l'assistant.

La reconnaissance est volontairement sévère. Une routine éteint des fenêtres,
verrouille la session, coupe le son : la déclencher par erreur coûte bien plus
cher que de ne pas la déclencher. La phrase entendue doit donc correspondre à
une phrase déclarée *en entier* — jamais comme fragment d'une phrase plus
longue. « Je pars » lance la routine ; « je pars à Dakar jeudi » ne la lance
pas, c'est une conversation.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
ROUTINES_PATH = BASE_DIR / "config" / "routines.yaml"

# En dessous, deux phrases différentes se ressemblent trop pour trancher.
FUZZY_THRESHOLD = 0.87

# Le mot d'activation et les formules de politesse précèdent souvent l'ordre :
# ils ne font pas partie de la phrase à reconnaître.
_PREFIXES = (
    "hey ano gpt", "ok ano gpt", "salut ano gpt", "dis ano gpt",
    "hey ano", "he ano", "ok ano", "salut ano", "dis ano", "ano gpt",
    "jarvis", "ano", "anno", "anneau",
)
_SUFFIXES = (
    "s il te plait", "s il vous plait", "stp", "merci", "maintenant",
    "tout de suite",
)

# Une commande shell d'étape ne doit jamais bloquer l'assistant : au-delà, on
# la laisse vivre sa vie et on passe à l'étape suivante.
SHELL_TIMEOUT_S = 15


def normalise(text: str) -> str:
    """Forme comparable : sans accents, sans ponctuation, espaces réduits."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def strip_wake(text: str) -> str:
    """Retire le mot d'activation et les formules qui encadrent l'ordre."""
    out = normalise(text)
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if out == prefix:
                return ""
            if out.startswith(prefix + " "):
                out = out[len(prefix) + 1:]
                changed = True
        for suffix in _SUFFIXES:
            if out.endswith(" " + suffix):
                out = out[: -len(suffix) - 1]
                changed = True
    return out.strip()


@dataclass
class Step:
    """Une étape : un outil de l'assistant, une commande, ou une pause."""
    kind: str                      # "outil" | "shell" | "attendre"
    name: str = ""
    args: dict = field(default_factory=dict)
    seconds: float = 0.0
    label: str = ""


@dataclass
class Routine:
    name: str
    phrases: list[str]
    steps: list[Step]
    say: str = ""            # prononcé seulement si la voix est déjà éveillée
    card: bool = True        # confirmation visible, y compris micro coupé

    def score(self, spoken: str) -> float:
        """Ressemblance de la phrase entendue avec la meilleure formulation.

        Les longueurs très différentes sont écartées avant toute comparaison :
        sans ce garde-fou, une phrase de vingt mots finit par ressembler
        « assez » à une phrase de deux, et la routine part toute seule.
        """
        best = 0.0
        for phrase in self.phrases:
            if spoken == phrase:
                return 1.0
            ratio = len(spoken) / max(1, len(phrase))
            if not 0.7 <= ratio <= 1.4:
                continue
            best = max(best, SequenceMatcher(None, spoken, phrase).ratio())
        return best


# ── chargement ──────────────────────────────────────────────────────────────

_CACHE: tuple[float, list[Routine]] = (0.0, [])


def load(force: bool = False) -> list[Routine]:
    """Routines du fichier, rechargées dès qu'il change."""
    global _CACHE
    try:
        mtime = ROUTINES_PATH.stat().st_mtime
    except OSError:
        return []
    if not force and _CACHE[0] == mtime:
        return _CACHE[1]

    try:
        import yaml
        raw = yaml.safe_load(ROUTINES_PATH.read_text(encoding="utf-8")) or []
    except Exception as exc:
        print(f"[Routines] fichier illisible : {exc}")
        return _CACHE[1]

    routines: list[Routine] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            routine = _parse(entry)
        except Exception as exc:
            print(f"[Routines] entrée ignorée : {exc}")
            continue
        if routine:
            routines.append(routine)

    _CACHE = (mtime, routines)
    return routines


def _parse(entry: dict) -> Routine | None:
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("nom") or entry.get("name") or "").strip()
    phrases = entry.get("phrases") or ([name] if name else [])
    if isinstance(phrases, str):
        phrases = [phrases]
    phrases = [normalise(p) for p in phrases if str(p).strip()]
    if not name or not phrases:
        return None

    steps: list[Step] = []
    for raw_step in entry.get("etapes") or entry.get("steps") or []:
        if not isinstance(raw_step, dict):
            continue
        if "attendre" in raw_step or "wait" in raw_step:
            seconds = float(raw_step.get("attendre", raw_step.get("wait", 0)) or 0)
            steps.append(Step("attendre", seconds=min(seconds, 10.0),
                              label=f"attendre {seconds:g}s"))
        elif "shell" in raw_step:
            command = str(raw_step["shell"]).strip()
            steps.append(Step("shell", name=command, label=command[:60]))
        else:
            tool = str(raw_step.get("outil") or raw_step.get("tool") or "").strip()
            if not tool:
                continue
            args = raw_step.get("args") or {}
            steps.append(Step("outil", name=tool,
                              args=dict(args) if isinstance(args, dict) else {},
                              label=raw_step.get("libelle") or tool))
    if not steps:
        return None

    return Routine(
        name=name, phrases=phrases, steps=steps,
        say=str(entry.get("dire") or entry.get("say") or "").strip(),
        card=bool(entry.get("carte", entry.get("card", True))),
    )


# ── reconnaissance ──────────────────────────────────────────────────────────

def match(text: str) -> Routine | None:
    """Routine correspondant à la phrase, ou None si rien ne colle assez."""
    spoken = strip_wake(text)
    if not spoken:
        return None
    best: tuple[float, Routine] | None = None
    for routine in load():
        score = routine.score(spoken)
        if score >= FUZZY_THRESHOLD and (best is None or score > best[0]):
            best = (score, routine)
    return best[1] if best else None


def names() -> list[str]:
    return [r.name for r in load()]


# ── exécution ───────────────────────────────────────────────────────────────

def run(routine: Routine, dispatch: Callable[[str, dict], str],
        log: Callable[[str], None] | None = None) -> list[tuple[str, bool, str]]:
    """Exécute les étapes dans l'ordre. Bloquant : à lancer hors boucle audio.

    Une étape qui échoue n'arrête pas la routine : si le terminal refuse de
    s'ouvrir, la luminosité doit quand même être réglée. L'échec est rapporté,
    jamais avalé.
    """
    results: list[tuple[str, bool, str]] = []
    for step in routine.steps:
        try:
            if step.kind == "attendre":
                time.sleep(step.seconds)
                results.append((step.label, True, ""))
            elif step.kind == "shell":
                completed = subprocess.run(
                    shlex.split(step.name),
                    capture_output=True, text=True, timeout=SHELL_TIMEOUT_S,
                )
                ok = completed.returncode == 0
                message = (completed.stdout or completed.stderr or "").strip()
                results.append((step.label, ok, message.splitlines()[0][:120]
                                if message else ""))
            else:
                answer = dispatch(step.name, step.args) or ""
                results.append((step.label, True, str(answer)[:120]))
        except Exception as exc:
            results.append((step.label, False, str(exc)[:120]))
        if log:
            label, ok, message = results[-1]
            log(f"{'✓' if ok else '✗'} {label}" + (f" — {message}" if message else ""))
    return results


def summary(routine: Routine,
            results: list[tuple[str, bool, str]]) -> str:
    """Compte rendu court, pour la carte comme pour le modèle."""
    failed = [label for label, ok, _ in results if not ok]
    if not failed:
        return f"Routine « {routine.name} » exécutée ({len(results)} étapes)."
    return (f"Routine « {routine.name} » : {len(results) - len(failed)} étape(s) "
            f"sur {len(results)} — échec sur {', '.join(failed)}.")
