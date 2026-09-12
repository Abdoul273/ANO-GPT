import base64
import json
import stat
import time
from pathlib import Path

import actions.email as email_action
from core.email_service import (
    GmailService,
    GmailSetupRequired,
    build_gmail_search_query,
    _decode_header_value,
    _extract_message_content,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_un_compte_absent_est_signale_comme_non_configure(tmp_path, monkeypatch):
    """« La configuration n'est pas terminée » doit dire par quoi la terminer."""
    monkeypatch.setattr(
        "core.email_service.find_downloaded_client_secret", lambda: None
    )
    service = GmailService(
        token_file=tmp_path / "token.json",
        client_secret_file=tmp_path / "client.json",
    )
    status = service.status()
    assert status.configured is False
    assert status.authenticated is False
    assert status.next_step, "un diagnostic sans étape suivante n'aide personne"
    assert "console.cloud.google.com" in status.message
    assert "setup_gmail.py" in status.message


def test_une_cle_deja_telechargee_raccourcit_le_diagnostic(tmp_path, monkeypatch):
    """Inutile de réciter Google Cloud quand le fichier est déjà sur le disque."""
    key = tmp_path / "client_secret_1234.apps.googleusercontent.com.json"
    key.write_text(json.dumps({
        "installed": {"client_id": "id", "client_secret": "s"}
    }))
    monkeypatch.setattr(
        "core.email_service.find_downloaded_client_secret", lambda: key
    )
    service = GmailService(
        token_file=tmp_path / "token.json",
        client_secret_file=tmp_path / "absent.json",
    )
    status = service.status()
    assert "connecte Gmail" in status.message
    assert key.name in status.message


def test_la_cle_la_plus_recente_est_reperee_dans_les_telechargements(tmp_path, monkeypatch):
    from core.email_service import find_downloaded_client_secret

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "client_secret_ancien.json").write_text(json.dumps({
        "installed": {"client_id": "vieux", "client_secret": "s"}
    }))
    (downloads / "sans_rapport.json").write_text(json.dumps({"web": {}}))
    recent = downloads / "client_secret_recent.json"
    recent.write_text(json.dumps({
        "installed": {"client_id": "neuf", "client_secret": "s"}
    }))
    import os
    os.utime(recent, (time.time() + 60, time.time() + 60))

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert find_downloaded_client_secret() == recent


def test_une_cle_web_nest_pas_prise_pour_une_cle_bureau(tmp_path, monkeypatch):
    """Une clé OAuth « Application Web » ne peut pas ouvrir de flux local."""
    from core.email_service import find_downloaded_client_secret

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "client_secret_web.json").write_text(json.dumps({
        "web": {"client_id": "id", "client_secret": "s"}
    }))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert find_downloaded_client_secret() is None


def test_les_entetes_mime_sont_decodes():
    assert _decode_header_value("=?UTF-8?Q?R=C3=A9union_=C3=A0_Conakry?=") == "Réunion à Conakry"


def test_installation_de_la_cle_oauth_est_validee_et_privee(tmp_path):
    source = tmp_path / "download.json"
    source.write_text(json.dumps({
        "installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "secret"}
    }))
    target = tmp_path / "private" / "gmail_client_secret.json"
    service = GmailService(
        token_file=tmp_path / "token.json", client_secret_file=target
    )
    assert service.configure_client_secret(source) == target
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_corps_multipart_imbrique_et_pieces_jointes():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Version <b>HTML</b></p>")}},
                    {"mimeType": "text/plain", "body": {"data": _b64("Bonjour depuis Gmail")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "=?UTF-8?Q?facture_ao=C3=BBt.pdf?=",
                "body": {"attachmentId": "att-1", "size": 2048},
            },
        ],
    }
    body, attachments = _extract_message_content(payload)
    assert body == "Bonjour depuis Gmail"
    assert attachments == [{
        "filename": "facture août.pdf",
        "mime_type": "application/pdf",
        "size": 2048,
        "attachment_id": "att-1",
    }]


