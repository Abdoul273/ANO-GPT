"""Contrat PC ↔ ANO-Remote pour les appels Android (sans compiler Flutter)."""

import asyncio
from pathlib import Path

from dashboard.server import DashboardServer
from core.phone_numbers import spoken_number_to_digits
from main import TOOL_DECLARATIONS, _DESTRUCTIVE_TOOLS


ROOT = Path(__file__).resolve().parents[1]


class FakeAndroidSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(dict(payload))


def test_phone_call_est_un_outil_sensible_expose_au_modele():
    declaration = next(t for t in TOOL_DECLARATIONS if t["name"] == "phone_call")
    assert declaration["parameters"]["required"] == ["target"]
    assert "phone_call" in _DESTRUCTIVE_TOOLS
    assert "Android" in declaration["description"]
    assert "selection" in declaration["parameters"]["properties"]
    assert "deux derniers chiffres" in declaration["description"]


def test_manifest_demande_contacts_et_appels_explicitement():
    manifest = (ROOT / "mobile/ano_remote/android/app/src/main/AndroidManifest.xml").read_text()
    assert "android.permission.READ_CONTACTS" in manifest
    assert "android.permission.CALL_PHONE" in manifest
    assert "android.permission.RECEIVE_SMS" in manifest


def test_relais_sms_est_file_durable_et_independant_de_l_interface_flutter():
    receiver = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/SmsReceiver.kt").read_text()
    activity = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt").read_text()
    session = (ROOT / "mobile/ano_remote/lib/session.dart").read_text()
    assert 'putString("queue", queue.toString())' in receiver
    assert "List<Map<String, Any>>" in activity
    service = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/PhoneRelayService.kt").read_text()
    assert "PhoneRelayService.wake(context)" in receiver
    assert '"startPhoneRelay"' in activity
    assert "ano_remote_android_service" in service
    assert "phone_sms_received_ack" in service
    assert "startPhoneRelay" in session
    assert "sender_name" in receiver


def test_relais_en_arriere_plan_n_annonce_pas_un_sms_perdu_ni_un_appel_fictif():
    source = (ROOT / "dashboard/server.py").read_text()
    service = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/PhoneRelayService.kt").read_text()
    assert "_recent_sms_events" in source
    assert '"phone_sms_received_ack"' in source
    assert "removeSms(data.optString(\"event_id\"))" in service
    assert "Intent.ACTION_CALL" in service
    assert "SmsManager.getDefault().sendTextMessage" in service


def test_contrat_sms_entrant_demande_les_trois_modes_sans_envoi_implicite():
    source = (ROOT / "dashboard/server.py").read_text()
    prompt = (ROOT / "core/prompt.txt").read_text()
    assert '"type": "sms_received"' in source
    assert "1) l'utilisateur dicte" in source
    assert "2) tu proposes" in source
    assert "3) tu rédiges et envoies seulement" in source
    assert "N'envoie jamais de SMS avant ce choix explicite" in source
    assert "SMS ENTRANT" in prompt


def test_envoi_auto_reponse_est_ponctuel_et_exige_un_drapeau_explicite():
    source = (ROOT / "core/tool_dispatcher.py").read_text()
    assert "auto_reply_authorized" in source
    assert "if auto_reply_authorized:" in source
    assert "return human_confirmation.request(" in source


def test_module_android_applique_explicitement_le_plugin_kotlin():
    gradle = (ROOT / "mobile/ano_remote/android/app/build.gradle.kts").read_text()
    assert 'id("org.jetbrains.kotlin.android")' in gradle


def test_android_resout_le_contact_et_non_le_pc():
    source = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt").read_text()
    assert "ContactsContract.CommonDataKinds.Phone" in source
    assert "Intent.ACTION_CALL" in source
    assert "ACTION_DIAL" not in source


def test_android_connait_les_variantes_familiales_et_la_recherche_par_fin_de_numero():
    source = (ROOT / "mobile/ano_remote/android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt").read_text()
    assert "contactNameVariants" in source
    assert '"mum"' in source
    assert "findPhoneContactsBySuffix" in source
    assert "suffixLookup || resolved.size > 1" in source
    assert "foldContactText" in source
    assert "normalizeSpokenSuffix" in source


def test_nombres_dictes_et_accents_sont_normalises_sans_toucher_aux_noms():
    assert spoken_number_to_digits("soixante-dix") == "70"
    assert spoken_number_to_digits("quatre vingt onze") == "91"
    assert spoken_number_to_digits("soixante dix à la fin") == "70"
    assert spoken_number_to_digits("sept zéro") == "70"
    assert spoken_number_to_digits("Frère Aladji") == "Frère Aladji"


def test_recherche_telephone_transmet_la_fin_de_numero_dictee():
    async def scenario():
        server = DashboardServer()
        android = FakeAndroidSocket()
        server._phone_clients.add(android)
        task = asyncio.create_task(server.request_phone_contacts("soixante dix", timeout=1.0))
        for _ in range(20):
            if android.messages:
                break
            await asyncio.sleep(0.01)
        assert android.messages[-1]["query"] == "70"
        command = android.messages[-1]
        server._phone_call_waiters[command["request_id"]].set_result({"ok": True})
        await task

    asyncio.run(scenario())


