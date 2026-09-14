# Preuves et validation — audit ANO-GPT du 13 septembre 2026

[Roadmap principale](../../ROADMAP_ANO_GPT_ULTIME_2026-09-13.md) · [Inventaire](INVENTAIRE.md)

## Conditions

- Code de référence : `30a094f`.
- Interpréteur : Python 3.14.7 ; Ruff 0.16.7.
- Copie de travail : `/tmp/anogpt-audit-20260913-rpIfs8`, créée à partir des fichiers suivis présents dans le worktree.
- Configuration personnelle et bases non suivies non copiées ; aucun démarrage du programme principal.
- Qt hors écran : `QT_QPA_PLATFORM=offscreen`.
- La modification préexistante de `config/azure_catalog.json` est conservée ; les fichiers applicatifs du dépôt original n'ont pas été corrigés.
- Les sondes utilisent des objets factices et `TestClient` : aucun serveur ouvert sur le réseau, aucune vraie session distante, aucun SMS envoyé.
- Les logs historiques servent de signaux : ils ne sont pas attribuables automatiquement à la version courante.

Le dossier temporaire est conservé pour vérification immédiate ; il peut disparaître au nettoyage de la machine. Les résultats utiles et le code de reproduction sont donc conservés ici. Ne pas publier de copie brute des journaux personnels.

## Contrôles réalisés

| Commande / contrôle | Résultat |
|---|---|
| `ruff --version` | 0.16.7 |
| `ruff check .` | All checks passed |
| `ruff check --extend-select B,ASYNC,RUF006,RUF012,RUF013,RUF059,PLW1510,PGH003,SIM,UP --statistics .` | 2 526 signalements, code 1 attendu pour ce contrôle élargi |
| `QT_QPA_PLATFORM=offscreen timeout 900s python -m pytest -q --disable-warnings --tb=short` dans la copie | 5 failed, 1663 passed, 23 skipped, 1 xfailed, 4 warnings in 104.01s |
| Analyse AST des fichiers Python suivis | 442 fichiers parsés, 158 835 lignes |
| `flutter pub get --offline` dans la copie mobile | Dépendances résolues depuis le cache ; aucune mise à jour système |
| `flutter analyze --no-pub` | No issues found, 8.3 s |
| `flutter test --no-pub --reporter expanded` | 20 tests passent |
| `python -m pip check` sur l'interpréteur de l'audit | Conflits de l'environnement global, détaillés dans la roadmap |
| Sondes Python ci-dessous | Résultats décrits dans la table suivante |

Le lint du dépôt a été utilisé selon la compétence `ruff-quality`, en diagnostic uniquement : aucun `--fix`, aucune reformatation. Les règles élargies produisent surtout des signalements de style (UP006, UP045, etc.). Leur total ne doit jamais être affiché comme un nombre de bugs applicatifs.

### Échecs Python exacts

```text
FAILED tests/test_error_visibility.py::test_la_dette_de_silence_ne_grandit_pas
FAILED tests/test_face_memory.py::test_tool_identify_then_remember_from_pending
FAILED tests/test_face_memory.py::test_tool_object_path_uses_gemini_search_and_personal_memory
FAILED tests/test_interrupt_scope.py::test_le_budget_de_coupure_audio_reste_sous_100_ms
FAILED tests/test_voice_selection.py::test_un_changement_de_voix_declenche_la_reconnexion
```

Les 23 tests ignorés et le xfail ne prouvent pas le fonctionnement de leurs capacités. Aucun pourcentage de couverture de lignes n'a été mesuré. Le build Android/Kotlin n'a pas été réalisé : les tests Flutter ne le remplacent pas.

## Reproductions supplémentaires

| Fiche | Montage non destructif | Résultat observé |
|---|---|---|
| C01 | Consommateur réel, faux transport ; PCM vieux de 30 s, ancienne époque, hôte en parole | **1 paquet envoyé** malgré les trois motifs de rejet |
| C02 | Appel réel `_send_phone_sms`, faux téléphone, attente ramenée de 40 s à 80 ms | Timeout après ≈ 82 ms ; opération non démarrée pendant l'attente puis exécutée après |
| C03 | Même appel, `auto_reply_authorized=True`, fonction de confirmation espionnée | **0 appel à la barrière de confirmation** |
| C04 | Routes réelles, coordonnées fictives, aucune authentification | Position rendue avec HTTP 200 ; navigation inactive rendue avec HTTP 200 |
| C05 | Faux appareil et faux token, révocation puis commande fictive | Révocation 200 ; **ancien token accepté, commande en file** |
| C10 | Appel direct `_on_live_voice_change` avec voix différente | `AttributeError: module 'main' has no attribute 'save_live_voice'` |
| C18 | JSON `[]` et `42` aux routes login/pair/device-login | **HTTP 500** ; `device_token: 42` produit également 500 sur device-login |
| C21 | Un fichier fictif indexé, puis extraction vide simulée avec `force=True` | Retour `(False, 0, 0)`, **fragment précédent toujours présent** |
| C23 | Nouvelle connexion après initialisation des bases RAG et FTS fictives | `PRAGMA synchronous = 2` dans les deux cas |