class _FakeService:
    def __init__(self):
        self.messages = [
            {
                "id": "gmail-abc", "sender": "Alice <alice@example.com>",
                "subject": "Projet", "date": "13 Aug", "snippet": "Voici le rapport",
                "unread": True, "attachments": [],
            },
            {
                "id": "gmail-def", "sender": "Bob <bob@example.com>",
                "subject": "Rendez-vous", "date": "12 Aug", "snippet": "Demain à 10 h",
                "unread": False, "attachments": [],
            },
        ]

    def get_recent(self, max_results=10):
        return self.messages[:max_results]

    def get_unread(self, max_results=10):
        return [m for m in self.messages if m["unread"]][:max_results]

    def search_emails(self, query, max_results=10):
        return self.messages[:max_results] if query else []

    def read_email(self, msg_id):
        assert msg_id == "gmail-def"
        return {
            **self.messages[1], "body": "Le rendez-vous est confirmé.",
            "attachments": [{"filename": "invitation.ics"}],
        }


def test_les_resultats_sont_memorises_et_lisibles_par_numero(monkeypatch):
    monkeypatch.setattr(email_action, "get_gmail_service", lambda: _FakeService())
    memory = {}
    listing = email_action.email_control({"action": "recent"}, session_memory=memory)
    assert "Alice" in listing and "Bob" in listing
    assert memory["email_results"] == ["gmail-abc", "gmail-def"]

    read = email_action.email_control(
        {"action": "read", "id": "2"}, session_memory=memory
    )
    assert "rendez-vous est confirmé" in read
    assert "invitation.ics" in read


def test_une_erreur_de_configuration_nest_pas_annoncee_comme_boite_vide(monkeypatch):
    class _Missing:
        def get_unread(self, max_results=10):
            raise GmailSetupRequired("autorisation absente")

    monkeypatch.setattr(email_action, "get_gmail_service", lambda: _Missing())
    result = email_action.email_control({"action": "unread"})
    assert "Configuration Gmail requise" in result
    assert "Aucun" not in result


# ── recherche avancée ──────────────────────────────────────────────────────

def test_la_recherche_naturelle_francaise_devient_une_requete_gmail_precise():
    from datetime import date

    plan = build_gmail_search_query(
        "les mails de Mamadou des trente derniers jours avec pièce jointe non lus",
        today=date(2026, 8, 15),
    )
    assert plan.gmail_query == (
        'from:"Mamadou" newer_than:30d has:attachment is:unread'
    )
    assert plan.interpreted is True


def test_une_requete_gmail_experte_nest_jamais_reformulee():
    query = "from:alice@example.com newer_than:30d {facture devis}"
    plan = build_gmail_search_query(query)
    assert plan.gmail_query == query
    assert plan.interpreted is False
    assert plan.free_terms == ("facture", "devis")

    negative = build_gmail_search_query("-has:attachment facture")
    assert negative.gmail_query == "-has:attachment facture"
    assert negative.interpreted is False


def test_les_filtres_structures_sont_cumules_et_les_dates_validees():
    plan = build_gmail_search_query("contrat", {
        "from": "Alice Martin",
        "after": "01/08/2026",
        "before": "2026-08-16",
        "filename": "pdf",
        "unread": True,
        "scope": "inbox",
    })
    assert plan.gmail_query == (
        'contrat from:"Alice Martin" filename:"pdf" after:2026/08/01 '
        "before:2026/08/16 is:unread in:inbox"
    )