def test_app_exige_opt_in_avant_appel_automatique():
    source = (ROOT / "mobile/ano_remote/lib/phone_link.dart").read_text()
    assert "automaticCallsEnabled" in source
    assert "requestPhonePermissions" in source
    assert "Activez les appels automatiques" in source


def test_serveur_refuse_si_anoremote_android_absent():
    async def scenario():
        server = DashboardServer()
        outcome = await server.request_phone_call("garage", timeout=0.01)
        assert outcome["status"] == "phone_offline"

    asyncio.run(scenario())


def test_serveur_ne_declenche_jamais_plusieurs_telephones():
    async def scenario():
        server = DashboardServer()
        server._phone_clients.update({FakeAndroidSocket(), FakeAndroidSocket()})
        outcome = await server.request_phone_call("garage", timeout=0.01)
        assert outcome["status"] == "multiple_phones"
        assert all(not phone.messages for phone in server._phone_clients)

    asyncio.run(scenario())


def test_commande_est_transitoire_et_retour_android_associe_par_id():
    async def scenario():
        server = DashboardServer()
        android = FakeAndroidSocket()
        server._phone_clients.add(android)
        task = asyncio.create_task(
            server.request_phone_call("garage", selection=2, timeout=1.0)
        )
        for _ in range(20):
            if android.messages:
                break
            await asyncio.sleep(0.01)
        assert len(android.messages) == 1
        command = android.messages[0]
        assert command["type"] == "phone_call"
        assert command["target"] == "garage"
        assert command["selection"] == 2
        # Une commande d'appel n'entre jamais dans l'historique rejoué.
        assert command not in server._history
        waiter = server._phone_call_waiters[command["request_id"]]
        waiter.set_result({
            "ok": True,
            "status": "started",
            "message": "Appel lancé vers Garage Central.",
        })
        outcome = await task
        assert outcome["ok"] is True
        assert not server._phone_call_waiters

    asyncio.run(scenario())


def test_sms_ne_part_jamais_du_pc():
    from actions.send_message import send_message

    answer = send_message({"platform": "SMS", "receiver": "Ma Maman",
                           "message_text": "Salut"})
    assert "phone_sms" in answer
    assert "MAUVAIS_OUTIL" in answer


def test_outils_telephone_exposes_au_modele():
    names = {t["name"] for t in TOOL_DECLARATIONS}
    assert {"phone_call", "phone_hangup", "phone_sms", "phone_contacts"} <= names
    assert {"phone_hangup", "phone_sms", "phone_contacts"} <= _DESTRUCTIVE_TOOLS


def test_sms_raccrochage_et_contacts_passent_par_le_meme_canal():
    async def scenario():
        server = DashboardServer()
        android = FakeAndroidSocket()
        server._phone_clients.add(android)

        task = asyncio.create_task(
            server.request_phone_sms("Ma Maman", "Salut", selection=2, timeout=1.0)
        )
        for _ in range(20):
            if android.messages:
                break
            await asyncio.sleep(0.01)
        command = android.messages[-1]
        assert command["type"] == "phone_sms_send"
        assert command["target"] == "Ma Maman"
        assert command["body"] == "Salut"
        assert command["selection"] == 2
        server._phone_call_waiters[command["request_id"]].set_result(
            {"ok": True, "message": "SMS envoyé à Ma Maman."}
        )
        assert (await task)["ok"] is True

        task = asyncio.create_task(server.request_phone_hangup(timeout=1.0))
        for _ in range(20):
            if android.messages[-1]["type"] == "phone_hangup":
                break
            await asyncio.sleep(0.01)
        command = android.messages[-1]
        server._phone_call_waiters[command["request_id"]].set_result(
            {"ok": True, "message": "Appel raccroché."}
        )
        assert (await task)["message"] == "Appel raccroché."

        task = asyncio.create_task(server.request_phone_contacts("97", timeout=1.0))
        for _ in range(20):
            if android.messages[-1]["type"] == "phone_contacts":
                break
            await asyncio.sleep(0.01)
        command = android.messages[-1]
        assert command["query"] == "97"
        server._phone_call_waiters[command["request_id"]].set_result(
            {"ok": True, "count": 4, "message": "4 contact(s) trouvé(s)."}
        )
        assert (await task)["count"] == 4
        assert not server._phone_call_waiters

    asyncio.run(scenario())


def test_sms_sans_texte_ne_derange_pas_le_telephone():
    async def scenario():
        server = DashboardServer()
        server._phone_clients.add(FakeAndroidSocket())
        outcome = await server.request_phone_sms("Ma Maman", "   ", timeout=0.01)
        assert outcome["status"] == "invalid_body"
        assert all(not phone.messages for phone in server._phone_clients)

    asyncio.run(scenario())
