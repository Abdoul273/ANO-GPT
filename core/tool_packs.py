"""Outils par contexte : un noyau permanent, des paquets ouverts à la demande.

Gemini Live fige sa liste d'outils à la connexion. Les envoyer tous — 63
déclarations, près de 17 000 jetons — coûte trois fois :

* la latence de chaque reconnexion, payée avant le premier mot ;
* le quota, à chaque session ;
* et surtout la précision, car des dizaines de descriptions voisines finissent
  par se confondre (``contacts_control``, carnet du PC, pris pour le carnet du
  téléphone).

Ce module garde en permanence le noyau — voix, écran, recherche, carte,
téléphone, rappels, shell — et n'ouvre un paquet (musique, bureautique, dev…)
que lorsque la phrase de l'utilisateur le réclame vraiment. Un paquet ouvert le
reste jusqu'à la fin de la session : on ne paie l'élargissement qu'une fois.

Le déclenchement est lexical, volontairement : il est instantané, lisible,
testable, et ne dépense ni appel réseau ni jeton. Le repêchage, lui, passe par
``report_capability_gap`` — l'outil que le modèle appelle quand il se croit
démuni — ce qui rattrape les formulations qu'aucune liste ne prévoit.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping


# ── Noyau permanent ──────────────────────────────────────────────────────────
# Ce que l'utilisateur peut demander à froid, sans prévenir : parler, voir
# l'écran, chercher, se situer, téléphoner, et la porte de sortie universelle
# qu'est le shell. Tout ce qui est ici est payé à chaque session : n'y ajouter
# un outil que si son absence casserait une demande courante.
CORE: frozenset[str] = frozenset({
    # Recherche et mémoire
    "web_search", "second_brain", "save_memory", "deep_think",
    # Écran et vision
    "screen_process", "capture_control", "point_on_screen",
    # Carte et position
    "show_map", "close_map", "location",
    # Téléphone (ANO-Remote)
    "phone_call", "phone_hangup", "phone_sms", "phone_contacts",
    # Temps
    "reminder", "timer", "weather_report",
    # Machine
    "system_status", "open_app", "close_app", "shell_exec", "undo_action",
    # Cadre de session
    "report_capability_gap", "shutdown_jarvis", "voice_style",
})


@dataclass(frozen=True)
class ToolPack:
    """Un domaine d'outils et les mots qui l'ouvrent."""

    label: str
    tools: frozenset[str]
    triggers: tuple[re.Pattern[str], ...]

    def matches(self, folded: str) -> bool:
        return any(pattern.search(folded) for pattern in self.triggers)


def _triggers(*expressions: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(expr) for expr in expressions)


# Les motifs sont écrits sur du texte replié (minuscules, sans accents) : une
# transcription vocale ne garantit ni la casse ni les accents.
PACKS: Mapping[str, ToolPack] = {
    "navigation": ToolPack(
        label="navigation et lieux",
        tools=frozenset({"navigate", "find_nearby"}),
        triggers=_triggers(
            r"\b(itineraire|itinéraire|guide[- ]?moi|conduis|route vers|trajet)\b",
            r"\b(le plus proche|la plus proche|pres d'ici|pres de moi|autour de moi)\b",
            r"\b(pharmacie|restaurant|station|hopital|hotel|supermarche|banque|essence)\b",
            r"\b(comment (?:aller|me rendre)|emmene[- ]?moi)\b",
        ),
    ),
    "musique": ToolPack(
        label="musique et vidéo",
        tools=frozenset({"music_control", "download_music", "youtube_video"}),
        triggers=_triggers(
            r"\b(musique|chanson|morceau|album|playlist|artiste|son)\b",
            r"\b(joue|mets|lance|ecoute|ecouter|pause|volume|piste suivante)\b.{0,24}"
            r"\b(musique|chanson|son|titre|clip|video)\b",
            r"\b(youtube|clip|spotify|deezer|soundcloud)\b",
            r"\b(telecharge|telecharger)\b.{0,24}\b(musique|chanson|video|clip)\b",
        ),
    ),
    "images": ToolPack(
        label="images et création visuelle",
        tools=frozenset({
            "image_search", "generate_image", "generate_video",
            "show_last_generated_image", "close_image_gallery",
        }),
        triggers=_triggers(
            r"\b(image|images|photo de|illustration|dessine|dessin|logo|affiche)\b",
            r"\b(gener(?:e|er)|cree|creer|fabrique)\b.{0,24}\b(image|video|visuel|illustration)\b",
            r"\b(galerie|montre[- ]?moi (?:une|des) (?:image|photo))\b",
        ),
    ),
    "camera": ToolPack(
        label="caméra",
        tools=frozenset({"camera_control", "close_camera"}),
        triggers=_triggers(
            r"\b(camera|appareil photo|webcam|selfie|objectif)\b",
            r"\b(prends[- ]?moi en photo|filme|filmer|enregistre une video)\b",
        ),
    ),
    "bureautique": ToolPack(
        label="e-mail, agenda et messagerie",
        tools=frozenset({
            "email_control", "calendar_control", "contacts_control",
            "send_message", "cloud_integrations_control", "generate_document",
        }),
        triggers=_triggers(
            r"\b(mail|mails|e-?mail|gmail|boite mail|courriel)\b",
            r"\b(agenda|calendrier|reunion|rendez[- ]?vous|planning|disponibilit)\b",
            r"\b(whatsapp|telegram|signal|discord|instagram|messenger)\b",
            r"\b(envoie|envoyer|ecris|redige)\b.{0,30}\b(message|mail|courriel)\b",
            r"\b(notion|figma)\b",
            r"\b(carnet|contact|contacts)\b.{0,24}\b(pc|ordinateur|local)\b",
            r"\b(document|rapport|lettre|cv)\b.{0,24}\b(genere|cree|redige|ecris)\b",
            r"\b(genere|cree|redige|ecris)\b.{0,24}\b(document|rapport|lettre|cv)\b",
        ),
    ),
    "fichiers": ToolPack(
        label="fichiers et navigateur",
        tools=frozenset({"file_controller", "browser_control", "search_personal_docs"}),
        triggers=_triggers(
            r"\b(fichier|fichiers|dossier|repertoire|disque|telechargements)\b",
            r"\b(ai[- ]?je un fichier|trouve|cherche|ou est)\b.{0,30}\b(fichier|dossier|pdf|document)\b",
            r"\b(navigateur|chrome|firefox|onglet|page web|formulaire|connecte[- ]?toi sur)\b",
            r"\b(mes (?:documents|notes|pdf)|dans mes fichiers)\b",
        ),
    ),
    "dev": ToolPack(
        label="développement et système avancé",
        tools=frozenset({
            "devsecops", "github_control", "live_auto_debug", "self_repair",
            "hypr_orchestrator", "computer_control", "computer_settings",
        }),
        triggers=_triggers(
            r"\b(git|github|commit|branche|pull request|depot|repo)\b",
            r"\b(docker|kubernetes|serveur|deploiement|deploie|pipeline|ci)\b",
            r"\b(bug|erreur|stack ?trace|traceback|compile|build|test unitaire)\b",
            r"\b(hyprland|workspace|fenetre|moniteur|ecran secondaire)\b",
            r"\b(luminosite|volume systeme|wifi|bluetooth|veille|verrouille)\b",
            r"\b(repare|repare[- ]?toi|auto[- ]?diagnostic|scan securite|vulnerabilit)\b",
        ),
    ),
    "reseaux": ToolPack(
        label="réseaux sociaux",
        tools=frozenset({"tiktok_tracker", "tiktok_coach"}),
        triggers=_triggers(
            r"\b(tiktok|tik tok|abonnes|abonne|followers|follower|vues|likes)\b",
            r"\b(mon compte|ma video|mes videos|ma derniere video)\b",
            r"\b(blow|ca monte|ca decolle|viral|virale|percer|coach)\b",
            r"\b(pourquoi|analyse|regarde|avant de (?:la )?poster|publier)\b.{0,30}\b(video|vues|likes|tiktok)\b",
        ),
    ),
    "assistanat": ToolPack(
        label="veilles, routines et entraînement",
        tools=frozenset({
            "background_tasks", "proactive_mode", "focus_guard", "routine",
            "sparring_partner", "simulate_decision", "auto_extension_control",
            "plugin_manager", "voice_id", "prayer_control",
        }),
        triggers=_triggers(
            r"\b(veille|surveille|previens[- ]?moi quand|des que|tache de fond)\b",
            r"\b(routine|mode (?:travail|nuit|concentration)|je pars|scenario)\b",
            r"\b(entraine[- ]?moi|entrainement|simulation|entretien|oral|sparring)\b",
            r"\b(concentration|distraction|bloque (?:les|le) (?:sites|reseaux))\b",
            r"\b(priere|prieres|salat|adhan)\b",
            r"\b(plugin|extension|apprends ma voix|empreinte vocale)\b",
            r"\b(mode proactif|sois proactif)\b",
        ),
    ),
}


def _fold(text: str) -> str:
    """Minuscule sans accents : une transcription vocale n'en garantit aucun."""
    lowered = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in lowered if not unicodedata.combining(c))