class _SearchMessagesApi:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []
        self._pages = {
            "": {"messages": [{"id": "m-old"}], "nextPageToken": "p2",
                 "resultSizeEstimate": 3},
            "p2": {"messages": [{"id": "m-match"}, {"id": "m-other"}],
                   "resultSizeEstimate": 3},
        }

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Exec(self._pages[kwargs.get("pageToken", "")])

    def get(self, **kwargs):
        msg_id = kwargs["id"]
        self.get_calls.append(kwargs)
        subjects = {
            "m-old": "Archive", "m-match": "Contrat Alpha urgent",
            "m-other": "Divers",
        }
        dates = {
            "m-old": "1 Aug 2026 10:00:00 +0000",
            "m-match": "14 Aug 2026 10:00:00 +0000",
            "m-other": "15 Aug 2026 10:00:00 +0000",
        }
        return _Exec({
            "id": msg_id, "threadId": "t-" + msg_id, "labelIds": ["INBOX"],
            "snippet": "alpha" if msg_id == "m-match" else "sans rapport",
            "payload": {"headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "Subject", "value": subjects[msg_id]},
                {"name": "Date", "value": dates[msg_id]},
            ]},
        })


def test_la_recherche_pagine_classe_et_expose_un_diagnostic(tmp_path):
    api = _SearchMessagesApi()
    service = _service_with(api, tmp_path)

    results = service.search_emails("alpha", max_results=2, include_spam_trash=True)

    assert [item["id"] for item in results] == ["m-match", "m-other"]
    assert len(api.list_calls) == 2
    assert api.list_calls[0]["includeSpamTrash"] is True
    assert service.last_search_info["gmail_query"] == "alpha"
    assert service.last_search_info["result_size_estimate"] == 3
    assert service.last_search_info["candidates_checked"] == 3


def test_laction_transmet_tous_les_filtres_avances(monkeypatch):
    class _Advanced:
        last_search_info = {
            "gmail_query": 'facture from:"alice@example.com" is:unread',
            "interpreted": True,
            "result_size_estimate": 1,
        }

        def search_emails(self, query, max_results=10, **kwargs):
            assert query == "facture"
            assert kwargs["filters"]["from"] == "alice@example.com"
            assert kwargs["filters"]["unread"] is True
            assert kwargs["include_spam_trash"] is True
            return [{
                "id": "m-1", "sender": "Alice", "subject": "Facture",
                "date": "15 Aug", "snippet": "Facture août", "unread": True,
                "attachments": [],
            }]

    monkeypatch.setattr(email_action, "get_gmail_service", lambda: _Advanced())
    result = email_action.email_control({
        "action": "advanced_search", "query": "facture",
        "from": "alice@example.com", "unread": True,
        "include_spam_trash": True,
    })
    assert "Interprétation Gmail" in result
    assert "Alice" in result


# ── veille temps réel ───────────────────────────────────────────────────────

class _FakeApi:
    """Imite l'API Gmail : profil, historique et lecture de message."""

    def __init__(self, history=None, history_id="100", fail=None):
        self._history = history or []
        self._history_id = history_id
        self._fail = fail
        self.history_calls = []
        self.fetched = []

    # google-api-python-client s'utilise en chaîne d'appels.
    def users(self):
        return self

    def getProfile(self, userId):  # noqa: N802 - nom imposé par l'API
        return _Exec({"emailAddress": "moi@example.com",
                      "historyId": self._history_id})

    def history(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.history_calls.append(kwargs)
        if self._fail:
            raise self._fail
        return _Exec({"historyId": "205", "history": self._history})

    def get(self, userId, id, format=None, metadataHeaders=None):  # noqa: A002
        self.fetched.append(id)
        return _Exec({
            "id": id, "threadId": "t", "labelIds": ["UNREAD"],
            "snippet": f"extrait de {id}",
            "payload": {"headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "Subject", "value": "Urgent"},
                {"name": "Date", "value": "15 Aug"},
            ]},
        })


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def _added(msg_id, labels=("UNREAD", "INBOX")):
    return {"messagesAdded": [{"message": {"id": msg_id, "labelIds": list(labels)}}]}


