import time

from core import auto_fix, incident_log


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(incident_log, "_INCIDENTS", [])
    monkeypatch.setattr(incident_log, "_LAST_ANNOUNCED", {})
    monkeypatch.setattr(incident_log, "_PENDING", [])
    monkeypatch.setattr(incident_log, "_LOADED", True)
    monkeypatch.setattr(incident_log, "ANNOUNCE_DELAY_S", 0.01)


def test_incident_is_recorded_with_project_frame_and_announced(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    published = []
    incident_log.bind(lambda topic, text, **kw: published.append((topic, text, kw)), None)
    try:
        raise ValueError("clé API absente")
    except ValueError as caught:
        exc = caught
        inc = incident_log.record("weather_report", exc, message="La météo n'a pas pu répondre.")
    assert inc.kind == "ValueError" and inc.file.startswith("tests/")
    assert inc.line > 0
    time.sleep(0.1)
    assert published and published[0][0] == "incident"
    assert "weather_report" in published[0][1] and "corrige" in published[0][1]
    # la même erreur juste après n'est pas ré-annoncée
    incident_log.record("weather_report", exc, message="La météo n'a pas pu répondre.")
    time.sleep(0.1)
    assert len(published) == 1
    assert incident_log.last().key == inc.key
    assert incident_log.find("la météo").key == inc.key
    assert (tmp_path / "jarvis" / "incidents.json").exists()


def test_repair_runs_engine_in_background_and_announces(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    incident_log.bind(None, None)
    inc = incident_log.record("tiktok_tracker", RuntimeError("boum"), message="boum")
    monkeypatch.setattr(auto_fix, "available_engines", lambda: ["claude"])
    monkeypatch.setattr(auto_fix, "_head", lambda cwd: "abc")
    monkeypatch.setattr(auto_fix, "_changed_files", lambda cwd, since: ["actions/tiktok_tracker.py"])
    monkeypatch.setattr(auto_fix, "_engine_claude", lambda prompt, cwd: (0, "Le parseur plantait sur un id vide ; corrigé dans tiktok_tracker.py."))
    ran = []
    monkeypatch.setattr(auto_fix.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    import core.thread_pool as tp
    monkeypatch.setattr(tp, "get_thread_pool", lambda: (_ for _ in ()).throw(RuntimeError("pas de pool")))
    said, cards = [], []

    class P:
        def show_card(self, k, t, b): cards.append(b)

    text = auto_fix.repair(inc, player=P(), speak=said.append, on_done=ran.append)
    assert "Je répare" in text and "claude" in text
    assert ran and ran[0]["ok"] and ran[0]["files"] == ["actions/tiktok_tracker.py"]
    assert said and "C'est corrigé" in said[0] and "redémarre" in said[0]
    assert incident_log.last(unresolved_only=True) is None
    assert "✅" in cards[-1]


def test_prompt_contains_traceback_and_rules(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    incident_log.bind(None, None)
    inc = incident_log.record("email_control", KeyError("subject"), message="sujet manquant")
    prompt = auto_fix.build_prompt(inc)
    assert "email_control" in prompt and "KeyError" in prompt
    assert "pytest" in prompt and "main.py" in prompt and "git push" in prompt


def test_self_repair_tool_routes_corrige_to_autofix(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    incident_log.bind(None, None)
    from actions.self_repair import self_repair
    assert "Aucune erreur" in self_repair({"action": "last_error"})
    inc = incident_log.record("weather_report", ValueError("x"), message="x")
    monkeypatch.setattr(auto_fix, "repair", lambda i, player=None, speak=None: f"réparation de {i.source}")
    assert self_repair({"action": "repair"}) == "réparation de weather_report"
    assert self_repair({"action": "repair", "tool": "météo"}) == "réparation de weather_report"
    assert "weather_report" in self_repair({"action": "last_error"})
