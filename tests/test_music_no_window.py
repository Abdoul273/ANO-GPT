"""La musique doit jouer dans la carte lecteur de l'interface, sans fenêtre.

Deux bugs corrigés ici :

1. La déclaration de l'outil annonçait « 'vlc' (default) » pour le paramètre
   `player`. Le modèle le remplissait donc spontanément, alors que `chosen` non
   vide court-circuite le lecteur headless (`_play_result`) et part sur un
   lecteur fenêtré `--force-window=yes`.

2. Fermer cette fenêtre pendant la vérification audio comptait comme un échec :
   `_launch_with_fallback` relançait la variante suivante, puis le lecteur
   suivant — jusqu'à 3 variantes × 4 lecteurs. « Je ferme, il rouvre. »
"""

import pytest

import actions.music as music


class _FakeIPC:
    def __init__(self):
        self.played = []

    def play(self, target, title="", artist=""):
        self.played.append((target, title, artist))


def test_sans_lecteur_demande_la_lecture_est_headless(monkeypatch):
    ipc = _FakeIPC()
    monkeypatch.setattr(music, "_HAS_IPC_PLAYER", True)
    monkeypatch.setattr(music, "get_player", lambda: ipc)
    monkeypatch.setattr(
        music, "_launch_with_fallback",
        lambda *a, **k: pytest.fail("un lecteur fenêtré a été lancé"),
    )

    out = music._play_result("/musique/morceau.mp3", False, "Morceau",
                             "Local", chosen="", session_memory=None)

    assert ipc.played == [("/musique/morceau.mp3", "Morceau", "Local")]
    assert "sans fenêtre" in out


def test_fermer_la_fenetre_narrete_la_chaine_de_replis(monkeypatch):
    """Le cœur du bug : une fermeture volontaire ne doit RIEN relancer."""
    calls = []

    def _fake_launch(binary, target, is_url, variant=0):
        calls.append((binary, variant))
        raise music.PlayerClosedByUser("VLC a été fermé pendant la lecture")

    monkeypatch.setattr(music, "_launch_one", _fake_launch)
    monkeypatch.setattr(
        music, "available_players",
        lambda for_url=False: [{"binary": "vlc", "label": "VLC"},
                               {"binary": "mpv", "label": "mpv"}],
    )
    monkeypatch.setattr(music, "_default_player", lambda is_url: "vlc")

    ok, note = music._launch_with_fallback("/x.mp3", False, None)

    assert ok is True, "une fermeture volontaire n'est pas un échec"
    assert len(calls) == 1, f"relances après fermeture : {calls}"
    assert "fermé" in note


def test_un_vrai_echec_essaie_bien_les_replis(monkeypatch):
    """Le garde-fou ne doit pas neutraliser les replis légitimes."""
    calls = []

    def _fake_launch(binary, target, is_url, variant=0):
        calls.append((binary, variant))
        return False, f"{binary} a refusé le média"

    monkeypatch.setattr(music, "_launch_one", _fake_launch)
    monkeypatch.setattr(
        music, "available_players",
        lambda for_url=False: [{"binary": "vlc", "label": "VLC"}],
    )
    monkeypatch.setattr(music, "_default_player", lambda is_url: "vlc")

    ok, _ = music._launch_with_fallback("/x.mp3", False, None)

    assert ok is False
    assert len(calls) > 1, "un échec réel doit encore tenter les variantes"


def test_la_declaration_ninvite_plus_a_choisir_un_lecteur():
    """C'est le « 'vlc' (default) » de la description qui poussait le modèle à
    remplir `player`, et donc à ouvrir une fenêtre."""
    import main

    decl = next(t for t in main.TOOL_DECLARATIONS if t["name"] == "music_control")
    desc = decl["parameters"]["properties"]["player"]["description"]

    assert "(default)" not in desc
    assert "LEAVE EMPTY" in desc


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