def _service_with(api, tmp_path):
    service = GmailService(
        token_file=tmp_path / "token.json",
        client_secret_file=tmp_path / "client.json",
    )
    service._service = api
    return service


def test_le_premier_passage_ne_rejoue_pas_la_boite(tmp_path):
    """Annoncer d'un coup tous les non-lus au démarrage serait insupportable."""
    api = _FakeApi(history=[_added("m-1")])
    service = _service_with(api, tmp_path)

    assert service.new_unread() == []
    assert api.history_calls == [], "l'historique a été interrogé sans point de départ"
    assert service._history_id == "100"


def test_un_nouveau_message_est_signale_une_seule_fois(tmp_path):
    api = _FakeApi(history=[_added("m-1")])
    service = _service_with(api, tmp_path)
    service.new_unread()                       # établit le point de départ

    first = service.new_unread()
    assert [m["subject"] for m in first] == ["Urgent"]
    assert first[0]["sender"] == "Alice <alice@example.com>"

    # Gmail peut renvoyer le même identifiant dans la fenêtre suivante.
    assert service.new_unread() == []


def test_les_messages_deja_lus_ou_jetes_ne_sont_pas_annonces(tmp_path):
    api = _FakeApi(history=[
        _added("lu", labels=("INBOX",)),
        _added("corbeille", labels=("UNREAD", "TRASH")),
        _added("spam", labels=("UNREAD", "SPAM")),
    ])
    service = _service_with(api, tmp_path)
    service.new_unread()
    assert service.new_unread() == []
    assert api.fetched == []


def test_un_historique_expire_repart_dun_point_neuf(tmp_path):
    """Après une longue coupure, Gmail répond 404 : ne pas rejouer la boîte."""
    api = _FakeApi(fail=Exception("HttpError 404 startHistoryId trop ancien"))
    service = _service_with(api, tmp_path)
    service._history_id = "1"

    assert service.new_unread() == []
    assert service._history_id == "100"


def test_la_memoire_des_messages_vus_reste_bornee(tmp_path):
    from core.email_service import _SEEN_LIMIT

    service = _service_with(_FakeApi(), tmp_path)
    for index in range(_SEEN_LIMIT + 120):
        service._mark_seen(f"m-{index}")
    assert len(service._seen) == _SEEN_LIMIT
    # Ce sont bien les plus anciens qui sont tombés.
    assert "m-0" not in service._seen
    assert f"m-{_SEEN_LIMIT + 119}" in service._seen


def test_la_veille_ne_demarre_quune_fois(tmp_path):
    service = _service_with(_FakeApi(), tmp_path)
    try:
        assert service.start_polling(lambda mail: None, interval_sec=999) is True
        assert service.start_polling(lambda mail: None, interval_sec=999) is False
        assert service.watching is True
    finally:
        service.stop_polling()


def test_la_veille_appelle_le_rappel_pour_chaque_nouveau_message(tmp_path):
    import threading

    api = _FakeApi(history=[_added("m-7")])
    service = _service_with(api, tmp_path)
    service._history_id = "100"          # point de départ déjà établi
    received = []
    seen = threading.Event()

    def _on_mail(mail):
        received.append(mail)
        seen.set()

    service.start_polling(_on_mail, interval_sec=10)
    try:
        assert seen.wait(3.0), "aucun message annoncé en trois secondes"
        assert received[0]["subject"] == "Urgent"
    finally:
        service.stop_polling()


def test_une_autorisation_absente_arrete_la_veille(tmp_path):
    """Boucler sur une erreur d'autorisation ne ferait que polluer les logs."""
    import threading

    service = _service_with(None, tmp_path)
    stopped = threading.Event()

    def _explode():
        stopped.set()
        raise GmailSetupRequired("autorisation absente")

    service.new_unread = _explode
    service.start_polling(lambda mail: None, interval_sec=10)
    assert stopped.wait(3.0)
    for _ in range(30):
        if not service.watching:
            break
        time.sleep(0.05)
    assert service.watching is False


