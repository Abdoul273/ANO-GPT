import json

from core.distraction_guard import DistractionGuard, distracting_feed
from main import TOOL_DECLARATIONS


class Clock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make_guard(tmp_path, clock=None, **kwargs):
    clock = clock or Clock()
    return DistractionGuard(
        tmp_path / "focus.json",
        clock=clock,
        window_provider=kwargs.get("window_provider", lambda: ("code", "Projet")),
        tab_provider=kwargs.get("tab_provider", lambda: []),
        tab_closer=kwargs.get("tab_closer", lambda tab: True),
        url_opener=kwargs.get("url_opener", lambda url: True),
    )


def test_outil_focus_est_expose_avec_arret_et_restauration():
    tool = next(item for item in TOOL_DECLARATIONS if item["name"] == "focus_guard")
    actions = tool["parameters"]["properties"]["action"]["description"]
    assert tool["parameters"]["required"] == ["action"]
    assert "restore" in actions
    assert "stop" in actions


def test_flux_cibles_sont_precis_sans_bloquer_les_pages_normales():
    assert distracting_feed("https://youtube.com/shorts/abc") == "YouTube Shorts"
    assert distracting_feed("https://www.instagram.com/reels/abc") == "Instagram Reels"
    assert distracting_feed("https://x.com/home") == "X/Twitter"
    assert distracting_feed("https://www.tiktok.com/") == "TikTok"
    assert distracting_feed("https://youtube.com/watch?v=abc") is None
    assert distracting_feed("https://x.com/anonymous") is None
    assert distracting_feed("https://instagram.com/anonymous/") is None


def test_aucune_observation_ni_blocage_hors_session(tmp_path):
    closed = []
    guard = make_guard(
        tmp_path,
        tab_provider=lambda: [{
            "url": "https://youtube.com/shorts/abc", "source": "cdp",
        }],
        tab_closer=lambda tab: closed.append(tab) or True,
    )
    assert guard.poll() == []
    assert closed == []
    assert guard.metrics()["switches_10m"] == 0


def test_demarrage_exige_objectif_et_borne_configuration(tmp_path):
    guard = make_guard(tmp_path)
    assert "objectif" in guard.control({"action": "start"}).casefold()
    result = guard.control({
        "action": "start", "goal": "terminer le rapport",
        "work_minutes": 999, "break_minutes": 1, "switch_threshold": 99,
    })
    assert "120 minutes" in result
    assert guard._state["break_minutes"] == 3
    assert guard._state["switch_threshold"] == 30


def test_bloque_uniquement_onglet_cdp_et_permet_restauration(tmp_path):
    closed = []
    opened = []
    tabs = [
        {"url": "https://youtube.com/shorts/abc", "source": "cdp", "id": "1"},
        {"url": "https://instagram.com/reels/abc", "source": "hyprland", "id": "2"},
        {"url": "https://youtube.com/watch?v=ok", "source": "cdp", "id": "3"},
    ]
    guard = make_guard(
        tmp_path,
        tab_provider=lambda: tabs,
        tab_closer=lambda tab: closed.append(tab["id"]) or True,
        url_opener=lambda url: opened.append(url) or True,
    )
    guard.control({"action": "start", "goal": "coder"})
    events = guard.poll()
    assert closed == ["1"]
    assert any("YouTube Shorts" in event["message"] for event in events)
    assert "1/1" in guard.control({"action": "restore"})
    assert opened == ["https://youtube.com/shorts/abc"]


def test_allow_suspend_blocage_sans_arreter_focus(tmp_path):
    closed = []
    guard = make_guard(
        tmp_path,
        tab_provider=lambda: [{"url": "https://x.com/home", "source": "cdp"}],
        tab_closer=lambda tab: closed.append(tab) or True,
    )
    guard.control({"action": "start", "goal": "écrire"})
    assert "autorisés" in guard.control({"action": "allow"})
    guard.poll()
    assert guard.active
    assert not closed


