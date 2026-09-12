"""Action vocale/MCP du carnet de contacts."""

from __future__ import annotations

from core.contacts import ContactAmbiguous, ContactError, get_contacts_book

from core import action_kit as kit


def _format(rows: list[dict]) -> str:
    if not rows:
        return "Aucun contact enregistré."
    lines = []
    for row in rows:
        details = []
        if row.get("emails"):
            details.append(", ".join(row["emails"]))
        if row.get("phone"):
            details.append(row["phone"])
        if row.get("handles"):
            details.extend(f"{k}: {v}" for k, v in row["handles"].items())
        lines.append(f"- {row.get('name')}" + (f" — {' · '.join(details)}" if details else ""))
    return "Contacts :\n" + "\n".join(lines)


@kit.action("contacts_control")
def contacts_control(parameters: dict | None = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list") or "list").strip().casefold()
    book = get_contacts_book()
    try:
        if action in {"list", "liste"}:
            return _format(book.list())
        if action in {"search", "find", "recherche"}:
            return _format(book.find(str(params.get("query", ""))))
        if action in {"add", "create", "save", "update", "ajouter", "modifier"}:
            handles = dict(params.get("handles") or {})
            for platform in ("whatsapp", "telegram", "signal", "discord", "instagram", "messenger"):
                if params.get(platform):
                    handles[platform] = params[platform]
            contact = book.save(
                name=params.get("name", ""), contact_id=params.get("id", ""),
                aliases=(params.get("aliases") or None),
                emails=(params.get("emails") or None),
                phone=(params.get("phone") or None), handles=(handles or None),
                notes=(params.get("notes") or None),
            )
            try:
                from core.knowledge_graph import upsert_contact

                upsert_contact(contact)
            except Exception:
                pass
            return f"Contact enregistré : {contact['name']} ({contact['id']})."
        if action in {"remove_email", "remove_alias", "remove_phone", "retirer"}:
            target = str(params.get("id") or params.get("name") or params.get("query") or "")
            matches = book.find(target)
            if len(matches) != 1:
                return (f"Contact introuvable : {target}." if not matches
                        else f"Plusieurs contacts correspondent à « {target} » : précisez.")
            row = matches[0]
            value = str(params.get("value") or params.get("email") or params.get("alias") or "").strip()
            if action == "remove_phone" or (not value and params.get("phone")):
                book.save(contact_id=row["id"], phone="")
                return f"Téléphone retiré pour {row['name']}."
            if not value:
                return "Précisez la valeur à retirer."
            field = "emails" if ("@" in value or action == "remove_email") else "aliases"
            kept = [v for v in row.get(field) or [] if v.casefold() != value.casefold()]
            if len(kept) == len(row.get(field) or []):
                return f"« {value} » n'est pas enregistré pour {row['name']}."
            book.replace_field(row["id"], field, kept)
            return f"« {value} » retiré de {row['name']}."
        if action in {"delete", "remove", "supprimer"}:
            value = str(params.get("id") or params.get("query") or params.get("name") or "")
            if not value:
                return "Précisez le contact à supprimer."
            matches = book.find(value)
            deleted = book.delete(value)
            if deleted:
                try:
                    from core.knowledge_graph import remove_source

                    for contact in matches:
                        remove_source("person", str(contact.get("id") or ""))
                except Exception:
                    pass
            return (f"Contact supprimé : {value}." if deleted
                    else f"Contact introuvable : {value}.")
        return ("Action contact inconnue. Actions : list, search, add, update "
                "(fusionne e-mails et surnoms), remove_email, remove_alias, remove_phone, delete.")
    except (ContactError, ContactAmbiguous) as exc:
        return f"Erreur contact : {exc}"
