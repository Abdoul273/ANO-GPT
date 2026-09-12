"""Simulateur vocal d'entretien, d'oral technique et de client difficile.

Le module garde un état inter-tours dans ``session_memory``. Il ne prétend pas
juger seul la justesse métier d'une réponse : il mesure objectivement la forme
de la prise de parole et fournit au modèle les critères attendus pour évaluer
le fond dans le rôle demandé.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from typing import Any

from core import action_kit as kit


_STATE_KEY = "sparring_partner"
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_FILLER_PATTERNS = {
    "euh": re.compile(r"\b(?:eu+h*|heu+h*)\b", re.IGNORECASE),
    "hum": re.compile(r"\b(?:hum+|hmm+)\b", re.IGNORECASE),
    "bah/ben": re.compile(r"\b(?:bah|ben)\b", re.IGNORECASE),
    "du coup": re.compile(r"\bdu\s+coup\b", re.IGNORECASE),
    "en fait": re.compile(r"\ben\s+fait\b", re.IGNORECASE),
    "genre": re.compile(r"\bgenre\b", re.IGNORECASE),
    "voilà": re.compile(r"\bvoil[aà]\b", re.IGNORECASE),
}
_STRUCTURE_MARKERS = (
    "d'abord", "premièrement", "ensuite", "puis", "parce que",
    "par exemple", "concrètement", "cependant", "donc", "résultat",
    "en conclusion", "finalement", "d'une part", "d’autre part",
)

_DIFFICULTY_RULES = {
    "debutant": "Laisse finir, reformule si nécessaire et augmente progressivement la pression.",
    "intermediaire": "Sois réaliste, demande une preuve concrète et fais une objection à la fois.",
    "avance": "Conteste les généralités, impose des contraintes et exige des compromis explicites.",
    "expert": "Sois très exigeant : contradictions, relances courtes, données chiffrées et pression temporelle, sans humiliation.",
}


SCENARIOS: dict[str, dict[str, Any]] = {
    "entretien_technique": {
        "aliases": ("technique", "entretien technique", "recruteur technique", "developpeur"),
        "role": "recruteur technique exigeant",
        "opening": "Présentez-vous en 90 secondes et reliez votre parcours au poste visé.",
        "questions": (
            ("Décrivez un problème technique difficile que vous avez résolu.",
             "Demande des faits vérifiables, les contraintes et le résultat mesuré."),
            ("Votre solution fonctionne, mais double la latence en production. Que faites-vous ?",
             "Conteste toute réponse qui optimise avant de mesurer."),
            ("Expliquez un échec dont vous étiez responsable.",
             "Refuse les réponses qui maquillent une qualité en défaut."),
            ("Comment enquêteriez-vous sur une panne intermittente impossible à reproduire localement ?",
             "Exige hypothèses, instrumentation, réduction du périmètre et critères d'arrêt."),
            ("Un collègue senior rejette votre proposition sans argument. Comment réagissez-vous ?",
             "Teste désaccord professionnel, écoute et décision documentée."),
            ("Concevez un service qui doit rester disponible pendant une montée de charge brutale.",
             "Demande compromis, observabilité, dégradation contrôlée et validation."),
            ("Pourquoi devrions-nous vous recruter plutôt qu'un candidat plus expérimenté ?",
             "Interrompt les généralités et réclame une preuve concrète."),
        ),
    },
    "entretien_embauche": {
        "aliases": ("entretien", "embauche", "recruteur", "rh"),
        "role": "recruteur exigeant",
        "opening": "Présentez-vous brièvement et dites pourquoi ce poste vous intéresse.",
        "questions": (
            ("Quelle réalisation récente démontre le mieux votre valeur ?",
             "Demande situation, action personnelle et résultat concret."),
            ("Quelle est votre faiblesse la plus gênante pour ce poste ?",
             "Refuse les faux défauts et cherche un plan de progression réel."),
            ("Pourquoi quittez-vous votre situation actuelle ?",
             "Teste la diplomatie et l'absence de dénigrement."),
            ("Parlez-moi d'un conflit professionnel que vous avez mal géré.",
             "Demande responsabilité personnelle et apprentissage."),
            ("Vos prétentions semblent élevées. Justifiez-les.",
             "Exige valeur, données de marché et marge de négociation."),
            ("Vous n'avez pas toute l'expérience demandée. Pourquoi prendre ce risque ?",
             "Cherche preuves de transfert de compétences et vitesse d'apprentissage."),
        ),
    },
    "client_difficile": {
        "aliases": ("client", "client difficile", "objection", "commercial"),
        "role": "client mécontent, sceptique et pressé",
        "opening": "Votre livraison est en retard et personne ne m'a prévenu. Expliquez-vous.",
        "questions": (
            ("Pourquoi devrais-je encore vous faire confiance ?",
             "Exige reconnaissance, faits, correction datée et suivi."),
            ("Votre concurrent coûte moins cher et promet davantage.",
             "Refuse le dénigrement ; demande une valeur différenciante prouvable."),
            ("Je veux un remboursement complet aujourd'hui.",
             "Teste empathie, limites de mandat et solution praticable."),
            ("Vous répétez des excuses, mais quel est votre plan exact ?",
             "Exige responsable, échéance, jalons et mécanisme d'escalade."),
            ("Je veux parler immédiatement à votre directeur.",
             "Teste désescalade sans empêcher une escalade légitime."),
            ("Garantissez-moi que cela ne se reproduira jamais.",
             "Piège : refuse une garantie absolue et propose des contrôles vérifiables."),
        ),
    },
    "oral_technique": {
        "aliases": ("oral", "examen", "examinateur", "oral technique"),
        "role": "examinateur technique rigoureux",
        "opening": "Définissez votre sujet, son objectif et les hypothèses de départ.",
        "questions": (
            ("Expliquez ce concept à une personne compétente mais non spécialiste.",
             "Sanctionne le jargon non défini et demande un exemple."),
            ("Quelle hypothèse, si elle est fausse, invalide votre raisonnement ?",
             "Cherche limites, conditions et falsifiabilité."),
            ("Donnez un contre-exemple à votre propre conclusion.",
             "Teste recul critique et domaine de validité."),
            ("Comment mesureriez-vous expérimentalement votre résultat ?",
             "Exige métrique, protocole, témoin et incertitude."),
            ("Je ne suis pas convaincu. Reformulez sans répéter vos mots précédents.",
             "Teste clarté, adaptation et synthèse."),
            ("Concluez en une minute : résultat, limite principale et prochaine étape.",
             "Coupe les digressions et exige une conclusion structurée."),
        ),
    },
}


def _plain(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _scenario_key(value: object) -> str:
    wanted = _plain(value).replace("-", " ").replace("_", " ").strip()
    if not wanted:
        return "entretien_technique"
    for key, spec in SCENARIOS.items():
        candidates = (key.replace("_", " "), *spec["aliases"])
        if any(_plain(candidate) in wanted or wanted in _plain(candidate) for candidate in candidates):
            return key
    return "entretien_technique"


def _words(text: str) -> list[str]:
    return [word.casefold().replace("’", "'") for word in _WORD_RE.findall(text)]


def analyse_utterance(text: str, duration_ms: float = 0.0) -> dict[str, Any]:
    """Retourne uniquement des métriques explicables à partir du texte/délai."""
    clean = " ".join(str(text or "").split()).strip()
    words = _words(clean)
    fillers = {
        label: len(pattern.findall(clean))
        for label, pattern in _FILLER_PATTERNS.items()
        if pattern.search(clean)
    }
    repeated = sum(1 for left, right in zip(words, words[1:]) if left == right)
    counts = Counter(words)
    overused = [word for word, count in counts.most_common(5) if count >= 4 and len(word) > 3]
    markers = [marker for marker in _STRUCTURE_MARKERS if marker in clean.casefold()]
    measured = float(duration_ms or 0.0) >= 1000.0
    wpm = round(len(words) / (float(duration_ms) / 60000.0)) if measured else None

    score = 62
    if 18 <= len(words) <= 180:
        score += 12
    elif len(words) < 8:
        score -= 18
    elif len(words) > 260:
        score -= 10
    score += min(12, len(markers) * 3)
    score -= min(24, sum(fillers.values()) * 4)
    score -= min(12, repeated * 4)
    if measured and wpm is not None:
        if 105 <= wpm <= 175:
            score += 8
        elif wpm < 75 or wpm > 220:
            score -= 10
    score = max(0, min(100, score))
    return {
        "text": clean,
        "words": len(words),
        "duration_ms": round(float(duration_ms or 0.0)),
        "wpm": wpm,
        "fillers": fillers,
        "filler_count": sum(fillers.values()),
        "adjacent_repetitions": repeated,
        "overused_words": overused,
        "structure_markers": markers,
        "clarity_score": score,
    }


def _state(memory: dict) -> dict | None:
    candidate = memory.get(_STATE_KEY)
    return candidate if isinstance(candidate, dict) else None


def observe_sparring_utterance(memory: dict, text: str, duration_ms: float = 0.0) -> bool:
    """Capture une transcription finale si une session attend une réponse.

    L'appel d'outil et la transcription finale peuvent arriver dans les deux
    ordres. La normalisation rend donc cette opération idempotente.
    """
    state = _state(memory)
    clean = " ".join(str(text or "").split()).strip()
    if not state or not state.get("active") or not clean:
        return False
    normalized = _plain(clean)
    duplicate = next(
        (item for item in reversed(state.get("answers", [])[-2:])
         if _plain(item.get("text", "")) == normalized),
        None,
    )
    if duplicate is not None:
        # Enrichir la mesure déjà créée par l'outil si la durée arrive après.
        if duration_ms and not duplicate.get("duration_ms"):
            duplicate.update(analyse_utterance(clean, duration_ms))
        return False
    state.setdefault("answers", []).append(analyse_utterance(clean, duration_ms))
    state["updated_at"] = time.time()
    return True


def _question(state: dict) -> dict[str, str] | None:
    spec = SCENARIOS[state["scenario"]]
    index = int(state.get("round", 0))
    if index == 0:
        return {"question": spec["opening"], "trap": "Évalue concision, pertinence et preuves concrètes."}
    questions = spec["questions"]
    if index - 1 >= len(questions):
        return None
    question, trap = questions[index - 1]
    return {"question": question, "trap": trap}


def _feedback(metric: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if metric["words"] < 8:
        notes.append("Réponse trop courte : développe une preuve ou un exemple.")
    elif metric["words"] > 260:
        notes.append("Réponse longue : annonce l'idée principale puis deux arguments maximum.")
    if metric["filler_count"]:
        detail = ", ".join(f"{key} ×{value}" for key, value in metric["fillers"].items())
        notes.append(f"Hésitations détectées : {detail}. Remplace-les par une courte pause.")
    if metric["adjacent_repetitions"]:
        notes.append("Répétitions immédiates détectées : ralentis au début des phrases.")
    if not metric["structure_markers"] and metric["words"] >= 20:
        notes.append("Structure peu visible : utilise contexte, action, résultat.")
    wpm = metric.get("wpm")
    if wpm is not None and wpm > 220:
        notes.append(f"Débit très rapide ({wpm} mots/min) : vise environ 120–170.")
    elif wpm is not None and wpm < 75:
        notes.append(f"Débit lent ({wpm} mots/min) : raccourcis les pauses et prépare ton ouverture.")
    if not notes:
        notes.append("Expression claire sur les indicateurs mesurables ; vérifie maintenant la précision du fond.")
    return notes


def _report(state: dict) -> str:
    answers = state.get("answers", [])
    if not answers:
        return "Session terminée sans réponse analysable."
    total_words = sum(item["words"] for item in answers)
    fillers = sum(item["filler_count"] for item in answers)
    repetitions = sum(item["adjacent_repetitions"] for item in answers)
    score = round(sum(item["clarity_score"] for item in answers) / len(answers))
    measured = [item for item in answers if item.get("duration_ms", 0) >= 1000]
    measured_words = sum(item["words"] for item in measured)
    measured_duration = sum(item["duration_ms"] for item in measured)
    avg_wpm = (
        round(measured_words / (measured_duration / 60000.0))
        if measured_duration else None
    )
    top_fillers = Counter()
    for item in answers:
        top_fillers.update(item["fillers"])
    filler_text = ", ".join(f"{key} ×{value}" for key, value in top_fillers.most_common()) or "aucune"
    pace = f"{avg_wpm} mots/min mesurés" if avg_wpm is not None else "non mesuré (session texte)"
    priorities = []
    if fillers:
        priorities.append("remplacer les hésitations par des pauses silencieuses")
    if repetitions:
        priorities.append("ralentir l'amorce des phrases")
    if any(not item["structure_markers"] and item["words"] >= 20 for item in answers):
        priorities.append("annoncer une structure contexte–action–résultat")
    if not priorities:
        priorities.append("conserver cette clarté et renforcer les preuves chiffrées")
    return (
        f"RAPPORT DE SESSION — {SCENARIOS[state['scenario']]['role']}\n"
        f"Réponses analysées : {len(answers)} · mots : {total_words} · "
        f"clarté : {score}/100 · débit : {pace}.\n"
        f"Hésitations : {filler_text} · répétitions immédiates : {repetitions}.\n"
        f"Priorités : {' ; '.join(priorities[:3])}.\n"
        "Le score concerne uniquement l'élocution mesurable ; ajoute ton évaluation "
        "du fond, une réponse améliorée et un exercice précis pour la prochaine session."
    )


@kit.action("sparring_partner")
def sparring_partner(parameters: dict | None, session_memory: dict | None) -> str:
    """Pilote une session et renvoie des instructions directement exploitables."""
    args = dict(parameters or {})
    memory = session_memory if isinstance(session_memory, dict) else {}
    action = _plain(args.get("action", "status")).replace(" ", "_")
    state = _state(memory)

    if action in {"start", "demarrer", "commencer", "nouvelle_session"}:
        scenario = _scenario_key(args.get("scenario") or args.get("role"))
        difficulty = _plain(args.get("difficulty") or "intermediaire")
        if difficulty not in {"debutant", "intermediaire", "avance", "expert"}:
            difficulty = "intermediaire"
        try:
            rounds = max(3, min(8, int(args.get("rounds") or 5)))
        except (TypeError, ValueError):
            rounds = 5
        spec = SCENARIOS[scenario]
        state = {
            "active": True,
            "scenario": scenario,
            "role": spec["role"],
            "difficulty": difficulty,
            "objective": str(args.get("objective") or "").strip()[:300],
            "rounds": min(rounds, len(spec["questions"]) + 1),
            "round": 0,
            "answers": [],
            "started_at": time.time(),
            "updated_at": time.time(),
        }
        memory[_STATE_KEY] = state
        prompt = _question(state)
        return (
            f"SESSION DÉMARRÉE — rôle : {spec['role']} · difficulté : {difficulty} · "
            f"{state['rounds']} manches. Objectif : {state['objective'] or 'entraînement général'}.\n"
            "Reste strictement dans ce rôle. Pose UNE question à la fois, attends la réponse, "
            "puis appelle sparring_partner(action='answer') avec la transcription exacte. "
            f"Comportement de difficulté : {_DIFFICULTY_RULES[difficulty]} "
            f"Question 1 : {prompt['question']}\nCritère caché : {prompt['trap']}"
        )

    if action in {"answer", "reponse", "next", "suivant"}:
        if not state or not state.get("active"):
            return "Aucune session active. Utilise action='start' avant d'évaluer une réponse."
        answer = str(args.get("answer") or "").strip()
        try:
            duration_ms = max(0.0, float(args.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            duration_ms = 0.0
        if answer:
            observe_sparring_utterance(memory, answer, duration_ms)
        if not state.get("answers"):
            return "Réponse absente : demande à l'utilisateur de répondre avant de continuer."
        metric = state["answers"][-1]
        state["round"] = int(state.get("round", 0)) + 1
        state["updated_at"] = time.time()
        notes = " ".join(_feedback(metric))
        if state["round"] >= state["rounds"]:
            state["active"] = False
            return f"DERNIÈRE RÉPONSE — clarté {metric['clarity_score']}/100. {notes}\n{_report(state)}"
        prompt = _question(state)
        if prompt is None:
            state["active"] = False
            return _report(state)
        return (
            f"MANCHE {state['round']}/{state['rounds']} — clarté mesurable "
            f"{metric['clarity_score']}/100. {notes}\n"
            "Donne un retour oral de deux phrases maximum sur la réponse, puis reprends ton rôle "
            f"({_DIFFICULTY_RULES[state['difficulty']]}) et pose cette objection/question : "
            f"{prompt['question']}\n"
            f"Adapte-la à cet objectif sans changer le piège : {state['objective'] or 'entraînement général'}.\n"
            f"Critère d'évaluation du fond : {prompt['trap']}"
        )

    if action in {"end", "stop", "terminer", "rapport", "report"}:
        if not state:
            return "Aucune session d'entraînement n'a été commencée."
        state["active"] = False
        state["updated_at"] = time.time()
        return _report(state)

    if action in {"pause", "resume", "reprendre"}:
        if not state:
            return "Aucune session d'entraînement n'a été commencée."
        state["active"] = action in {"resume", "reprendre"}
        state["updated_at"] = time.time()
        return "Session reprise." if state["active"] else "Session mise en pause."

    if not state:
        return "Aucune session active. Scénarios : entretien technique, entretien d'embauche, client difficile, oral technique."
    status = "active" if state.get("active") else "terminée"
    return (
        f"Session {status} : {SCENARIOS[state['scenario']]['role']}, difficulté "
        f"{state['difficulty']}, manche {state['round'] + 1}/{state['rounds']}, "
        f"{len(state.get('answers', []))} réponse(s) analysée(s)."
    )