La sonde C01 prouve l'absence de filtre au consommateur ; elle n'affirme pas qu'un micro réel a capté et transmis l'écho d'ANO pendant cet audit. La sonde C02 raccourcit volontairement le timeout pour éviter 40 secondes d'attente : les primitives et l'ordre d'exécution sont conservés. La sonde C21 isole le cas « extraction réussie sans fragments » ; les autres motifs d'échec d'extraction doivent rester distincts dans la correction.

## Rejouer dans une copie isolée

Depuis le dépôt original, créer une nouvelle copie des fichiers suivis et lancer d'abord la suite, avant d'ajouter des scripts de sondes au répertoire : certains tests recensent les fichiers Python du dossier.

```bash
audit_workspace=$(mktemp -d /tmp/anogpt-audit-replay-XXXXXX)
git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$audit_workspace"
cd "$audit_workspace"
QT_QPA_PLATFORM=offscreen timeout 900s python -m pytest -q --disable-warnings --tb=short
```

Ne pas copier les clés API ou les bases personnelles. Utiliser un environnement applicatif dont les dépendances sont installées. Les résultats exacts dépendent de la version du dépôt ; les valeurs ci-dessus correspondent à la photographie auditée.

Dans cette copie uniquement, créer `probe_doc.txt` avec le texte suivant :

```text
Cette note fictive sert seulement au diagnostic local de l'index documentaire.
Le code d'inventaire fictif est ALPHA42.
```

Créer ensuite `audit_probes.py` avec le script ci-dessous et l'exécuter avec `QT_QPA_PLATFORM=offscreen python audit_probes.py`. Il crée seulement deux bases fictives près du script et utilise les routes en mémoire ; la commande fictive déposée en file n'est consommée par aucun assistant.

```python
"""Reproductions non destructives, données fictives, application non démarrée."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from dashboard import server
from core import geolocation, navigation, human_confirmation
from core.tool_dispatcher import ToolDispatcher
from core.session_manager import SessionManager
from core.personal_rag import PersonalRAG
from core.file_indexer import PersonalFileIndexer
from pathlib import Path


def main():
    findings = {}
    with patch.object(server, '_local_ip', return_value='192.168.1.2'), patch.object(server, '_load_devices', return_value={}), patch.object(server, '_save_devices'):
        dashboard = server.DashboardServer()
        dashboard._tokens.add('fake-session')
        dashboard._device_sessions['fake-device'] = {'session_key': 'ABCDEF'}
        with TestClient(dashboard.app, raise_server_exceptions=False) as client:
            with patch.object(geolocation, 'get_live_position', return_value={'lat': 1.0, 'lon': 2.0}), patch.object(navigation, 'get_navigation_manager', return_value=SimpleNamespace(current_session=None)):
                for route in ('/api/live_position', '/api/navigation/state'):
                    response = client.get(route)
                    findings[route] = {'unauthenticated_status': response.status_code, 'body': response.json()}
            headers = {'Authorization': 'Bearer fake-session'}
            revoked = client.post('/api/revoke-devices', headers=headers)
            response = client.post('/api/command', headers=headers, json={'text': 'AUDIT FICTIF'})
            findings['revoke'] = {'revoke_status': revoked.status_code, 'old_session_command_status': response.status_code, 'queue_size': dashboard._command_queue.qsize()}
            for route in ('/login', '/api/pair', '/api/device-login'):
                findings[route] = {str(payload): client.post(route, json=payload).status_code for payload in ([], 42, {'device_token': 42})}

    async def sms_probe():
        calls = []
        async def request(*args):
            calls.append('executed')
            return {'ok': True, 'message': 'simulation'}
        real_submit = asyncio.run_coroutine_threadsafe
        class ShortFuture:
            def __init__(self, future):
                self.future = future
            def result(self, timeout=None):
                return self.future.result(timeout=0.08)
        def submit(coro, loop):
            return ShortFuture(real_submit(coro, loop))
        host = SimpleNamespace(_dashboard=SimpleNamespace(request_phone_sms=request))
        start = time.monotonic()
        with patch('core.tool_dispatcher.asyncio.run_coroutine_threadsafe', side_effect=submit), patch.object(human_confirmation, 'request') as confirm:
            try:
                await ToolDispatcher._send_phone_sms(host, {'target': 'FAKE', 'body': 'AUDIT', 'auto_reply_authorized': True})
                outcome = 'returned'
            except TimeoutError:
                outcome = 'TimeoutError'
            findings['sms'] = {'outcome_with_shortened_80ms_wait': outcome, 'elapsed_ms': round((time.monotonic()-start)*1000), 'confirmation_calls': confirm.call_count, 'request_executed_before_timeout': len(calls)}
            await asyncio.sleep(0.01)
            findings['sms']['request_executed_after_timeout'] = len(calls)
    asyncio.run(sms_probe())

    async def audio_probe():
        sent = []
        class Captions:
            def __init__(self, host): pass
            def start(self): pass
            def push(self, msg): pass
            async def close(self): pass
        async def send(**kwargs):
            sent.append(kwargs)
        host = SimpleNamespace(out_queue=asyncio.Queue(), session=SimpleNamespace(send_realtime_input=send), _is_speaking=True, _speech_output_epoch=2)
        await host.out_queue.put({'data': b'FAKE_PCM', 'mime_type': 'audio/pcm;rate=16000', '_captured_at': time.monotonic()-30, '_audio_epoch': 1})
        with patch('core.live_captions.LiveCaptions', Captions):
            task = asyncio.create_task(SessionManager._send_realtime(host))
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        findings['queued_audio'] = {'speaking': True, 'old_epoch': True, 'age_seconds': 30, 'audio_packets_sent': len(sent)}
    asyncio.run(audio_probe())

    host = SimpleNamespace(_live_voice='Charon')
    try:
        SessionManager._on_live_voice_change(host, 'Kore')
    except Exception as exc:
        findings['voice_change'] = {'exception': type(exc).__name__, 'message': str(exc)}

    rag = PersonalRAG(db_path=Path(__file__).parent/'probe_rag.db', roots=[Path(__file__).parent])
    fixture = Path(__file__).parent/'probe_doc.txt'
    rag.index_file(fixture)
    before = rag.storage.count_chunks()
    with patch.object(rag.txt_extractor, 'extract_chunks', return_value=[]):
        result = rag.index_file(fixture, force=True)
    findings['rag_empty'] = {'chunks_before': before, 'empty_extraction_result': result, 'chunks_after': rag.storage.count_chunks()}
    for label, storage in [('rag', rag.storage), ('file_index', PersonalFileIndexer(db_path=Path(__file__).parent/'probe_index.db'))]:
        conn = storage._get_connection()
        findings[label+'_synchronous'] = conn.execute('PRAGMA synchronous').fetchone()[0]
        conn.close()
    print(json.dumps(findings, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
```

