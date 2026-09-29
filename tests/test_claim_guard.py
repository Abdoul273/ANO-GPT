from core.claim_guard import is_unbacked_claim


def test_fausse_confirmation_detectee():
    assert is_unbacked_claim("Déplace la fenêtre vers le bureau 4",
                             "C'est fait, la fenêtre est sur le bureau quatre.", 0)
    assert is_unbacked_claim("Ouvre l'application Ora", "L'application Ora est ouverte.", 0)


def test_outil_appele_ou_simple_question_ne_declenche_pas():
    assert not is_unbacked_claim("Ouvre Aura", "C'est fait.", 1)
    assert not is_unbacked_claim("Est-ce que Aura est ouverte ?", "Oui, elle est ouverte.", 0)
    assert not is_unbacked_claim("Salut", "C'est fait ?", 0)
    assert not is_unbacked_claim("Ouvre Aura", "Je lance ça, patiente.", 0)
