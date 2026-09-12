"""Régressions du compagnon flottant piloté par le focus Hyprland."""

from core import hypr_focus


def _watcher(events: list[bool]) -> hypr_focus.FocusWatcher:
    return hypr_focus.FocusWatcher(
        events.append,
        app_classes=("jarvis-dashboard",),
        ignore_titles=("ANO Orb",),
    )


def test_workspace_relit_la_fenetre_active_au_lieu_de_forcer_absent(monkeypatch):
    events: list[bool] = []
    watcher = _watcher(events)
    monkeypatch.setattr(
        hypr_focus,
        "active_window",
        lambda: ("jarvis-dashboard", "JARVIS — MARK XLIX"),
    )

    watcher._handle("workspace>>2")

    assert events == [True]


def test_changement_de_bureau_affiche_le_compagnon_si_une_autre_app_est_active(monkeypatch):
    events: list[bool] = []
    watcher = _watcher(events)
    monkeypatch.setattr(hypr_focus, "active_window", lambda: ("firefox", "ANO-GPT"))

    watcher._handle("focusedmon>>DP-1,3")

    assert events == [False]


def test_la_fenetre_compagnon_ne_compte_jamais_comme_fenetre_principale(monkeypatch):
    events: list[bool] = []
    watcher = _watcher(events)
    monkeypatch.setattr(
        hypr_focus, "active_window", lambda: ("jarvis-dashboard", "ANO Orb")
    )

    watcher.refresh()

    assert events == [False]


def test_echec_de_resynchronisation_conserve_le_dernier_etat(monkeypatch):
    events: list[bool] = []
    watcher = _watcher(events)
    monkeypatch.setattr(hypr_focus, "active_window", lambda: None)

    watcher._handle("closewindow>>abc")

    assert events == []
