from core.auto_persona import detect_contextual_persona


def test_routes_explicit_english_learning_requests_to_the_bilingual_coach():
    assert detect_contextual_persona("Aide-moi à apprendre l'anglais") == "english_learning_coach"
    assert detect_contextual_persona("I need English pronunciation practice") == "english_learning_coach"


def test_routes_job_interview_preparation_to_its_coach():
    assert detect_contextual_persona("Aide-moi à me préparer pour mon entretien d'embauche") == "job_interview_coach"
    assert detect_contextual_persona("Fais une simulation d'interview pour un job") == "job_interview_coach"


def test_does_not_change_persona_for_an_ordinary_english_sentence():
    assert detect_contextual_persona("Hello, how are you?") is None
