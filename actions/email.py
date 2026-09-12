"""Action Gmail : diagnostic, connexion, liste, recherche et lecture."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

from core.contacts import ContactError, get_contacts_book

from core import action_kit as kit

try:
    from core.email_service import GmailError, GmailSetupRequired, get_gmail_service
    _HAS_GMAIL = True
except ImportError:
    _HAS_GMAIL = False


def _remember_results(session_memory: dict | None, emails: List[Dict[str, Any]]) -> None:
    if session_memory is not None:
        session_memory["email_results"] = [m.get("id", "") for m in emails]
    # Gmail reste la source de vérité : le graphe ne conserve qu'un extrait
    # des messages effectivement consultés par ANO-GPT.
    try:
        from core.knowledge_graph import upsert_source

        for message in emails:
            message_id = str(message.get("id") or "").strip()
            if not message_id:
                continue
            happened = ""
            raw_internal = message.get("internal_date")
            try:
                if raw_internal:
                    happened = datetime.fromtimestamp(
                        int(raw_internal) / 1000, timezone.utc
                    ).isoformat(timespec="seconds")
                elif message.get("date"):
                    happened = parsedate_to_datetime(
                        str(message["date"])
                    ).isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError):
                happened = str(message.get("date") or "")
            sender = str(message.get("sender") or "")
            recipients = str(message.get("to") or "")
            subject = str(message.get("subject") or "Sans objet")
            body = str(message.get("body") or message.get("snippet") or "")
            upsert_source(
                kind="email", external_id=message_id, title=subject,
                content=(f"De : {sender}\nÀ : {recipients}\n"
                         f"Objet : {subject}\n{body}"),
                aliases=f"{sender} {recipients}", happened=happened,
                metadata={"thread_id": str(message.get("thread_id") or "")},
            )
    except Exception:
        # Une indisponibilité de l'index ne doit jamais masquer les e-mails.
        pass


def _resolve_message_id(value: str, session_memory: dict | None) -> str:
    value = (value or "").strip()
    if value.isdigit() and session_memory is not None:
        results = session_memory.get("email_results", [])
        index = int(value) - 1
        if 0 <= index < len(results):
            return results[index]
    return value


def _format_messages(emails: List[Dict[str, Any]], title: str, ui=None) -> str:
    lines = [title]
    for index, message in enumerate(emails, 1):
        unread = " • non lu" if message.get("unread") else ""
        attachment = " 📎" if message.get("attachments") else ""
        lines.append(
            f"{index}. De : {message.get('sender', 'Inconnu')}{unread}\n"
            f"   Objet : {message.get('subject', 'Sans objet')}{attachment}\n"
            f"   Date : {message.get('date', '')}\n"
            f"   Extrait : {message.get('snippet', '')[:180]}"
        )
        if ui and hasattr(ui, "show_card"):
            try:
                ui.show_card(
                    "message", f"✉️ {message.get('sender', 'Inconnu')[:36]}",
                    f"**{message.get('subject', 'Sans objet')}**\n\n{message.get('snippet', '')}",
                    [{"label": f"Lire le {index}", "primary": True}],
                )
            except Exception:
                pass
    return "\n\n".join(lines)


@kit.action("email_control")
def email_control(parameters: dict = None, session_memory=None, ui=None) -> str:
    """Pilote Gmail sans jamais masquer une erreur de connexion en boîte vide."""
    params = parameters or {}
    action = str(params.get("action", "unread") or "unread").strip().lower()
    query = str(params.get("query", "") or "").strip()
    msg_id = str(params.get("id", "") or "").strip()
    try:
        max_results = max(1, min(int(params.get("max_results", 10) or 10), 100))
    except (TypeError, ValueError):
        max_results = 10

    if not _HAS_GMAIL:
        return (
            "Le module Gmail n'est pas installé. Exécutez pip install "
            "google-api-python-client google-auth google-auth-oauthlib."
        )

    service = get_gmail_service()
    try:
        if action in {"status", "diagnostic"}:
            status = service.status(verify=True)
            if status.authenticated:
                account = f" pour {status.account}" if status.account else ""
                watch = (" La veille des nouveaux messages est active."
                         if getattr(service, "watching", False) else "")
                return f"Gmail est connecté et opérationnel{account}.{watch}"
            # Toujours dire quoi faire ensuite : « la configuration n'est pas
            # terminée » sans la suite est la réponse la plus inutile possible.
            if status.next_step:
                return f"{status.message}\n\nProchaine étape : {status.next_step}"
            return status.message

        if action in {"setup", "configuration", "aide"}:
            status = service.status()
            if status.authenticated:
                return ("Gmail est déjà relié : rien à configurer. "
                        "Dites « lis mes e-mails non lus ».")
            steps = "\n".join(service.setup_steps())
            return f"{status.message}\n\nMarche à suivre :\n{steps}"

        if action in {"connect", "login", "authorize"}:
            client_secret_path = str(params.get("client_secret_path", "") or "").strip()
            if client_secret_path:
                service.configure_client_secret(client_secret_path)
            profile = service.connect(interactive=True)
            account = profile.get("emailAddress", "votre compte")
            return (f"Gmail est maintenant connecté pour {account}. "
                    "Je surveille la boîte : les nouveaux messages seront "
                    "annoncés dès leur arrivée.")

        if action in {"unread", "non_lus"}:
            emails = service.get_unread(max_results=max_results)
            if not emails:
                return "La connexion Gmail fonctionne. Aucun message non lu."
            _remember_results(session_memory, emails)
            return _format_messages(
                emails, f"Vous avez {len(emails)} message(s) non lu(s) :", ui
            )

        if action in {"recent", "inbox", "latest"}:
            emails = service.get_recent(max_results=max_results)
            if not emails:
                return "La connexion Gmail fonctionne, mais la boîte de réception est vide."
            _remember_results(session_memory, emails)
            return _format_messages(
                emails, f"{len(emails)} message(s) récent(s) :", ui
            )

        if action in {"search", "advanced_search", "recherche_avancee"}:
            filter_names = (
                "from", "to", "subject", "after", "before", "filename", "label",
                "scope", "has_attachment", "unread", "starred", "important",
                "larger_than", "smaller_than",
            )
            filters: Dict[str, Any] = {}
            for name in filter_names:
                if name not in params or params[name] in (None, ""):
                    continue
                if name in {"has_attachment", "starred", "important"} and params[name] is False:
                    continue
                filters[name] = params[name]
            # « les mails de Maman » peut utiliser l'adresse enregistrée sans
            # obliger l'utilisateur à la dicter.
            try:
                for contact_filter in ("from", "to"):
                    value = str(filters.get(contact_filter, "") or "").strip()
                    if value and "@" not in value:
                        resolved = get_contacts_book().resolve(value, "email")
                        if resolved:
                            filters[contact_filter] = resolved.value
            except ImportError:
                pass
            if not query and not filters:
                return "Précisez les mots, l'expéditeur ou la période à rechercher dans Gmail."
            include_spam_trash = bool(params.get("include_spam_trash", False))
            if filters or include_spam_trash:
                emails = service.search_emails(
                    query,
                    max_results=max_results,
                    filters=filters,
                    include_spam_trash=include_spam_trash,
                )
            else:
                # Compatibilité avec d'anciens fournisseurs Gmail et les tests
                # d'intégration qui n'acceptent que la signature historique.
                emails = service.search_emails(query, max_results=max_results)
            info = getattr(service, "last_search_info", {}) or {}
            shown_query = str(info.get("gmail_query") or query)
            if not emails:
                detail = f" Requête exécutée : `{shown_query}`." if shown_query else ""
                return (
                    "La recherche Gmail fonctionne, mais aucun message ne correspond."
                    + detail
                )
            _remember_results(session_memory, emails)
            estimate = info.get("result_size_estimate")
            count_text = f" sur environ {estimate}" if isinstance(estimate, int) and estimate > len(emails) else ""
            interpretation = ""
            if info.get("interpreted"):
                interpretation = f"\nInterprétation Gmail : `{shown_query}`"
            return _format_messages(
                emails,
                f"{len(emails)} résultat(s) Gmail{count_text} :{interpretation}",
                ui,
            )

        if action == "read":
            target = _resolve_message_id(msg_id or query, session_memory)
            if not target:
                return "Précisez l'identifiant du message ou dites par exemple « lis le 2 »."
            message = service.read_email(target)
            _remember_results(session_memory, [message])
            attachments = message.get("attachments", [])
            attachment_text = ""
            if attachments:
                names = ", ".join(a["filename"] for a in attachments)
                attachment_text = f"\n\nPièces jointes : {names}"
            return (
                f"De : {message['sender']}\n"
                f"Objet : {message['subject']}\n"
                f"Date : {message['date']}\n\n"
                f"{message['body']}{attachment_text}"
            )

        if action == "summary":
            emails = service.get_unread(max_results=max_results)
            if not emails:
                return "La connexion Gmail fonctionne. Aucun message non lu à résumer."
            _remember_results(session_memory, emails)
            return _format_messages(
                emails, f"Voici les {len(emails)} message(s) à résumer :", ui
            )

        return (
            "Action Gmail inconnue. Actions valides : status, setup, connect, "
            "unread, recent, search, advanced_search, read, summary."
        )
    except ContactError as exc:
        return f"Erreur contact : {exc}"
    except GmailSetupRequired as exc:
        return f"Configuration Gmail requise : {exc}"
    except GmailError as exc:
        return f"Erreur Gmail : {exc}"
    except Exception as exc:
        return f"Erreur Gmail inattendue : {exc}"


if __name__ == "__main__":
    print(email_control({"action": "status"}))
