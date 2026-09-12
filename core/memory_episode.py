"""core/memory_episode.py — Transformer une conversation en souvenir.

Un assistant qui retient chaque phrase ne retient rien : au tour suivant, le
contexte est noyé. Ce qui se garde d'une conversation tient en deux lignes —
de quoi il a été question, et ce qui en est sorti.

Le résumé est confié à `agent_brain` (Antigravity), pour deux raisons : il ne
coûte rien de plus que l'abonnement, et il tourne en processus séparé à
priorité basse, donc sans disputer le CPU à la voix. S'il n'est pas là, un
repli local écrit quand même la trace : un souvenir approximatif vaut mieux
qu'un trou.

Rien ici ne s'exécute sur la boucle audio : `flush()` est fait pour être
appelé depuis un thread de fond.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from core import memory_store

# En dessous, il ne s'est rien passé qui mérite un souvenir : un « quelle heure
# est-il » n'a pas besoin d'être raconté demain.
MIN_TURNS = 3
MIN_DURATION_S = 45.0

# Une session Live se referme à chaque coupure réseau. Sans ce garde-fou, une
# connexion instable lancerait l'agent de résumé toutes les deux minutes.
MIN_INTERVAL_S = 300.0

SUMMARY_TIMEOUT_S = 60


class EpisodeRecorder:
    """Accumule les tours d'une conversation, puis en écrit le résumé."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: list[tuple[str, str]] = []
        self._started: float = 0.0
        self._last_turn: float = 0.0
        self._last_flush: float = 0.0

    # ── collecte ────────────────────────────────────────────────────────────

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        user_text = " ".join((user_text or "").split())
        assistant_text = " ".join((assistant_text or "").split())
        if not user_text and not assistant_text:
            return
        now = time.monotonic()
        with self._lock:
            if not self._turns:
                self._started = now
            self._last_turn = now
            # Une conversation très longue n'est pas mieux résumée par la
            # totalité de ses tours : les quarante derniers suffisent, et
            # bornent ce qui part à l'agent.
            self._turns.append((user_text, assistant_text))
            del self._turns[:-40]

    @property
    def idle_seconds(self) -> float:
        with self._lock:
            if not self._turns:
                return 0.0
            return time.monotonic() - self._last_turn

    # ── écriture ────────────────────────────────────────────────────────────

    def flush(self, *, reason: str = "", allow_agent: bool = True) -> str | None:
        """Clôt la conversation en cours et écrit son résumé. Bloquant."""
        with self._lock:
            turns = list(self._turns)
            started = self._started
            self._turns.clear()
            if not turns:
                return None
            duration = time.monotonic() - started
            recent_flush = (time.monotonic() - self._last_flush) < MIN_INTERVAL_S
            self._last_flush = time.monotonic()

        if len(turns) < MIN_TURNS or duration < MIN_DURATION_S:
            return None

        summary = ""
        if allow_agent and not recent_flush:
            summary = _summarize_with_agent(turns)
        if not summary:
            summary = _summarize_locally(turns)
        if not summary:
            return None

        memory_store.record_episode(summary)
        print(f"[Mémoire] 📓 épisode enregistré ({reason or 'fin de session'}) : "
              f"{summary[:90]}")
        return summary


# ── résumés ─────────────────────────────────────────────────────────────────

def _transcript(turns: list[tuple[str, str]]) -> str:
    lines = []
    for user_text, assistant_text in turns:
        if user_text:
            lines.append(f"Lui : {user_text}")
        if assistant_text:
            lines.append(f"Toi : {assistant_text}")
    return "\n".join(lines)[:6000]


def _summarize_with_agent(turns: list[tuple[str, str]]) -> str:
    try:
        from core import agent_brain
    except Exception:
        return ""
    if not agent_brain.available():
        return ""

    question = (
        "Voici la transcription d'une conversation entre l'utilisateur et son "
        "assistant vocal. Résume-la en deux phrases maximum, à la troisième "
        "personne, en gardant uniquement ce qui aura de la valeur dans une "
        "semaine : de quoi il a été question, ce qui a été décidé ou fait. "
        "N'invente rien, ne commente pas, ne dis pas « l'utilisateur a "
        "demandé » plus d'une fois. Réponds seulement par le résumé.\n\n"
        f"{_transcript(turns)}"
    )
    try:
        answer = agent_brain.think(question, timeout=SUMMARY_TIMEOUT_S)
    except Exception as exc:
        print(f"[Mémoire] résumé délégué impossible : {exc}")
        return ""
    answer = " ".join((answer or "").split())
    # L'agent signale ses propres échecs en français : ces phrases ne sont pas
    # des résumés et n'ont rien à faire dans la mémoire.
    if not answer or answer.lower().startswith(("l'agent", "la question")):
        return ""
    return answer[:memory_store.MAX_VALUE_CHARS]


def _summarize_locally(turns: list[tuple[str, str]]) -> str:
    """Repli sans modèle : les demandes les plus substantielles, mises bout à bout."""
    asked = [u for u, _ in turns if len(u) > 12]
    if not asked:
        return ""
    asked.sort(key=len, reverse=True)
    kept = asked[:3]
    heure = datetime.now().strftime("%H:%M")
    return (f"Conversation de {heure} — il a demandé : "
            + " ; ".join(k.rstrip(".?!") for k in kept))[
        : memory_store.MAX_VALUE_CHARS]
