"""Veilles persistantes reliées aux événements proactifs."""

from __future__ import annotations

from actions import background_tasks as background
from actions.background_tasks import (
    BackgroundTaskService,
    extract_price,
    format_agent_result,
)


def _service(tmp_path):
    published = []

    def publish(topic, message, **kwargs):
        published.append((topic, message, kwargs))
        return True

    return BackgroundTaskService(publish, tmp_path / "tasks.json"), published


def test_les_taches_survivent_au_redemarrage_et_sannulent(tmp_path):
    service, _ = _service(tmp_path)
    created = service.add_build_wait("npm run build", message="Compilation finie.")

    restarted, _ = _service(tmp_path)
    assert restarted.list_tasks()[0]["id"] == created["id"]
    assert restarted.cancel(created["id"][:10])
    assert restarted.list_tasks() == []


def test_linterface_est_notifiee_a_la_creation_et_annulation(tmp_path):
    updates = []
    service = BackgroundTaskService(
        lambda *a, **k: True, tmp_path / "tasks.json", on_task_update=updates.append,
    )
    task = service.add_build_wait("make test")
    assert updates[-1]["id"] == task["id"]
    assert service.cancel(task["id"])
    assert updates[-1]["status"] == "cancelled"


def test_fin_de_build_declenche_exactement_la_tache_correspondante(tmp_path):
    service, published = _service(tmp_path)
    wanted = service.add_build_wait("cargo build", message="Rust est prêt.")
    service.add_build_wait("npm", message="JavaScript est prêt.")

    count = service.handle_event("terminal", {
        "command": "cargo build --release", "duration_s": 42, "exit_code": 0,
    })

    assert count == 1
    assert published[0][0] == "background-build"
    assert "Rust est prêt" in published[0][1]
    assert published[0][2]["dedupe_key"] == f"background:{wanted['id']}"
    assert len(service.list_tasks()) == 1


def test_arrivee_exige_une_transition_exterieur_interieur(tmp_path, monkeypatch):
    from core import geolocation

    monkeypatch.setattr(geolocation, "get_live_position", lambda **kwargs: None)
    service, published = _service(tmp_path)
    task = service.add_arrival_wait(
        "Achète du pain.", lat=9.5, lon=-13.7, radius_m=250, label="maison"
    )

    assert service.observe_location({"lat": 9.6, "lon": -13.7}) == 0
    assert service.observe_location({"lat": 9.5, "lon": -13.7}) == 1
    assert published[0][1] == "Achète du pain."
    assert published[0][2]["dedupe_key"] == f"background:{task['id']}"


def test_extraction_de_prix_privilegie_les_donnees_structurees():
    page = '''
      <script type="application/ld+json">
      {"offers":{"price":"1 299,50","priceCurrency":"EUR"}}
      </script>
      <div>4,8 sur 5</div>
    '''
    assert extract_price(page) == (1299.5, "EUR")


def test_la_page_inchangee_utilise_etag_puis_une_baisse_declenche(tmp_path, monkeypatch):
    import requests

    service, published = _service(tmp_path)
    task = service.add_price_watch(
        "https://shop.example/product", interval_seconds=300, label="Le produit"
    )
    calls = []
    prices = iter(("100.00", "80.00"))

    class Response:
        status_code = 200
        headers = {"ETag": '"version-1"'}

        def __init__(self):
            self.text = f'<meta property="product:price:amount" content="{next(prices)}">'

        def raise_for_status(self):
            return None

    def get(self, url, headers, timeout):
        calls.append((url, dict(headers), timeout))
        return Response()

    monkeypatch.setattr(requests.Session, "get", get)
    service._check_price(task["id"])
    assert published == []
    service._check_price(task["id"])

    assert calls[1][1]["If-None-Match"] == '"version-1"'
    assert published[0][0] == "background-price"
    assert "80" in published[0][1]
    assert service.list_tasks() == []


def test_intervalle_web_est_borne_a_cinq_minutes(tmp_path):
    service, _ = _service(tmp_path)
    task = service.add_price_watch("https://example.com/p", interval_seconds=1)
    assert task["spec"]["interval_seconds"] == 300


def test_un_build_fini_assistant_eteint_est_rejoue_au_redemarrage(tmp_path, monkeypatch):
    spool = tmp_path / "events"
    monkeypatch.setattr(background, "_EVENTS_PATH", spool)
    service, published = _service(tmp_path)
    service.add_build_wait("make release", message="La version est prête.")
    background.persist_external_event({
        "topic": "terminal",
        "data": {"command": "make release", "duration_s": 90, "exit_code": 0},
    })

    service._consume_spooled_events()

    assert published and "version est prête" in published[0][1]
    assert list(spool.iterdir()) == []