def test_oauth_chrome_ne_depend_pas_du_navigateur_par_defaut(monkeypatch):
    import webbrowser
    from unittest.mock import Mock
    from core.email_service import _register_gmail_chrome

    monkeypatch.setattr(webbrowser, "_browsers", {})
    monkeypatch.setattr(webbrowser, "_tryorder", [])
    monkeypatch.setattr("core.email_service.shutil.which",
                        lambda name: "/opt/Google Chrome/chrome" if name == "google-chrome-stable" else None)
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr("subprocess.Popen", launch)
    controller = webbrowser.get(_register_gmail_chrome())
    url = "https://accounts.google.com/o/oauth2/auth?state=test&scope=gmail"
    assert controller.open(url, new=1)
    assert launch.call_args.args[0] == ["/opt/Google Chrome/chrome", url]


def test_echec_lancement_chrome_ne_reste_pas_en_attente_oauth(monkeypatch):
    import pytest
    import webbrowser
    from core.email_service import GmailError, _register_gmail_chrome

    monkeypatch.setattr("core.email_service.shutil.which", lambda name: "/usr/bin/google-chrome")
    monkeypatch.setattr(webbrowser.BackgroundBrowser, "open", lambda *a, **kw: False)
    with pytest.raises(GmailError, match="clé OAuth est conservée"):
        webbrowser.get(_register_gmail_chrome()).open("https://accounts.google.com/")


def test_reconnexion_reutilise_la_cle_et_ouvre_chrome(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from core.email_service import GmailService

    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}))
    service = GmailService(token_file=tmp_path / "token.json", client_secret_file=client)
    expired = Mock(expired=True, refresh_token="old", valid=False)
    expired.refresh.side_effect = RuntimeError("invalid_grant")
    fresh = Mock()
    fresh.to_json.return_value = '{"token":"new"}'
    monkeypatch.setattr(service, "_load_credentials", lambda: expired)
    monkeypatch.setattr(service, "dependencies_available", lambda: True)
    monkeypatch.setattr("core.email_service._register_gmail_chrome", lambda: "anogpt-gmail-chrome")
    flow = Mock()
    flow.run_local_server.return_value = fresh
    factory = Mock(return_value=flow)
    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", factory)
    api = Mock()
    api.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "test@example.com"}
    monkeypatch.setattr("googleapiclient.discovery.build", Mock(return_value=api))
    assert service.connect(interactive=True)["emailAddress"] == "test@example.com"
    assert factory.call_args.args[0] == str(client)
    kwargs = flow.run_local_server.call_args.kwargs
    assert kwargs["browser"] == "anogpt-gmail-chrome"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["open_browser"] is True
    assert kwargs["timeout_seconds"] == 180
    assert "{url}" not in kwargs["authorization_prompt_message"]
    assert stat.S_IMODE(service.token_file.stat().st_mode) == 0o600


def test_cle_installee_ne_redonne_pas_le_tutoriel_google_cloud(tmp_path):
    service = GmailService(token_file=tmp_path / "token.json", client_secret_file=tmp_path / "client.json")
    service.client_secret_file.write_text("{}")
    assert "connecte Gmail" in service._setup_message()
    assert service.setup_steps() == [
        "Dites « connecte Gmail » pour ouvrir l’autorisation dans Google Chrome."
    ]


def test_lecture_sans_autorisation_ne_lance_pas_chrome(tmp_path, monkeypatch):
    import pytest
    from unittest.mock import Mock

    service = GmailService(token_file=tmp_path / "token.json", client_secret_file=tmp_path / "client.json")
    service.client_secret_file.write_text("{}")
    register = Mock()
    monkeypatch.setattr("core.email_service._register_gmail_chrome", register)
    with pytest.raises(GmailSetupRequired, match="connecte Gmail"):
        service.connect(interactive=False)
    register.assert_not_called()
