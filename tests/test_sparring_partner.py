from actions.sparring_partner import (
    analyse_utterance,
    observe_sparring_utterance,
    sparring_partner,
)
from main import TOOL_DECLARATIONS


def test_outil_est_expose_au_modele_avec_contrat_interactif():
    tool = next(item for item in TOOL_DECLARATIONS if item["name"] == "sparring_partner")
    assert tool["parameters"]["required"] == ["action"]
    assert "CHAQUE réponse" in tool["description"]
    assert "end" in tool["parameters"]["properties"]["action"]["description"]


def test_demarrage_choisit_client_difficile_et_borne_les_manches():
    memory = {}
    result = sparring_partner({
        "action": "start",
        "scenario": "simulation avec un client très difficile",
        "difficulty": "expert",
        "rounds": 99,
        "objective": "défendre un retard de livraison",
    }, memory)

    state = memory["sparring_partner"]
    assert state["scenario"] == "client_difficile"
    assert state["difficulty"] == "expert"
    assert state["rounds"] <= 8
    assert state["active"] is True
    assert "UNE question" in result
    assert "retard" in result


def test_analyse_elocution_mesure_hesitations_repetitions_structure_et_debit():
    result = analyse_utterance(
        "Euh, en fait je je commencerais d'abord par mesurer, puis par exemple je testerais.",
        duration_ms=30_000,
    )

    assert result["filler_count"] == 2
    assert result["adjacent_repetitions"] == 1
    assert "d'abord" in result["structure_markers"]
    assert result["wpm"] is not None
    assert 0 <= result["clarity_score"] <= 100


def test_debit_nest_jamais_invente_pour_une_reponse_texte():
    result = analyse_utterance("Je réponds avec un exemple concret et un résultat mesuré.")
    assert result["wpm"] is None


def test_capture_transcription_est_idempotente_et_enrichit_la_duree():
    memory = {}
    sparring_partner({"action": "start", "scenario": "entretien technique"}, memory)

    assert observe_sparring_utterance(memory, "J'ai mesuré la latence avant de modifier le service.")
    assert not observe_sparring_utterance(
        memory,
        "J'ai mesuré la latence avant de modifier le service.",
        duration_ms=8_000,
    )
    answers = memory["sparring_partner"]["answers"]
    assert len(answers) == 1
    assert answers[0]["duration_ms"] == 8_000
    assert answers[0]["wpm"] is not None


def test_une_reponse_produit_feedback_et_question_suivante():
    memory = {}
    sparring_partner({
        "action": "start",
        "scenario": "entretien_embauche",
        "rounds": 3,
    }, memory)
    result = sparring_partner({
        "action": "answer",
        "answer": "Euh je suis motivé parce que ce poste correspond à mon expérience.",
    }, memory)

    assert "clarté" in result
    assert "Hésitations détectées" in result
    assert "pose cette objection/question" in result
    assert memory["sparring_partner"]["round"] == 1


def test_fin_automatique_produit_un_rapport_honnete():
    memory = {}
    sparring_partner({
        "action": "start",
        "scenario": "oral technique",
        "rounds": 3,
    }, memory)
    last = ""
    for answer in (
        "D'abord je définis le problème, ensuite je présente les contraintes.",
        "Par exemple je mesure le résultat avec une expérience contrôlée.",
        "En conclusion je donne la limite principale et la prochaine étape.",
    ):
        last = sparring_partner({"action": "answer", "answer": answer}, memory)

    assert memory["sparring_partner"]["active"] is False
    assert "RAPPORT DE SESSION" in last
    assert "non mesuré (session texte)" in last
    assert "uniquement l'élocution mesurable" in last


def test_pause_empeche_la_capture_et_reprise_la_reactive():
    memory = {}
    sparring_partner({"action": "start"}, memory)
    assert "pause" in sparring_partner({"action": "pause"}, memory).casefold()
    assert not observe_sparring_utterance(memory, "Cette phrase ne doit pas compter.", 2_000)
    assert "reprise" in sparring_partner({"action": "resume"}, memory).casefold()
    assert observe_sparring_utterance(memory, "Cette réponse doit être analysée.", 2_000)


def test_end_sans_reponse_ne_fabrique_pas_de_score():
    memory = {}
    sparring_partner({"action": "start"}, memory)
    report = sparring_partner({"action": "end"}, memory)
    assert report == "Session terminée sans réponse analysable."

