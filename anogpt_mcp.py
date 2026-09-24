#!/usr/bin/env python3
"""anogpt_mcp.py — ANO-GPT vu comme un serveur MCP.

Branche ANO-GPT sur les agents qui parlent MCP : Antigravity (`agy`), Claude
Code, Codex CLI, Claude Desktop. L'agent devient le cerveau, ANO-GPT reste le
corps — sa carte, sa caméra, sa voix, sa boîte mail, sa machine.

    agy                      Claude Code / Codex / Desktop
      │                                │
      └────────── stdio (MCP) ─────────┘
                     │
              anogpt_mcp.py   ← ce fichier
                     │
        socket Unix (core/tool_bridge)
                     │
              ANO-GPT en cours d'exécution

Ce serveur ne contient aucune logique métier : il traduit des appels MCP en
appels sur le socket de contrôle. Les outils qui n'ont pas besoin de
l'interface graphique savent toutefois se débrouiller seuls quand
l'application est éteinte — un agent qui demande une pharmacie à trois heures
du matin mérite une réponse, même sans carte à l'écran.

Lancement direct (pour vérifier) :  python anogpt_mcp.py --selftest
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore
except ModuleNotFoundError:  # MCP SDK 2.x : FastMCP a été renommé
    from mcp.server.mcpserver import MCPServer as FastMCP

from core.agent_brain import LOOP_GUARD_ENV
from core.ghost_agent import GHOST_MODE_ENV
from core.tool_bridge import (
    AppUnavailable,
    ToolError,
    app_running,
    call_app,
)

mcp = FastMCP(
    "ano-gpt",
    instructions=(
        "Pilote ANO-GPT, l'assistant vocal de bureau de l'utilisateur : sa "
        "carte plein écran, la caméra du PC et du téléphone, sa voix, sa boîte "
        "Gmail, l'état de la machine et la musique. Les outils agissent sur "
        "l'application réellement affichée devant l'utilisateur — ce qu'ils "
        "montrent, il le voit. Préfère `find_nearby` à une recherche web pour "
        "un lieu : le résultat s'épingle sur la carte. Pour des photos trouvées "
        "sur le web, utilise `image_search` : elles apparaissent dans la galerie "
        "native sans ouvrir le navigateur. Pour « télécharge [musique] », utilise "
        "`download_music` : le morceau part dans ~/Musique avec une carte de "
        "progression, jamais un onglet YouTube.\n\n"
        "IMPORTANT — l'utilisateur ne regarde pas forcément ce terminal : il "
        "est devant son assistant vocal, souvent les mains occupées. Termine "
        "donc par un appel à `speak` avec ta réponse finale, en une ou deux "
        "phrases dites à voix haute. Le détail peut rester écrit ici ; "
        "l'essentiel doit être entendu."
    ),
)


def _run(name: str, /, *, offline=None, **args) -> str:
    """Exécute l'outil dans ANO-GPT, avec repli hors-ligne facultatif.

    `offline` est appelé si — et seulement si — l'application est éteinte. Les
    outils qui ont besoin de l'écran n'en fournissent pas : il vaut mieux dire
    « ANO-GPT n'est pas lancé » que faire croire à une action invisible.
    """
    try:
        return call_app(name, args)
    except AppUnavailable as exc:
        if offline is None:
            return (
                f"ANO-GPT n'est pas lancé, donc « {name} » n'a rien pu faire "
                f"({exc}). Lancez l'application, puis réessayez."
            )
        try:
            return f"[ANO-GPT éteint, exécuté hors application]\n{offline()}"
        except Exception as fallback_error:
            return f"ANO-GPT est éteint et le repli a échoué : {fallback_error}"
    except ToolError as exc:
        return f"Échec de « {name} » : {exc}"


# ── le cœur visuel ──────────────────────────────────────────────────────────

@mcp.tool()
def find_nearby(query: str, near: str = "", radius_km: float = 5.0) -> str:
    """Cherche des lieux autour de l'utilisateur et les épingle sur la carte.

    À préférer à toute recherche web pour « où est la pharmacie la plus
    proche », « un bon restaurant », « hôtels autour de moi ». Les résultats
    s'affichent en grand devant l'utilisateur, chacun avec sa fiche.

    query : ce qui est cherché, en mots simples et dans la langue de
            l'utilisateur (« pharmacie de garde », « pizza »).
    near  : pour chercher autour d'un lieu nommé plutôt qu'autour de
            l'utilisateur (« près de la gare »).
    """
    def _offline() -> str:
        from actions.find_nearby import find_nearby as _search

        return _search({"query": query, "near": near, "radius_km": radius_km})

    return _run("find_nearby", offline=_offline,
                query=query, near=near, radius_km=radius_km)


@mcp.tool()
def show_map(query: str = "", lat: float | None = None,
             lon: float | None = None, radius_km: float = 3.0) -> str:
    """Affiche UN point sur la grande carte d'ANO-GPT.

    Pour une liste de lieux, utilisez `find_nearby` : il remplit la même carte
    avec toutes les fiches. Donnez `lat`/`lon` quand vous les connaissez — le
    géocodage d'un nom de commerce échoue souvent, des coordonnées jamais.
    """
    return _run("show_map", query=query, lat=lat, lon=lon, radius_km=radius_km)


@mcp.tool()
def close_map() -> str:
    """Referme la carte et rend l'écran à la vue normale de l'assistant."""
    return _run("close_map")


@mcp.tool()
def camera(action: str = "open", source: str = "", lens: str = "") -> str:
    """Pilote la caméra affichée dans ANO-GPT (jamais une application externe).

    action : open | photo | video_start | video_stop | switch | lens | flip | close
    source : 'pc' pour la webcam, 'phone' pour le téléphone appairé
    lens   : 'front' (selfie, celle qui regarde l'utilisateur) ou 'back'.
             Demander l'objectif frontal bascule automatiquement sur le
             téléphone : lui seul possède deux objectifs.
    """
    return _run("camera", action=action, source=source, lens=lens)


@mcp.tool()
def show_card(title: str, body: str, type: str = "info") -> str:
    """Affiche une fiche dans le HUD d'ANO-GPT, devant l'utilisateur.

    Le bon outil pour rendre un résultat visible sans interrompre la voix.
    type : info | result | message | task | error
    """
    return _run("show_card", title=title, body=body, type=type)


@mcp.tool()
def hud_appearance(action: str = "apply", orb_style: str = "",
                   background_image: str = "") -> str:
    """Change immédiatement le fond et l'orbe du HUD d'ANO-GPT.

    action : list (choix disponibles) | status (apparence actuelle) | apply.
    orb_style : nom ou ID de l'orbe, par exemple IRIS, NEBULA, SPECTRE.
    background_image : nom d'une image dans background/, chemin local, ou
                       'aucun' pour retirer le fond. Les deux peuvent être
                       changés dans un même appel. Ce n'est pas le fond Linux.
    """
    return _run("hud_appearance", action=action, orb_style=orb_style,
                background_image=background_image)


# ── voix et état ────────────────────────────────────────────────────────────

@mcp.tool()
def speak(text: str) -> str:
    """Fait dire quelque chose à ANO-GPT à voix haute.

    Le texte passe par la voix de l'assistant, qui le reformule dans la langue
    de l'utilisateur — ce n'est pas une lecture mot pour mot. Utile pour
    signaler la fin d'une tâche longue pendant que l'utilisateur regarde
    ailleurs.
    """
    if os.environ.get(GHOST_MODE_ENV):
        return (
            "Annonce différée : tu es un Agent Fantôme. Termine la mission et "
            "rends ton résumé ; ANO-GPT préviendra l'utilisateur au moment sûr."
        )
    return _run("speak", text=text)


@mcp.tool()
def ask_assistant(text: str) -> str:
    """Confie une demande à l'assistant vocal, qui décidera lui-même quoi faire.

    À réserver aux cas où vous voulez que l'assistant reprenne la main. Pour
    exécuter une action précise, appelez directement l'outil correspondant :
    c'est plus rapide et le résultat vous revient.
    """
    if os.environ.get(LOOP_GUARD_ENV):
        # C'est ANO-GPT lui-même qui a lancé cet agent pour réfléchir. Renvoyer
        # la question à l'assistant vocal la ferait repartir vers l'agent, sans
        # fin. Refuser est la seule issue, et il faut dire pourquoi.
        return (
            "Indisponible ici : cette session a été lancée par ANO-GPT lui-même, "
            "et lui renvoyer la demande créerait une boucle. Réponds directement, "
            "ou utilise `speak` pour faire dire quelque chose à l'utilisateur."
        )
    # Côté application l'outil s'appelle « ask » ; le nom exposé à l'agent est
    # plus explicite, car « ask » seul ne dit pas à qui l'on s'adresse.
    return _run("ask", text=text)


@mcp.tool()
def assistant_status() -> str:
    """État d'ANO-GPT : micro, session vocale, caméra et objectif en cours."""
    return _run("assistant_status", offline=lambda: "ANO-GPT n'est pas lancé.")


# ── données personnelles ────────────────────────────────────────────────────

@mcp.tool()
def email(action: str = "unread", query: str = "", id: str = "",
          max_results: int = 10, sender: str = "", recipient: str = "",
          subject: str = "", after: str = "", before: str = "",
          filename: str = "", label: str = "", scope: str = "",
          has_attachment: bool = False, unread: bool | None = None,
          starred: bool = False, important: bool = False,
          include_spam_trash: bool = False) -> str:
    """Accède à la boîte Gmail de l'utilisateur (lecture seule, OAuth).

    action : status | setup | unread | recent | search | advanced_search | read | summary
    query  : demande naturelle ou requête Gmail native ('from:alice newer_than:30d')
    id     : identifiant du message, ou son numéro dans la dernière liste

    Pour une recherche précise, utilisez sender, recipient, subject, after,
    before, filename, label, scope, has_attachment, unread, starred et important.

    Ne concluez jamais que la boîte est vide quand l'outil signale un problème
    de configuration ou de connexion : lisez ce qu'il renvoie.
    """
    def _offline() -> str:
        from actions.email import email_control

        return email_control({
            "action": action, "query": query, "id": id,
            "max_results": max_results, "from": sender, "to": recipient,
            "subject": subject, "after": after, "before": before,
            "filename": filename, "label": label, "scope": scope,
            "has_attachment": has_attachment, "unread": unread,
            "starred": starred, "important": important,
            "include_spam_trash": include_spam_trash,
        })

    return _run(
        "email", offline=_offline, action=action, query=query, id=id,
        max_results=max_results, **{
            "from": sender, "to": recipient, "subject": subject,
            "after": after, "before": before, "filename": filename,
            "label": label, "scope": scope,
            "has_attachment": has_attachment, "unread": unread,
            "starred": starred, "important": important,
            "include_spam_trash": include_spam_trash,
        },
    )


@mcp.tool()
def calendar(action: str = "list", provider: str = "auto", id: str = "",
             title: str = "", start: str = "", end: str = "",
             description: str = "", location: str = "",
             attendees: list[str] | None = None, max_results: int = 20,
             calendar_id: str = "primary", dry_run: bool = False,
             allow_conflict: bool = False) -> str:
    """Lit et modifie Google Calendar ou un agenda CalDAV.

    action : status | connect | list | availability | create | update | delete
    provider : auto | google | caldav. start/end utilisent ISO 8601 ; une date
    seule crée ou sélectionne une journée entière. Les invités peuvent être des
    e-mails ou des noms présents dans le carnet `contacts`.
    """
    args = {
        "action": action, "provider": provider, "id": id, "title": title,
        "start": start, "end": end, "description": description,
        "location": location, "attendees": attendees or [],
        "max_results": max_results, "calendar_id": calendar_id,
        "dry_run": dry_run, "allow_conflict": allow_conflict,
    }

    def _offline() -> str:
        from actions.calendar import calendar_control
        return calendar_control(args)

    return _run("calendar", offline=_offline, **args)


@mcp.tool()
def cloud_integrations(service: str, action: str = "status", query: str = "",
                       title: str = "", content: str = "", parent_id: str = "",
                       file_key: str = "") -> str:
    """Connecte les outils cloud officiels d'ANO-GPT.

    service : calendar | notion | figma | figma_make | notebooklm | gemini | stitch.
    action : status | connect | open. Notion offre aussi search et create_note,
    Figma offre inspect. Ces deux API utilisent un jeton personnel gratuit,
    stocké dans le trousseau ; elles ne changent ni ne facturent le plan Student.
    Les autres services s'ouvrent dans le profil Google Chrome habituel.
    """
    args = {"service": service, "action": action, "query": query, "title": title,
            "content": content, "parent_id": parent_id, "file_key": file_key}

    def _offline() -> str:
        from actions.cloud_integrations import cloud_integrations_control
        return cloud_integrations_control(args)

    return _run("cloud_integrations", offline=_offline, **args)


@mcp.tool()
def contacts(action: str = "list", id: str = "", query: str = "",
             name: str = "", aliases: list[str] | None = None,
             emails: list[str] | None = None, phone: str = "", notes: str = "",
             whatsapp: str = "", telegram: str = "", signal: str = "",
             discord: str = "", instagram: str = "", messenger: str = "") -> str:
    """Gère le carnet durable utilisé par la messagerie, Gmail et l'agenda.

    action : list | search | add | update | delete. Un contact accepte plusieurs
    alias/e-mails, un téléphone et un identifiant propre à chaque messagerie.
    """
    args = {
        "action": action, "id": id, "query": query, "name": name,
        "aliases": aliases or [], "emails": emails or [], "phone": phone,
        "notes": notes, "whatsapp": whatsapp, "telegram": telegram,
        "signal": signal, "discord": discord, "instagram": instagram,
        "messenger": messenger,
    }

    def _offline() -> str:
        from actions.contacts import contacts_control
        return contacts_control(args)

    return _run("contacts", offline=_offline, **args)


@mcp.tool()
def memory_save(key: str = "", value: str = "", category: str = "notes") -> str:
    """Enregistre durablement un fait personnel explicitement confié par l'utilisateur.

    À utiliser seulement quand l'utilisateur demande de retenir une information.
    category : identity | preferences | projects | relationships | wishes | notes
    key      : nom court et stable du fait ; value : information à conserver.
    """
    def _offline() -> str:
        from core import memory_store
        from memory.memory_manager import remember

        if not key.strip() or not value.strip():
            return "Précisez la clé et la valeur à mémoriser."
        cat = category.strip() or "notes"
        remember(key.strip(), value.strip(), cat)
        kind = (memory_store.KIND_PROFILE
                if cat in ("identity", "preferences", "relationships")
                else memory_store.KIND_FACT)
        return memory_store.save(value.strip(), kind=kind, key=key.strip(),
                                 category=cat)

    return _run("memory_save", offline=_offline,
                key=key, value=value, category=category)


@mcp.tool()
def memory_search(query: str = "") -> str:
    """Cherche dans la mémoire longue durée de l'utilisateur.

    À utiliser pour retrouver une préférence, un projet, une relation, un fait
    daté ou le résumé d'une conversation passée.
    query : les mots du sujet recherché. Vide, l'outil rend le socle — profil,
    faits récents et dernières conversations.
    """
    def _offline() -> str:
        from core import memory_store

        if query.strip():
            rows = memory_store.search(query.strip(), limit=6)
            if not rows:
                return f"Rien en mémoire à propos de « {query.strip()} »."
            return "\n".join(
                f"- {(r['key'] or '').replace('_', ' ') or r['kind']} : {r['value']}"
                for r in rows
            )
        return (memory_store.session_block(max_chars=2500).strip()
                or "Aucun souvenir enregistré.")

    return _run("memory_search", offline=_offline, query=query)


@mcp.tool()
def second_brain(query: str = "", action: str = "search", limit: int = 8,
                 command: str = "", project: str = "", title: str = "", content: str = "") -> str:
    """Recherche associative dans toutes les connaissances locales d'ANO-GPT.

    Sources : conversations, souvenirs, fichiers/notes, commandes, contacts,
    projets, dates et e-mails déjà consultés.
    Actions : search | status | reindex | save_command | index_project | note | visualize.
    Utiliser cet outil pour retrouver une information passée par association ou mémoriser des connaissances.
    """
    def _offline() -> str:
        from actions.second_brain import second_brain_action

        return second_brain_action({
            "query": query, "action": action, "limit": limit,
            "command": command, "project": project, "title": title, "content": content,
        })

    return _run("second_brain", offline=_offline, query=query,
                action=action, limit=limit, command=command,
                project=project, title=title, content=content)


# ── machine et médias ───────────────────────────────────────────────────────

@mcp.tool()
def weather(city: str = "", time: str = "aujourd'hui") -> str:
    """Donne la météo actuelle ou prévue pour une ville.

    À utiliser pour la température, la pluie, le vent et les prévisions.
    city : ville visée ; time : aujourd'hui | demain | période en langage naturel.
    """
    def _offline() -> str:
        from actions.weather_report import weather_report

        return weather_report({"city": city, "time": time})

    return _run("weather", offline=_offline, city=city, time=time)


@mcp.tool()
def web_search(query: str = "", mode: str = "search", platform: str = "",
               items: list[str] | None = None, aspect: str = "general",
               count: int = 5) -> str:
    """Recherche des informations récentes sur le web, sans ouvrir de navigateur.

    À utiliser pour les faits actuels, actualités, prix ou recherches approfondies.
    Pour trouver un créateur par son pseudo, utilisez mode='social', query=<pseudo>
    et platform='tiktok', 'instagram', 'youtube', etc. Le résultat ne confirme
    que des liens de profil publics indexés, jamais un compte supposé.
    Pour un commerce proche, préférez `find_nearby`, qui affiche aussi la carte.
    mode : search | social | news | research | price | compare | headlines
    items : éléments à comparer avec mode='compare' ; count : nombre de résultats.
    """
    safe_items = items or []

    def _offline() -> str:
        from actions.web_search import web_search as _search

        return _search({"query": query, "mode": mode, "platform": platform, "items": safe_items,
                        "aspect": aspect, "count": count})

    return _run("web_search", offline=_offline, query=query, mode=mode, platform=platform,
                items=safe_items, aspect=aspect, count=count)


@mcp.tool()
def image_search(query: str, limit: int = 6) -> str:
    """Cherche des images et les affiche dans la galerie plein écran d'ANO-GPT.

    Utilisez cet outil — jamais un navigateur — quand l'utilisateur demande de
    chercher, montrer ou afficher des photos trouvées sur le web. La recherche
    reste en arrière-plan ; les résultats sont classés par pertinence et la
    meilleure correspondance est affichée en premier.

    query : sujet visuel exact demandé ; limit : 1 à 8 images.
    """
    return _run("image_search", query=query, limit=max(1, min(int(limit), 8)))


@mcp.tool()
def close_image_gallery() -> str:
    """Ferme la galerie d'images plein écran et revient à la vue normale."""
    return _run("close_image_gallery")


@mcp.tool()
def screenshot(action: str = "screenshot", path: str = "", monitor: str = "",
               window: str = "", copy_clipboard: bool = True,
               annotate: bool = False, delay: float = 0) -> str:
    """Capture l'écran de la machine ou une zone, même si ANO-GPT est éteint.

    action : screenshot | region | window | monitors
    path : destination facultative ; monitor/window : cible facultative.
    copy_clipboard copie l'image dans le presse-papiers quand c'est possible.
    """
    params = {"action": action, "path": path or None, "monitor": monitor or None,
              "window": window or None, "copy_clipboard": copy_clipboard,
              "annotate": annotate, "delay": delay}

    def _offline() -> str:
        from actions.capture import capture_control

        return capture_control(params)

    return _run("screenshot", offline=_offline, **params)


@mcp.tool()
def open_app(app_name: str = "", command: str = "", target: str = "", workspace: str = "",
             hidden: bool = False) -> str:
    """Ouvre une application, un fichier ou un dossier sur le bureau de l'utilisateur.

    app_name : application ; command : commande ou texte à taper/exécuter après ouverture (ex: 'codex') ;
    target : fichier/dossier facultatif ; workspace : bureau cible.
    hidden=True lance sur le bureau spécial invisible de Hyprland.
    ANO-GPT doit être lancé pour que l'action reste visible et coordonnée.
    """
    return _run("open_app", app_name=app_name, command=command, target=target,
                workspace=workspace, hidden=hidden)


@mcp.tool()
def close_app(app_name: str = "", workspace: str = "",
              force: bool = False) -> str:
    """Ferme une application ou fenêtre sur le bureau piloté par ANO-GPT.

    app_name : nom de l'application ; workspace : bureau facultatif.
    force=True force la fermeture et ne doit être utilisé qu'à la demande explicite.
    """
    return _run("close_app", app_name=app_name,
                workspace=workspace, force=force)


@mcp.tool()
def computer_settings(action: str = "", value: str = "",
                      confirmed: str = "") -> str:
    """Modifie un réglage ou déclenche un raccourci de la machine affichée.

    action : volume_get | volume_set | volume_up | volume_down | volume_mute |
    brightness_get | brightness_set | brightness_up | brightness_down |
    lock | dark_mode | open_settings | show_desktop | switch_window.
    value : niveau ou quantité. Les actions risquées exigent confirmed='yes'.
    """
    return _run("computer_settings", action=action,
                value=value, confirmed=confirmed)


@mcp.tool()
def file_search(name: str = "", extension: str = "", path: str = "home",
                kind: str = "", max_results: int = 20) -> str:
    """Recherche rapidement des fichiers locaux par nom approximatif.

    name : nom ou fragment ; path : home | desktop | downloads | documents |
    pictures | music | videos, ou chemin autorisé. extension : pdf, .docx, etc.
    kind : image | audio | video | document ; max_results est limité à 50.
    """
    def _offline() -> str:
        from actions.file_controller import file_controller

        return file_controller({"action": "find", "name": name,
                                "extension": extension, "path": path,
                                "kind": kind, "max_results": max_results})

    return _run("file_search", offline=_offline, name=name,
                extension=extension, path=path, kind=kind,
                max_results=max_results)


@mcp.tool()
def search_personal_docs(query: str, file_pattern: str = "", max_results: int = 5) -> str:
    """Recherche chirurgicale et sémantique dans les documents personnels et code source (~/Documents, ~/OUTILS, git).

    Découpe le code (.py, .js, .ts, .sh, .rs) par fonctions/classes via Tree-sitter et les documents (.md, .txt, .pdf)
    par titres avec fil d'Ariane. Retourne des extraits exacts avec liens cliquables 'file:///...' et numéros de lignes.
    """
    def _offline() -> str:
        from core.personal_rag import search_personal_docs as _rag_search
        return _rag_search(query=query, file_pattern=file_pattern, max_results=max_results)

    return _run("search_personal_docs", offline=_offline, query=query,
                file_pattern=file_pattern, max_results=max_results)


@mcp.tool()
def location(refresh: bool = False) -> str:
    """Retourne la position connue de l'utilisateur et sa provenance.

    À utiliser pour une question géographique ne nécessitant pas d'afficher la
    carte. refresh=True ignore le cache et redétecte la position si possible.
    """
    def _offline() -> str:
        import json
        from core.geolocation import get_user_location

        return json.dumps(get_user_location(force_refresh=refresh),
                          ensure_ascii=False, sort_keys=True)

    return _run("location", offline=_offline, refresh=refresh)


@mcp.tool()
def youtube(action: str = "", query: str = "", url: str = "",
            region: str = "FR", value: str = "") -> str:
    """Recherche et pilote YouTube dans l'interface affichée par ANO-GPT.

    action : search | select | play | pause | resume | back | forward |
    volume | speed | fullscreen | subtitles | next | previous | get_info |
    transcript | summarize | trending. query/url/value précisent l'action.
    """
    return _run("youtube", action=action, query=query, url=url,
                region=region, value=value)


@mcp.tool()
def reminder(action: str = "set", date: str = "", time: str = "",
             message: str = "Rappel", value: str = "",
             description: str = "") -> str:
    """Crée, liste ou annule un rappel système persistant.

    action : set | list | cancel. Pour set, fournir date au format AAAA-MM-JJ,
    time au format HH:MM et message, ou la phrase naturelle dans description.
    Pour cancel, value est le numéro ou nom.
    """
    def _offline() -> str:
        from actions.reminder import reminder as _reminder

        return _reminder({"action": action, "date": date, "time": time,
                          "message": message, "value": value,
                          "description": description})

    return _run("reminder", offline=_offline, action=action,
                date=date, time=time, message=message, value=value,
                description=description)

@mcp.tool()
def system_status(component: str = "", action: str = "status") -> str:
    """État de la machine : CPU, RAM, GPU, température, disque, batterie.

    component : cpu | ram | temp | gpu | disk | battery (vide = tout)
    action    : status | uptime | processes | top | component
    """
    def _offline() -> str:
        from actions.system_monitor import system_status_tool

        return system_status_tool({"action": action, "component": component})

    return _run("system_status", offline=_offline,
                component=component, action=action)


@mcp.tool()
def music(action: str = "play", query: str = "", value: str = "",
          kind: str = "audio") -> str:
    """Contrôle les médias locaux d'ANO-GPT dans ses lecteurs intégrés.

    action : play | pause | resume | next | previous | stop | now_playing
             | shuffle | seek | volume
    query  : ce qu'il faut écouter ou regarder
    value  : position pour 'seek' ('1:30', '+30') ou volume ('50')
    kind   : audio | video ; video utilise la galerie et le lecteur natifs
    """
    return _run("music", action=action, query=query, value=value, kind=kind)


@mcp.tool()
def download_music(query: str = "", url: str = "") -> str:
    """Télécharge le meilleur morceau YouTube dans le dossier Musique.

    À utiliser dès que l'utilisateur dit « télécharge [titre] ». Cherche la
    version officielle (pas un cover, un live ni un mix), extrait l'audio
    meilleure qualité (m4a) vers ~/Musique et affiche une carte de progression.
    """
    def _offline() -> str:
        from actions.download_music import download_music as _dl

        return _dl({"query": query, "url": url})

    return _run("download_music", offline=_offline, query=query, url=url)


@mcp.tool()
def self_repair(action: str = "diagnose", tool: str = "") -> str:
    """Bilan de santé des outils d'ANO-GPT, à partir des échecs journalisés.

    action : diagnose (bilan) | repair (réparation d'un outil nommé)
    tool   : nom de l'outil à réparer ; vide, le premier outil défaillant.
    """
    def _offline() -> str:
        from actions.self_repair import self_repair as _repair

        return _repair(parameters={"action": action, "tool": tool})

    return _run("self_repair", offline=_offline, action=action, tool=tool)


@mcp.tool()
def voice_id(action: str = "status", name: str = "") -> str:
    """Empreinte vocale de l'utilisateur : qui vient de parler à l'assistant.

    action : status (qui parle) | enroll (apprendre sa voix sur ce qu'il vient
    de dire) | forget (tout effacer). N'appelez `enroll` que s'il le demande.
    """
    return _run("voice_id", action=action, name=name)


@mcp.tool()
def voice_style(style: str = "") -> str:
    """Lit ou choisit le style d'élocution durable d'ANO-GPT.

    style : professional | stark | synthetic. Vide retourne le style actuel.
    L'urgence et la fatigue détectées acoustiquement restent toujours
    prioritaires sur cette préférence esthétique.
    """
    def _offline() -> str:
        from core.prosody import get_prosody_manager

        manager = get_prosody_manager()
        if not style.strip():
            return f"Style vocal actuel : {manager.preferred_style()}."
        return f"Style vocal {manager.set_preferred_style(style)} activé."

    return _run("voice_style", offline=_offline, style=style)


@mcp.tool()
def routine(name: str = "") -> str:
    """Exécute une routine définie par l'utilisateur (« mode travail », « je pars »…).

    Les routines vivent dans config/routines.yaml : leur contenu appartient à
    l'utilisateur, il ne faut pas refaire leurs étapes une par une.
    name : nom ou phrase de la routine. Vide, l'outil rend la liste.
    """
    def _offline() -> str:
        from core import routines as _routines

        available = _routines.names()
        if not name.strip():
            return ("Routines disponibles : " + ", ".join(available)
                    if available else "Aucune routine définie.")
        return ("ANO-GPT n'est pas lancé : une routine agit sur la machine et "
                "sur l'affichage, elle ne peut pas s'exécuter sans lui.")

    return _run("routine", offline=_offline, name=name)


@mcp.tool()
def background_tasks(
    action: str = "list",
    url: str = "",
    target_price: float | None = None,
    interval_minutes: int = 15,
    command_contains: str = "",
    message: str = "",
    task_id: str = "",
    label: str = "",
    radius_m: float = 250.0,
    mission: str = "",
    workspace: str = "",
    timeout_minutes: int = 60,
) -> str:
    """Crée une veille durable, liste les veilles ou en annule une.

    action : delegate | watch_price | wait_build | wait_arrival | list | cancel
    delegate lance un sous-agent MCP autonome sans bloquer la conversation.
    Les tâches survivent au redémarrage et parleront via la proactivité
    d'ANO-GPT. Une veille créée hors ligne démarrera au prochain lancement.
    """
    args = {
        "action": action, "url": url, "target_price": target_price,
        "interval_minutes": interval_minutes,
        "command_contains": command_contains, "message": message,
        "task_id": task_id, "label": label, "radius_m": radius_m,
        "mission": mission, "workspace": workspace,
        "timeout_minutes": timeout_minutes,
    }

    def _offline() -> str:
        from actions.background_tasks import BackgroundTaskService, format_tasks

        service = BackgroundTaskService(lambda *a, **k: False)
        mode = (action or "list").strip().casefold()
        if mode == "list":
            return format_tasks(service.list_tasks())
        if mode == "cancel":
            return (f"Tâche {task_id} annulée."
                    if service.cancel(task_id) else "Tâche active introuvable.")
        if mode == "watch_price":
            task = service.add_price_watch(
                url, target_price=target_price,
                interval_seconds=max(1, int(interval_minutes)) * 60,
                label=label,
            )
        elif mode == "wait_build":
            task = service.add_build_wait(command_contains, message=message)
        elif mode == "wait_arrival":
            task = service.add_arrival_wait(
                message or "Tu es arrivé à la maison.",
                radius_m=radius_m, label=label or "maison",
            )
        elif mode == "delegate":
            task = service.add_agent_mission(
                mission or message,
                workspace=workspace or None,
                timeout_minutes=timeout_minutes,
            )
        else:
            return "Action de tâche de fond inconnue."
        return f"Tâche durable créée : {task['id']}."

    return _run("background_tasks", offline=_offline, **args)


@mcp.tool()
def devsecops(
    domain: str = "",
    action: str = "",
    target: str = "",
    message: str = "",
    access_logs: bool = False,
    error_logs: bool = False,
    description: str = "",
) -> str:
    """Pilote complet DevSecOps et Maître du Système Linux.

    - Docker : restart_stack, purge_dead, logs, list.
    - Systemd : status, restart, list_failed, diagnose.
    - Paquets : check_updates, search, clean_orphans.
    - Git : commit (conventionnel), rebase, status (avec scan anti-fuite de secrets).
    - Sécurité : audit des ports et de l'état système.
    """
    args = {
        "domain": domain, "action": action, "target": target,
        "message": message, "access_logs": access_logs, "error_logs": error_logs,
        "description": description,
    }

    def _offline() -> str:
        from actions.devsecops import devsecops_control
        return devsecops_control(args)

    return _run("devsecops", offline=_offline, **args)


@mcp.tool()
def hypr_orchestrator(
    action: str = "organize",
    preset: str = "",
    target: str = "",
    workspace: str = "",
    description: str = "",
) -> str:
    """Orchestrateur dynamique de fenêtres et d'espaces de travail Hyprland.

    - organize : classe automatiquement chaque fenêtre sur son workspace dédié.
    - preset : applique un preset d'agencement ('devsecops', 'coding', 'monitoring', 'web').
    - move_window : déplace une fenêtre spécifique vers un workspace.
    """
    args = {
        "action": action, "preset": preset, "target": target,
        "workspace": workspace, "description": description,
    }

    def _offline() -> str:
        from actions.hypr_orchestrator import hypr_orchestrator_control
        return hypr_orchestrator_control(args)

    return _run("hypr_orchestrator", offline=_offline, **args)


@mcp.tool()
def auto_debug(query: str = "", target: str = "active_window", input_text: str = "",
               auto_apply: bool = False) -> str:
    """Auto-debug live et interception d'erreurs : tracebacks Python, panics Rust, builds C++.

    Capture la fenêtre active (terminal, IDE), extrait l'erreur, relie le code source
    local sur disque, affiche une fiche HUD de diagnostic et explique la solution.
    query : question ('C'est quoi ce bug ?', 'Pourquoi mon build plante ?')
    target : 'active_window' | 'screen' ; input_text : log/traceback optionnel ;
    auto_apply : applique le correctif au fichier local si disponible.
    """
    def _offline() -> str:
        from actions.auto_debug import auto_debug_action
        return auto_debug_action({"query": query, "target": target,
                                  "input_text": input_text, "auto_apply": auto_apply})

    return _run("auto_debug", offline=_offline, query=query, target=target,
                input_text=input_text, auto_apply=auto_apply)


@mcp.tool()
def inspect_screen(query: str = "", domain: str = "auto",
                   target: str = "active_window") -> str:
    """Vision multimodale en direct : schémas d'architecture, graphiques, PDFs et code.

    domain : auto | architecture | chart | document | code | debug | general
    target : active_window | screen | monitor
    query  : question précise sur ce qui est affiché ('Explique ce schéma', 'Analyse la courbe')
    """
    def _offline() -> str:
        from core.multimodal_vision import inspect_screen_live
        spoken, diag = inspect_screen_live(user_query=query, target=target,
                                           domain=None if domain == "auto" else domain)
        return spoken

    return _run("inspect_screen", offline=_offline, query=query, domain=domain, target=target)


@mcp.tool()
def navigate(destination: str = "", action: str = "start",
             mode: str = "driving") -> str:
    """Guidage GPS parlé pas-à-pas et itinéraire animé sur grand écran et smartphone.

    destination : nom de lieu, adresse ou cible
    action      : start (démarrer) | stop (arrêter) | status (état / prochaine étape)
    mode        : driving (voiture) | walking (piéton) | cycling (vélo)
    """
    def _offline() -> str:
        from actions.navigation import navigation_action
        return navigation_action({"destination": destination, "action": action, "mode": mode})

    return _run("navigate", offline=_offline, destination=destination, action=action, mode=mode)


@mcp.tool()
def point_on_screen(description: str, target: str = "",
                    coordinates: list[int] | None = None,
                    mode: str = "auto", duration: float = 3.0) -> str:
    """Montre à l'utilisateur OÙ se trouve un élément sur son écran.

    UTILISE `target` — PRESQUE TOUJOURS. ANO-GPT capture alors l'écran lui-même,
    localise l'élément, vérifie sa trouvaille et n'affiche rien s'il ne le voit
    pas. C'est la seule façon d'être sûr de désigner le bon endroit.

    NE FOURNIS `coordinates` QUE si tu viens de lire ces coordonnées dans une
    analyse d'image réelle de CET écran. Ne les devine JAMAIS, ne les déduis
    jamais d'une disposition « habituelle » d'application : un pointeur posé au
    hasard envoie l'utilisateur regarder au mauvais endroit, ce qui est pire que
    de ne rien montrer. Sans certitude, laisse `coordinates` vide et donne
    `target`.

    description : Libellé lu à l'écran (ex: 'Bouton Paramètres', 'Menu Fichier').
    target      : Nom de l'élément à localiser automatiquement. À privilégier.
    coordinates : Optionnel, uniquement si mesurées :
                  - [x, y] point laser
                  - [x, y, w, h] pixels, ou [ymin, xmin, ymax, xmax] avec mode='gemini_box'
                  - [x1, y1, x2, y2, ...] trajectoire
    mode        : auto | highlight | laser | path | gemini_box
    duration    : Durée d'affichage en secondes (défaut 3.0s).
    """
    def _offline() -> str:
        from ui.visual_pointer import get_visual_pointer
        return get_visual_pointer().point_on_screen(
            description, coordinates, mode=mode, duration=duration, target=target,
        )

    return _run(
        "point_on_screen",
        offline=_offline,
        description=description,
        target=target,
        coordinates=coordinates,
        mode=mode,
        duration=duration,
    )


@mcp.tool()
def prayer(action: str = "next", prayer: str = "", enabled: bool | None = None, method: str = "") -> str:
    """Gestion et consultation des heures de prière (Adhan).

    action: 'next' (prochaine prière et temps restant), 'today' ou 'list' (toutes les prières du jour),
            'toggle' (activer/désactiver une prière spécifique via prayer et enabled),
            'toggle_all' (activer/désactiver toutes les annonces de prière),
            'set_method' (changer la méthode astronomique: MWL, UOIF, EGYPT, ISNA, MAKKAH, KARACHI),
            'status' (résumé des paramètres actuels).
    prayer: Nom de la prière pour toggle (ex: 'fajr', 'dhuhr', 'asr', 'maghrib', 'isha').
    enabled: True pour activer, False pour désactiver (optionnel si omission bascule l'état).
    method: Nom de la méthode de calcul pour set_method.
    """
    def _offline() -> str:
        from actions.prayer import prayer_control
        args = {"action": action}
        if prayer:
            args["prayer"] = prayer
        if enabled is not None:
            args["enabled"] = enabled
        if method:
            args["method"] = method
        return prayer_control(args)

    return _run("prayer", offline=_offline, action=action, prayer=prayer, enabled=enabled, method=method)


# ── vérification ────────────────────────────────────────────────────────────

def _selftest() -> int:
    """Vérifie le pont sans passer par un agent."""
    running = app_running()
    print(f"ANO-GPT en cours d'exécution : {'oui' if running else 'non'}")
    print(f"Outils exposés : {len(_TOOL_NAMES)}")
    for name in _TOOL_NAMES:
        print(f"  · {name}")
    if running:
        print("\nassistant_status ->", assistant_status())
    else:
        print("\nLancez ANO-GPT pour tester un appel réel.")
        print("system_status (repli hors ligne) ->",
              system_status(component="cpu")[:200])
    return 0


_TOOL_NAMES = (
    "find_nearby", "show_map", "close_map", "camera", "show_card", "hud_appearance",
    "speak", "ask_assistant", "assistant_status",
    "email", "calendar", "cloud_integrations", "contacts", "memory_save", "memory_search", "second_brain",
    "weather", "web_search", "image_search", "close_image_gallery", "screenshot", "open_app", "close_app",
    "computer_settings", "file_search", "search_personal_docs", "location", "youtube", "reminder",
    "system_status", "music", "download_music", "routine", "voice_id", "voice_style", "self_repair", "background_tasks",
    "devsecops", "hypr_orchestrator", "auto_debug", "inspect_screen", "navigate", "point_on_screen",
    "prayer",
)

# Ces deux ensembles rendent le contrat de repli vérifiable sans exécuter une
# capture ou une recherche réseau pendant les tests.
_OFFLINE_TOOLS = frozenset({
    "find_nearby", "assistant_status", "email", "calendar", "cloud_integrations", "contacts", "memory_save",
    "memory_search", "second_brain", "weather", "web_search", "screenshot", "file_search",
    "search_personal_docs", "location", "reminder", "system_status", "routine", "voice_style", "self_repair",
    "background_tasks", "devsecops", "hypr_orchestrator", "auto_debug", "inspect_screen", "navigate", "point_on_screen",
    "prayer", "download_music",
})
_SCREEN_REQUIRED_TOOLS = frozenset(_TOOL_NAMES) - _OFFLINE_TOOLS

# Un seul nom historique diffère sur le socket ; le déclarer ici évite que la
# documentation MCP et la table de l'application divergent silencieusement.
_APP_TOOL_NAMES = {
    name: ("ask" if name == "ask_assistant" else name)
    for name in _TOOL_NAMES
}


def _exit_cleanly(signum, _frame):
    """Sort avec le code 0 quand l'agent demande l'arrêt.

    Sans cela, Python meurt *du* signal et le processus est rapporté comme
    « signal: terminated ». Antigravity lit cette fin comme un échec d'arrêt et
    interrompt tout son rechargement — la liste des serveurs se vide alors et
    la CLI affiche « No MCP servers configured » alors que tout allait bien.

    `os._exit` plutôt qu'une exception : lever `SystemExit` depuis un
    gestionnaire de signal ne fonctionne pas ici, la boucle d'événements
    d'anyio l'absorbe et le processus reste vivant — mesuré. Ce serveur ne
    tient ni fichier ouvert en écriture ni verrou : il n'y a rien à refermer.
    """
    import os

    os._exit(0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())

    import signal

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _exit_cleanly)
        except (ValueError, OSError):
            pass  # plateforme sans ce signal : le défaut fera l'affaire

    # stdio : le transport attendu par agy, Claude Code, Codex et Desktop.
    mcp.run()
