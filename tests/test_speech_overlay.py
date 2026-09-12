import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.speech_sync import caption_targets, split_caption_units


def test_le_texte_est_decoupe_sans_perdre_les_mots_ni_la_ponctuation():
    text = "Bonjour Anonymous, voici votre réponse complète et fluide."
    units = split_caption_units(text, words_per_unit=2)
    assert units == [
        "Bonjour Anonymous,", "voici votre", "réponse complète", "et fluide."
    ]
    assert " ".join(units) == text


def test_les_fragments_sont_repartis_dans_la_duree_audio():
    targets = caption_targets(2.0, 4.0, 4)
    assert targets == sorted(targets)
    assert targets[0] == 2.0
    assert targets[-1] < 6.0


def test_le_journal_separe_les_fragments_de_sous_titres(qapp):
    import ui

    log = ui.LogWidget()

    def append_now(fragment: str) -> None:
        log._enqueue(fragment)
        log._tmr.stop()
        while log._pos < len(log._text):
            log._step()
        log._typing = False

    append_now("[INLINE][20:46:04] Ano-GPT: à proximité")
    append_now("[INLINE]de votre")
    append_now("[INLINE]position.")

    assert "à proximité de votre position." in log.toPlainText()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_la_hauteur_reste_fixe_et_seule_la_largeur_suit_le_texte(qapp):
    from PyQt6.QtWidgets import QWidget
    import ui

    parent = QWidget()
    parent.resize(1100, 700)
    bubble = ui.CenterSpeechOverlay(parent)

    bubble.show_speech("Bonjour.", speaker="ai")
    short = bubble.fit_to_bounds(800, 260)

    medium_text = "Je vais maintenant vous expliquer clairement cette réponse."
    bubble.show_speech(medium_text, speaker="ai")
    medium = bubble.fit_to_bounds(800, 260)

    assert short.height() == bubble.FIXED_HEIGHT
    assert medium.height() == bubble.FIXED_HEIGHT
    assert medium.width() > short.width()
    assert medium.width() <= 800
    assert bubble._txt_lbl.wordWrap() is False
    assert bubble._txt_lbl.text() == medium_text


def test_une_phrase_tres_longue_ne_change_jamais_la_hauteur(qapp):
    import ui

    bubble = ui.CenterSpeechOverlay()
    long_text = "Une phrase extrêmement longue " * 30
    bubble.show_speech(long_text, speaker="ai")
    size = bubble.fit_to_bounds(600, 999)
    assert size.height() == bubble.FIXED_HEIGHT
    assert size.width() <= 600
    assert bubble._txt_lbl.toolTip() == long_text.strip()
    assert bubble._txt_lbl.text().endswith("…")


def test_la_bulle_utilisateur_est_elle_aussi_adaptative(qapp):
    import ui

    bubble = ui.CenterSpeechOverlay()
    bubble.show_speech(
        "Je voudrais que tu recherches mes derniers courriers importants.",
        speaker="user",
    )
    size = bubble.fit_to_bounds(460, 220)
    assert size.height() == bubble.FIXED_HEIGHT
    assert bubble._hdr_lbl.text() == "✓  ANONYMOUS"
    assert bubble._txt_lbl.text().startswith("Je voudrais")


def test_le_vrai_nom_de_lassistant_est_affiche(qapp):
    import ui

    bubble = ui.CenterSpeechOverlay()
    bubble.set_assistant_name("Ano-GPT")
    bubble.show_speech("Bonjour.", speaker="ai")
    assert "ANO-GPT" in bubble._hdr_lbl.text()
    assert "J.A.R.V.I.S" not in bubble._hdr_lbl.text()
