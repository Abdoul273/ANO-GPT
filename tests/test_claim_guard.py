from core.claim_guard import is_unbacked_claim, missing_typing_claim


def test_fausse_confirmation_detectee():
    assert is_unbacked_claim("Déplace la fenêtre vers le bureau 4",
                             "C'est fait, la fenêtre est sur le bureau quatre.", 0)
    assert is_unbacked_claim("Ouvre l'application Ora", "L'application Ora est ouverte.", 0)


def test_outil_appele_ou_simple_question_ne_declenche_pas():
    assert not is_unbacked_claim("Ouvre Aura", "C'est fait.", 1)
    assert not is_unbacked_claim("Est-ce que Aura est ouverte ?", "Oui, elle est ouverte.", 0)
    assert not is_unbacked_claim("Salut", "C'est fait ?", 0)
    assert not is_unbacked_claim("Ouvre Aura", "Je lance ça, patiente.", 0)


def test_saisie_sans_outil_est_une_fausse_confirmation():
    assert is_unbacked_claim("écris salut sans envoyer", "J'ai écrit salut.", 0)
    assert is_unbacked_claim("tape okay", "C'est bon, j'ai tapé okay.", 0)


def test_ouvrir_kitty_ne_prouve_pas_la_saisie():
    request = "ouvre kitty et tape okay"
    claim = "C'est fait, Kitty est ouvert et le message est passé."
    assert missing_typing_claim(request, claim, [("open_app", {"app_name": "kitty"})])
    assert not missing_typing_claim(request, claim, [
        ("open_app", {"app_name": "kitty", "command": "okay"}),
    ])
    assert not missing_typing_claim(request, claim, [
        ("open_app", {"app_name": "kitty"}),
        ("computer_control", {"action": "type", "text": "okay", "press_enter": False}),
    ])
