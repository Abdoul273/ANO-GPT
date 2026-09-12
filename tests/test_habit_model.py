from datetime import datetime, timedelta

from core.habit_model import HabitModel


def test_cinq_occurrences_reparties_creent_une_seule_suggestion(tmp_path):
    model = HabitModel(tmp_path / "habits.db")
    now = datetime(2026, 8, 16, 21, 5)
    for days in (1, 4, 7, 11, 16):
        model.record("music", now - timedelta(days=days))
    assert model.candidate(now) == "music"
    assert model.candidate(now) is None


def test_un_refus_suspend_l_habitude_un_mois(tmp_path):
    model = HabitModel(tmp_path / "habits.db")
    now = datetime(2026, 8, 16, 21, 5)
    for days in (1, 4, 7, 11, 16):
        model.record("music", now - timedelta(days=days))
    model.decline("music", now)
    assert model.candidate(now + timedelta(days=1)) is None


def test_rafale_sur_un_seul_jour_ne_devient_pas_une_habitude(tmp_path):
    model = HabitModel(tmp_path / "habits.db")
    now = datetime(2026, 8, 16, 21, 5)
    for _ in range(10):
        model.record("music", now)
    assert model.candidate(now) is None


def test_import_historique_ne_compte_qu_une_fois(tmp_path):
    model = HabitModel(tmp_path / "habits.db")
    now = datetime(2026, 8, 16, 21, 5)
    events = [("music", now - timedelta(days=d)) for d in (1, 4, 7, 11, 16)]
    model.import_once("outils", events)
    model.import_once("outils", events)
    assert model.candidate(now) == "music"
