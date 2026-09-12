"""Le volume musical ne doit jamais modifier la sortie audio globale."""

from actions import music


def test_volume_lecteur_interne_est_relatif_et_cible_le_lecteur(monkeypatch):
    class Player:
        def __init__(self):
            self.levels = []

        def get_status(self):
            return {"state": "playing", "volume": 70}

        def set_volume(self, level):
            self.levels.append(level)
            return True

    player = Player()
    monkeypatch.setattr(music, "_HAS_IPC_PLAYER", True)
    monkeypatch.setattr(music, "get_player", lambda: player)

    result = music.music_control({"action": "volume", "value": "-10"})

    assert player.levels == [60]
    assert "musique" in result.lower()


def test_volume_mpris_ne_retombe_pas_sur_pactl(monkeypatch):
    calls = []
    monkeypatch.setattr(music, "_HAS_IPC_PLAYER", False)
    monkeypatch.setattr(music, "_mpris_target", lambda: "spotify")

    def fake_playerctl(*args):
        calls.append(args)
        return (True, "0.50") if len(calls) == 1 else (True, "")

    monkeypatch.setattr(music, "_playerctl", fake_playerctl)

    result = music.music_control({"action": "volume", "value": "-10"})

    assert calls == [
        ("--player", "spotify", "volume"),
        ("--player", "spotify", "volume", "0.40"),
    ]
    assert "musique" in result.lower()