def test_dispersion_declenche_apres_douze_basculements_sans_progres(tmp_path):
    clock = Clock()
    current = ["Initial"]
    guard = make_guard(tmp_path, clock, window_provider=lambda: ("browser", current[0]))
    guard.control({"action": "start", "goal": "finir le chapitre", "switch_threshold": 12})
    guard.observe_window(("browser", "Initial"))
    for index in range(12):
        clock.advance(30)
        current[0] = f"Onglet {index}"
        guard.observe_window(("browser", current[0]))
    events = guard.poll()
    assert guard.metrics()["dispersed"] is True
    assert any("12 fois" in event["message"] for event in events)


def test_progression_efface_la_dispersion(tmp_path):
    clock = Clock()
    guard = make_guard(tmp_path, clock)
    guard.control({"action": "start", "goal": "finir"})
    guard.observe_window(("app", "a"))
    for index in range(12):
        clock.advance(30)
        guard.observe_window(("app", str(index)))
    assert guard.metrics()["dispersed"]
    assert "enregistrée" in guard.control({"action": "progress", "note": "plan terminé"})
    assert guard.metrics()["switches_10m"] == 0


def test_pause_intelligente_protege_cinq_minutes_de_flow_puis_demarre(tmp_path):
    clock = Clock()
    guard = make_guard(tmp_path, clock, window_provider=lambda: ("code", "Projet stable"))
    guard.control({
        "action": "start", "goal": "coder", "work_minutes": 15,
        "break_minutes": 3,
    })
    guard.poll()
    clock.advance(15 * 60)
    events = guard.poll()
    assert guard._state["phase"] == "focus"
    assert any("cinq minutes de flow" in event["message"] for event in events)
    clock.advance(5 * 60)
    events = guard.poll()
    assert guard._state["phase"] == "break"
    assert any("Pause intelligente" in event["message"] for event in events)
    clock.advance(3 * 60)
    events = guard.poll()
    assert guard._state["phase"] == "focus"
    assert any("Pause terminée" in event["message"] for event in events)


def test_titres_observes_ne_sont_jamais_persistes(tmp_path):
    guard = make_guard(tmp_path)
    guard.control({"action": "start", "goal": "travail confidentiel"})
    guard.observe_window(("browser", "Dossier médical extrêmement secret"))
    persisted = (tmp_path / "focus.json").read_text(encoding="utf-8")
    assert "Dossier médical" not in persisted
    assert "travail confidentiel" in persisted
    json.loads(persisted)


def test_urls_bloquees_restent_en_memoire_et_non_sur_disque(tmp_path):
    secret_url = "https://youtube.com/shorts/video-privee-123"
    guard = make_guard(
        tmp_path,
        tab_provider=lambda: [{"url": secret_url, "source": "cdp"}],
    )
    guard.control({"action": "start", "goal": "écrire"})
    guard.poll()
    assert secret_url in guard._blocked_urls
    assert secret_url not in (tmp_path / "focus.json").read_text(encoding="utf-8")


def test_stop_desactive_immediatement_toute_action(tmp_path):
    closed = []
    guard = make_guard(
        tmp_path,
        tab_provider=lambda: [{"url": "https://x.com/home", "source": "cdp"}],
        tab_closer=lambda tab: closed.append(tab) or True,
    )
    guard.control({"action": "start", "goal": "travail"})
    assert "arrêté" in guard.control({"action": "stop"})
    assert guard.poll() == []
    assert not closed


def test_stop_pendant_inventaire_cdp_empeche_la_fermeture(tmp_path):
    closed = []
    holder = {}

    def tabs_then_stop():
        holder["guard"].control({"action": "stop"})
        return [{"url": "https://x.com/home", "source": "cdp"}]

    guard = make_guard(
        tmp_path,
        tab_provider=tabs_then_stop,
        tab_closer=lambda tab: closed.append(tab) or True,
    )
    holder["guard"] = guard
    guard.control({"action": "start", "goal": "travail"})
    assert guard.poll() == []
    assert closed == []
