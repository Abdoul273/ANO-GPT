"""Action vocale pour Notion, Figma et les surfaces Google AI officielles."""
from __future__ import annotations

from core.cloud_integrations import CloudIntegrationError, get_cloud_integrations

from core import action_kit as kit


@kit.action("cloud_integrations_control")
def cloud_integrations_control(parameters: dict | None = None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().casefold()
    service = str(params.get("service") or "").strip().casefold()
    if not service:
        return "Précisez le service : calendar, notion, figma, figma_make, notebooklm, gemini ou stitch."
    api = get_cloud_integrations()
    try:
        if action in {"status", "diagnostic"}:
            result = api.status(service)
            return result.message + (f"\nProchaine étape : {result.next_step}" if result.next_step else "")
        if action in {"connect", "login", "authorize"}:
            return api.connect(service)
        if action in {"open", "launch"}:
            return api.open(service)
        if service == "notion" and action in {"search", "find"}:
            return api.notion_search(str(params.get("query") or ""))
        if service == "notion" and action in {"create_note", "note", "create"}:
            title = str(params.get("title") or params.get("query") or "Note ANO-GPT")
            content = str(params.get("content") or params.get("query") or title)
            return api.notion_create_note(title, content, str(params.get("parent_id") or ""))
        if service == "figma" and action in {"file", "inspect", "read"}:
            return api.figma_file(str(params.get("file_key") or ""))
        return "Action inconnue. Actions : status, connect, open ; Notion : search, create_note ; Figma : inspect."
    except CloudIntegrationError as exc:
        return f"Erreur intégration : {exc}"
