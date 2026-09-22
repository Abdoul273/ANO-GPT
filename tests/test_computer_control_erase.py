"""« Efface » après « écris salut sans envoyer » : dans Kitty, Ctrl+A puis
Suppr ne retirait que le « s » (Ctrl+A = début de ligne en terminal), et
l'utilisateur restait avec « alut »."""
import pytest

import actions.computer_control as cc


@pytest.fixture
def keyboard(monkeypatch):
    sent = []
    window = {"address": "0xkitty", "class": "kitty"}
    monkeypatch.setattr(cc, "_active_window_info", lambda: dict(window))
    monkeypatch.setattr(cc, "_type_text", lambda text: f"Texte tapé : «{text}»")
    monkeypatch.setattr(cc, "_press_key", lambda key: sent.append(("key", key)) or f"Touche pressée : {key}")
    monkeypatch.setattr(cc, "_hotkey", lambda *keys: sent.append(("combo", "+".join(keys))) or "Combinaison envoyée : x")
    monkeypatch.setattr(cc, "_press_repeat", lambda key, n: sent.append(("repeat", key, n)) or True)
    monkeypatch.setattr(cc.kit, "hypr_invalidate", lambda: None)
    cc._last_typed.clear()
    return sent, window


def test_efface_retire_exactement_le_texte_tape(keyboard):
    sent, _ = keyboard
    cc.computer_control({"action": "type", "text": "salut"})

    out = cc.computer_control({"action": "erase"})

    assert sent == [("repeat", "backspace", 5)]
    assert "salut" in out
    # Plus rien à effacer ensuite : pas de second effacement à l'aveugle.
    assert "rien tapé" in cc.computer_control({"action": "erase"})


def test_la_description_vocale_efface_passe_par_erase(keyboard):
    sent, _ = keyboard
    cc.computer_control({"action": "type", "text": "salut"})
    cc.computer_control({"description": "efface"})
    assert sent == [("repeat", "backspace", 5)]


def test_clear_field_en_terminal_n_utilise_plus_ctrl_a(keyboard):
    sent, _ = keyboard
    cc.computer_control({"action": "clear_field"})
    assert ("combo", "ctrl+a") not in sent
    assert sent == [("combo", "ctrl+e"), ("combo", "ctrl+u")]


def test_clear_field_hors_terminal_selectionne_tout(keyboard):
    sent, window = keyboard
    window.update(address="0xchrome", **{"class": "google-chrome"})
    cc.computer_control({"action": "clear_field"})
    assert sent == [("combo", "ctrl+a"), ("key", "backspace")]


def test_rien_n_est_efface_si_la_fenetre_a_change(keyboard):
    sent, window = keyboard
    cc.computer_control({"action": "type", "text": "salut"})
    window["address"] = "0xautre"

    out = cc.computer_control({"action": "erase"})

    assert not sent
    assert "fenêtre active" in out


def test_un_texte_deja_valide_n_est_pas_efface(keyboard):
    sent, _ = keyboard
    cc.computer_control({"action": "type", "text": "ls", "press_enter": True})
    sent.clear()
    out = cc.computer_control({"action": "erase"})
    assert not sent
    assert "Entrée" in out


def test_dernier_mot_et_nombre_de_caracteres(keyboard):
    sent, _ = keyboard
    cc.computer_control({"description": "efface le dernier mot"})
    cc.computer_control({"description": "efface 3 caractères"})
    assert sent == [("combo", "ctrl+w"), ("repeat", "backspace", 3)]


def test_efface_tout(keyboard):
    sent, _ = keyboard
    assert "Ligne effacée" in cc.computer_control({"description": "efface tout"})
    assert sent == [("combo", "ctrl+e"), ("combo", "ctrl+u")]