def all_pack_tools() -> frozenset[str]:
    return frozenset().union(*(pack.tools for pack in PACKS.values()))


def resolve(text: str) -> frozenset[str]:
    """Paquets réclamés par cette phrase, sans tenir compte des actifs."""
    folded = _fold(text)
    if not folded:
        return frozenset()
    return frozenset(name for name, pack in PACKS.items() if pack.matches(folded))


def labels(packs: Iterable[str]) -> str:
    """Libellés lisibles, pour le journal montré à l'utilisateur."""
    known = [PACKS[name].label for name in sorted(packs) if name in PACKS]
    return ", ".join(known)


def select_declarations(declarations: Iterable[Mapping], active: Iterable[str]) -> list[dict]:
    """Noyau + paquets ouverts. Ce qu'aucun paquet ne revendique est conservé.

    Un outil inconnu du découpage — un plugin, un outil tout juste ajouté —
    doit rester visible : le silence d'une déclaration est un bug bien plus
    coûteux que quelques jetons de trop.
    """
    active_set = {name for name in active if name in PACKS}
    allowed = set(CORE)
    for name in active_set:
        allowed |= PACKS[name].tools
    owned = all_pack_tools()
    return [
        dict(declaration) for declaration in declarations
        if str(declaration.get("name", "")) in allowed
        or str(declaration.get("name", "")) not in owned
    ]