## Lecture des journaux historiques

Le relevé agrégé a lu uniquement les fichiers locaux existants :

- `logs/anogpt.jsonl` : 1 043 événements, du 9 au 13 septembre 2026 lors du relevé.
- `logs/freeze-*.txt` : 20 rapports datés du 13 septembre présents.
- `memory/tool_usage.jsonl` : 1 240 appels, du 13 août au 13 septembre 2026.

Le p95 historique a été obtenu par tri des durées `ms` et sélection de l'index `min(n-1, floor(0.95*n))`. Il dépend donc de cette convention, du petit nombre d'appels de certains outils et du mélange de versions. Il ne constitue pas un benchmark de la version actuelle.

Une seconde statistique AST ad hoc comptait 710 handlers constitués de `pass` ou `continue` dans les Python suivis hors tests. **Elle utilise une définition et un périmètre différents des 604 handlers muets du test officiel** : ces deux valeurs ne doivent pas être comparées comme une évolution. La roadmap retient le compteur du test du dépôt.

## Appuis techniques externes vérifiés

Les constats de bugs viennent du code local et des sondes. Les références ci-dessous servent à confirmer des contrats techniques, sans remplacer ces preuves.

- La programmation d'une coroutine depuis un autre thread et les délais asynchrones sont décrits dans [la documentation Python asyncio](https://docs.python.org/3/library/asyncio-task.html#scheduling-from-other-threads). Le blocage C02 est une déduction du code local, puis une reproduction.
- Les callbacks audio doivent respecter les contraintes de temps réel et éviter les opérations imprévisibles : [contrat des streams sounddevice](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html).
- Authentification, contrôle continu des sessions, limitation de taille et révocation des WebSockets : [guide OWASP WebSocket Security](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html).
- Sauvegarde cohérente d'une base active : [SQLite Online Backup API](https://www.sqlite.org/backup.html).
- Envoi SMS, callbacks et messages multipart : [Android SmsManager](https://developer.android.com/reference/android/telephony/SmsManager).
- Risque des mises à niveau partielles : [ArchWiki — System maintenance](https://wiki.archlinux.org/title/System_maintenance#Partial_upgrades_are_unsupported). La page directe présentait une protection anti-robot ; le passage pertinent a été confirmé dans son résultat indexé.

Aucun prix, débit commercial de modèle ou taux de reconnaissance fournisseur n'est présenté comme mesuré ou garanti dans ce rapport.