def test_une_mission_fantome_est_persistante_et_reprise_apres_coupure(tmp_path):
    service, _ = _service(tmp_path)
    created = service.add_agent_mission(
        "Analyse ce dépôt et écris les tests manquants.", workspace=tmp_path
    )
    service._tasks[0]["state"]["phase"] = "running"
    service._save()

    restarted, _ = _service(tmp_path)
    task = restarted.list_tasks()[0]
    assert task["id"] == created["id"]
    assert task["kind"] == "agent"
    assert task["state"]["phase"] == "queued"


def test_mission_fantome_terminee_archive_et_annonce(tmp_path, monkeypatch):
    import asyncio
    from core.ghost_agent import GhostResult

    service, published = _service(tmp_path)
    task = service.add_agent_mission("Audite le dépôt.", workspace=tmp_path)
    report = tmp_path / "rapport.md"

    monkeypatch.setattr(
        "core.ghost_agent.run_mission",
        lambda *args, **kwargs: GhostResult(
            "completed", "Deux vulnérabilités corrigées, tests validés.",
            str(report), 0, ("modifié: core/main.py", "créé: tests/test_fix.py"),
        ),
    )
    asyncio.run(service._run_ghost(task["id"]))

    finished = service.list_tasks(include_finished=True)[0]
    assert finished["status"] == "completed"
    assert finished["report_path"] == str(report)
    assert published[0][0] == "ghost-agent"
    assert "Patron" in published[0][1]
    assert "core/main.py" in published[0][1]
    assert finished["state"]["changed_files"] == [
        "modifié: core/main.py", "créé: tests/test_fix.py",
    ]
    assert published[0][2]["data"]["report_path"] == str(report)


def test_mission_peut_demander_un_journal_kitty_sans_devenir_bloquante(tmp_path, monkeypatch):
    import asyncio
    from core.ghost_agent import GhostResult

    service, _ = _service(tmp_path)
    task = service.add_agent_mission(
        "Corrige les tests.", workspace=tmp_path, show_terminal=True,
    )
    received = {}

    def fake_run(*args, **kwargs):
        received.update(kwargs)
        return GhostResult("completed", "Terminé.", str(tmp_path / "r.md"), 0)

    monkeypatch.setattr("core.ghost_agent.run_mission", fake_run)
    asyncio.run(service._run_ghost(task["id"]))
    assert received["show_terminal"] is True


def test_resultat_termine_reste_consultable_avec_rapport_et_fichiers(tmp_path):
    service, _ = _service(tmp_path)
    old = service._new_task("agent", {"mission": "Ancienne", "workspace": str(tmp_path)})
    old.update(status="completed", finished_at=10, summary="Ancien résultat")
    recent = service._new_task("agent", {"mission": "Récente", "workspace": str(tmp_path)})
    recent.update(
        status="completed", finished_at=20, summary="Tests corrigés.",
        report_path=str(tmp_path / "rapport.md"),
    )
    recent["state"] = {"changed_files": ["modifié: main.py"]}
    service._tasks.extend([old, recent])

    latest = service.agent_result()
    assert latest["id"] == recent["id"]
    rendered = format_agent_result(latest)
    assert "Tests corrigés" in rendered
    assert "modifié: main.py" in rendered
    assert "rapport.md" in rendered
    assert service.agent_result(old["id"][:10])["id"] == old["id"]


def test_annulation_mission_fantome_interrompt_le_travail_sans_annonce(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    from core.ghost_agent import GhostResult

    service, published = _service(tmp_path)
    task = service.add_agent_mission("Travail très long.", workspace=tmp_path)
    started = threading.Event()

    def fake_run(*args, **kwargs):
        started.set()
        while not kwargs["cancelled"]():
            time.sleep(0.01)
        return GhostResult("cancelled", "Mission annulée.", str(tmp_path / "r.md"))

    monkeypatch.setattr("core.ghost_agent.run_mission", fake_run)

    async def scenario():
        running = asyncio.create_task(service._run_ghost(task["id"]))
        assert await asyncio.to_thread(started.wait, 1)
        assert service.cancel(task["id"])
        await asyncio.wait_for(running, timeout=1)

    asyncio.run(scenario())
    assert service.list_tasks() == []
    assert service.list_tasks(include_finished=True)[0]["status"] == "cancelled"
    assert published == []