# ── Découpe du guide d'outils du prompt système ──────────────────────────────
# Le prompt décrit chaque outil dans une section dédiée. Garder les paragraphes
# d'outils absents de la session reviendrait à décrire au modèle des capacités
# qu'il n'a pas — la meilleure façon de le faire mentir.
_GUIDE_START = re.compile(r"🔧[^\n]*\n")
_GUIDE_END = re.compile(r"═{10,}\n🎙️")

# Une entrée du guide commence en début de ligne par le nom de l'outil, parfois
# précédé d'un pictogramme, suivi d'un tiret cadratin : « 🔎 file_controller — … ».
_ENTRY_RE = re.compile(r"^[^\w\n]{0,4}([a-z_]{3,40})\s+—")
# Un intertitre en capitales (« NAVIGATEUR — PRÉFÉRENCE PERMANENTE : ») porte une
# consigne générale : il ouvre son propre bloc pour ne pas être emporté par
# l'entrée qui le précède.
_HEADING_RE = re.compile(r"^[A-ZÉÈÀÇÎÔÛ][A-ZÉÈÀÇÎÔÛ0-9 '()\-/]{3,}\s*[—:]")


def _split_guide(guide: str, known: frozenset[str]) -> list[tuple[str | None, list[str]]]:
    """Découpe le guide en blocs (nom d'outil concerné, lignes)."""
    blocks: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in guide.splitlines(keepends=True):
        entry = _ENTRY_RE.match(line)
        name = entry.group(1) if entry and entry.group(1) in known else None
        if name is not None or _HEADING_RE.match(line):
            blocks.append((name, [line]))
        else:
            blocks[-1][1].append(line)
    return blocks


def filter_prompt(prompt: str, active: Iterable[str]) -> str:
    """Retire du guide les entrées propres aux paquets restés fermés.

    Une consigne générale — celles qui n'ouvrent aucune entrée d'outil — est
    toujours conservée : c'est ce qui tient la conduite du modèle, quels que
    soient les outils du moment.
    """
    start = _GUIDE_START.search(prompt)
    end = _GUIDE_END.search(prompt)
    if not start or not end or end.start() <= start.end():
        return prompt

    allowed = set(CORE)
    for name in (n for n in active if n in PACKS):
        allowed |= PACKS[name].tools
    dropped = all_pack_tools() - allowed
    if not dropped:
        return prompt

    known = CORE | all_pack_tools()
    head, guide, tail = (prompt[:start.end()],
                         prompt[start.end():end.start()],
                         prompt[end.start():])
    kept = [
        "".join(lines) for name, lines in _split_guide(guide, known)
        if name is None or name not in dropped
    ]
    return head + "".join(kept) + tail
