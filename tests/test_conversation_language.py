from core.conversation_language import detect_language_switch, normalise_conversation_language


def test_french_is_the_safe_default():
    assert normalise_conversation_language(None).code == "fr-FR"
    assert normalise_conversation_language("anglais").code == "en-US"


def test_explicit_requests_switch_and_return_to_french():
    assert detect_language_switch("Passe en anglais").code == "en-US"
    assert detect_language_switch("Speak English from now on").code == "en-US"
    assert detect_language_switch("Reviens normal").code == "fr-FR"
    assert detect_language_switch("Reste normal").code == "fr-FR"
    assert detect_language_switch("Go back to French").code == "fr-FR"


def test_foreign_words_do_not_switch_language_without_an_order():
    assert detect_language_switch("Hello, how are you?") is None
    assert detect_language_switch("Je parle anglais au travail") is None
