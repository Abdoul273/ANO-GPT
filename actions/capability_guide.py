"""Guide des compétences d'ANO-GPT — résumé parlé, fichier unique, jamais recréé à tort.

Quand l'utilisateur demande ce que l'assistant sait faire, on ne récite pas
soixante outils à voix haute. On dit les meilleures fonctions, puis on
propose de noter le reste dans un Markdown (ce que ça fait, comment ça
marche, comment m'en parler).

Le fichier est canonique : un seul chemin, une empreinte du catalogue dans
l'en-tête. On n'écrit que s'il manque ou s'il n'est plus à jour. Un « oui »
après la question ouvre le guide déjà prêt, ou le rédige s'il n'existe pas.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core import action_kit as kit

CATALOG_VERSION = 1
GUIDE_NAME = "Guide des compétences.md"
MARKER_RE = re.compile(
    r"<!--\s*anogpt-competences\s+fingerprint=([0-9a-f]+)\s+catalog=(\d+)\s*-->",
    re.IGNORECASE,
)


def guide_path() -> Path:
    override = os.environ.get("ANOGPT_COMPETENCES_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "ANO-GPT" / GUIDE_NAME


# ── Catalogue ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capability:
    """Une capacité utilisateur, pas un outil interne."""

    id: str
    category: str
    title: str
    does: str
    how: str
    say: tuple[str, ...]
    highlight: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    # Toujours actif
    Capability(
        "wake_word",
        "Voix et conversation",
        "Mot de réveil",
        "Tant que tu n'as pas dit « ANO », rien ne part vers le modèle : le micro reste local.",
        "Un petit modèle local écoute le mot « ANO ». Aucun flux n'est envoyé "
        "à Google avant ça. Après le réveil, tu peux enchaîner sans le répéter.",
        ("ANO", "ANO, quelle heure est-il ?"),
    ),
    Capability(
        "barge_in",
        "Voix et conversation",
        "M'interrompre",
        "Tu peux me couper pendant que je parle. J'arrête tout de suite.",
        "Un détecteur local (pas le cloud) reconnaît « stop », « attends », "
        "« tais-toi » ou « ANO stop » et coupe la voix sans clic.",
        ("stop", "attends", "ANO stop", "tais-toi"),
    ),
    Capability(
        "follow_up",
        "Voix et conversation",
        "Conversation continue",
        "Après une réponse, tu as une vingtaine de secondes pour enchaîner sans redire « ANO ».",
        "La fenêtre d'écoute reste ouverte un moment. « Merci », « c'est bon » "
        "ou un silence un peu long la referme.",
        ("c'est tout", "merci", "c'est bon"),
    ),
    Capability(
        "tone",
        "Voix et conversation",
        "Modes de ton",
        "Le tutoiement, l'humour et le vouvoiement suivent le mode actif (Normal, Astro, Coquin, Majeur).",
        "Le mode est injecté dans la session. Tu peux aussi demander un style "
        "durable : professionnel, Tony Stark, plus synthétique.",
        ("parle plus pro", "sois plus direct", "reviens normal"),
    ),
    Capability(
        "confirm",
        "Voix et conversation",
        "Confirmation humaine",
        "Pour une action sensible (sudo, suppression, message), une carte "
        "[Confirmer] / [Annuler] s'affiche. Rien ne part sans toi.",
        "L'outil crée la carte tout seul. Un « oui » vocal ou un clic suffit. Je ne contourne jamais ça.",
        ("oui", "non", "annule"),
    ),
    # Contrôle PC — highlight
    Capability(
        "pc_control",
        "Contrôle de l'ordinateur",
        "Piloter le PC",
        "J'ouvre et je ferme tes applications, je clique, je tape, je déplace "
        "les fenêtres, je change de bureau.",
        "Tout passe par Hyprland sur Wayland. Un lancement est vérifié pour de "
        "vrai : un simple « ok » ne me suffit pas. Fermer plusieurs fenêtres "
        "d'un coup n'arrive que si tu le demandes clairement.",
        (
            "ouvre Chrome",
            "ferme kitty",
            "tape ça dans le terminal",
            "mets cette fenêtre en plein écran",
            "passe au bureau 3",
        ),
        highlight=True,
    ),
    Capability(
        "settings",
        "Contrôle de l'ordinateur",
        "Réglages machine",
        "Volume, luminosité, Wi-Fi, Bluetooth, veille, verrouillage.",
        "Les commandes système passent par l'outil dédié, pas par une "
        "improvisation shell. Le volume média et le volume système sont distincts.",
        ("baisse la luminosité", "coupe le Wi-Fi", "verrouille l'écran", "mets le volume à 40"),
    ),
    Capability(
        "hypr_layout",
        "Contrôle de l'ordinateur",
        "Ranger le bureau",
        "Je reclasse tes fenêtres sur leurs bureaux, ou j'applique un preset (code, monitoring, DevSecOps).",
        "L'orchestrateur Hyprland répartit : code, web, terminal, comms, média, monitoring. Un mot suffit.",
        ("organise mon espace de travail", "preset coding", "range les fenêtres"),
    ),
    Capability(
        "capture",
        "Contrôle de l'ordinateur",
        "Capture d'écran",
        "Capture plein écran, une zone, une fenêtre, ou un enregistrement vidéo.",
        "Seul moyen fiable sous Wayland (sinon l'image est noire). Au premier "
        "enregistrement, une fenêtre de partage s'ouvre : il faut l'autoriser.",
        ("prends une capture", "capture une zone", "enregistre l'écran", "arrête l'enregistrement"),
    ),
    Capability(
        "system_health",
        "Contrôle de l'ordinateur",
        "Santé de la machine",
        "Charge CPU, RAM, température, disque, fenêtre et bureau actifs.",
        "Lecture réelle des compteurs, pas une estimation. Utile avant un "
        "traitement lourd : la machine a deux cœurs et 11 Go.",
        ("où en est le PC", "il reste de la RAM ?", "température CPU"),
    ),
    # Vision — highlight
    Capability(
        "screen_vision",
        "Vision, écran et caméra",
        "Voir ton écran",
        "Je lis, j'explique et je débogue ce qui est affiché : code, schéma, erreur de terminal, document.",
        "Une capture de la fenêtre active part à un modèle vision. Si tu me "
        "demandes où se trouve un bouton, je le cadre en néon au pixel près — "
        "jamais de coordonnées inventées.",
        ("regarde mon écran", "c'est quoi ce bug", "explique ce schéma", "montre-moi où est le bouton"),
        highlight=True,
    ),
    Capability(
        "camera",
        "Vision, écran et caméra",
        "Caméra",
        "Webcam du PC ou caméra du téléphone, photo, vidéo, selfie. Le flux "
        "s'affiche en grand dans ANO-GPT, pas dans une autre appli.",
        "source PC ou téléphone. « Frontale » bascule toute seule sur le téléphone, qui a deux objectifs.",
        ("ouvre la caméra", "prends-moi en photo", "filme", "passe sur la caméra du téléphone"),
    ),
    # Médias — highlight
    Capability(
        "media",
        "Musique, vidéo et YouTube",
        "Musique et YouTube",
        "Je cherche dans tes fichiers, je lance, je mets en pause, je passe "
        "la piste. Si le morceau n'est pas en local, je propose YouTube dans "
        "le lecteur intégré — jamais une fenêtre de navigateur.",
        "La recherche tolère les accents et les fautes de dictée. YouTube a "
        "son propre outil : recherche, lecture, volume, vitesse, sous-titres, "
        "mini-lecteur. « Télécharge cette chanson » enregistre le MP3 dans "
        "~/Musique, sans le confondre avec une simple lecture.",
        (
            "joue Daft Punk",
            "lance ma vidéo Claude",
            "cherche sur YouTube un tuto git",
            "télécharge cette chanson",
            "pause",
            "piste suivante",
        ),
        highlight=True,
    ),
    Capability(
        "shazam",
        "Musique, vidéo et YouTube",
        "Reconnaître un morceau",
        "J'identifie ce qui passe (haut-parleurs ou micro) : titre, artiste, "
        "album, année, liens Spotify et YouTube.",
        "D'abord le lecteur en cours (MPRIS). Sinon j'écoute quelques secondes "
        "et je prends l'empreinte. Pas de devinette de ma part.",
        ("tu connais cette musique ?", "c'est quoi ce son ?", "qui chante ?", "lance-la sur Spotify"),
    ),
    # Carte — highlight
    Capability(
        "maps",
        "Carte, GPS et lieux",
        "Carte et guidage",
        "Une seule grande carte : ta position, un lieu, les commerces autour, un itinéraire parlé pas à pas.",
        "Les lieux viennent de Google Maps et OpenStreetMap. Le GPS parlé "
        "anticipe à 500 m, 150 m et maintenant, synchronisé avec le téléphone "
        "si ANO-Remote tourne. Je cite le nom exact et la distance, jamais "
        "« à proximité ».",
        (
            "où suis-je",
            "montre Paris sur la carte",
            "où est la pharmacie la plus proche",
            "navigue vers la gare",
        ),
        highlight=True,
    ),
    # Téléphone — highlight
    Capability(
        "phone",
        "Téléphone et messages",
        "Appels, SMS et WhatsApp",
        "J'appelle, je raccroche, j'envoie un SMS depuis ton Android, ou un "
        "message WhatsApp / Telegram / Signal depuis le PC.",
        "Le carnet du téléphone n'est pas celui du PC. Une fin de numéro "
        "suffit. Un SMS ou un message sensible affiche une carte de "
        "confirmation. Si le téléphone est absent, je le dis — je n'invente "
        "jamais un envoi réussi.",
        ("appelle maman", "raccroche", "envoie un SMS à Karim", "envoie ça sur WhatsApp à Frère"),
        highlight=True,
    ),
    Capability(
        "contacts",
        "Téléphone et messages",
        "Carnets de contacts",
        "Deux carnets distincts : le vrai carnet du téléphone, et celui du PC "
        "(alias, e-mails, identifiants de messagerie).",
        "« Ai-je un contact nommé X ? » interroge le téléphone. Le carnet PC "
        "sert à résoudre les noms pour les mails, l'agenda et WhatsApp. S'il "
        "y a plusieurs homonymes, je demande lequel.",
        ("le numéro de Karim", "combien de numéros finissent par 97", "ajoute ce contact sur le PC"),
    ),
    # Productivité
    Capability(
        "mail",
        "Mails, agenda et documents",
        "Gmail",
        "Je lis, je cherche et je rédige tes e-mails. Les nouveaux messages peuvent s'annoncer tout seuls.",
        "La recherche comprend le français naturel et la syntaxe Gmail "
        "(expéditeur, objet, dates, pièces jointes). L'autorisation passe par "
        "Chrome, une seule fois. Chaque message a sa carte.",
        ("lis mes mails", "des nouveaux messages ?", "cherche les mails de Karim", "connecte Gmail"),
    ),
    Capability(
        "calendar",
        "Mails, agenda et documents",
        "Agenda",
        "Google Calendar / CalDAV en lecture-écriture : ce que tu as "
        "aujourd'hui, un créneau libre, créer / modifier / supprimer.",
        "Les dates sont ISO. Avant de créer, je signale les chevauchements. "
        "Le briefing du matin inclut les rendez-vous du jour.",
        (
            "qu'est-ce que j'ai aujourd'hui",
            "ajoute un rdv demain à 14 h",
            "est-ce que je suis libre vendredi",
        ),
    ),
    Capability(
        "reminders",
        "Mails, agenda et documents",
        "Rappels et minuteurs",
        "Rappels persistants et plusieurs minuteurs nommés en parallèle.",
        "« Dans 20 minutes » ou « demain à midi » suffisent. À l'heure dite, "
        "je l'annonce à voix haute. Je ne reste pas à attendre en silence.",
        (
            "rappelle-moi dans 20 minutes de sortir le linge",
            "minuteur pâtes 10 minutes",
            "quels sont mes rappels",
        ),
    ),
    Capability(
        "weather",
        "Mails, agenda et documents",
        "Météo",
        "Le temps ici ou dans une ville, avec une carte visuelle.",
        "Données structurées réelles, pas une recherche web générique.",
        ("il fait quel temps", "météo à Conakry demain"),
    ),
    Capability(
        "documents",
        "Mails, agenda et documents",
        "Rédiger un document",
        "Rapport, lettre, CV, note, diaporama — rédigé puis enregistré dans "
        "~/Documents/ANO-GPT (Markdown, texte, HTML, Word, PDF, PowerPoint).",
        "Un spécialiste Azure écrit le contenu. Si un format manque, je "
        "retombe sur du Markdown plutôt que d'échouer.",
        ("rédige une lettre de motivation", "fais un rapport sur X", "génère un CV"),
    ),
    Capability(
        "cloud",
        "Mails, agenda et documents",
        "Notion et Figma",
        "Chercher ou créer une note Notion, inspecter un fichier Figma. "
        "Calendar, NotebookLM et consorts s'ouvrent dans ton Chrome habituel.",
        "Les jetons restent dans le trousseau. Si la page Notion par défaut "
        "est configurée, « crée une note sur X » suffit.",
        ("crée une note Notion sur la réunion", "inspecte ce fichier Figma"),
    ),
    Capability(
        "files",
        "Fichiers et navigateur",
        "Trouver et gérer des fichiers",
        "Recherche sur le disque, y compris avec une dictée approximative, "
        "puis ouverture, déplacement, traitement (PDF, images, code, audio).",
        "L'index local tolère les erreurs vocales. Je n'improvise jamais un "
        "`find` dans le shell pour ça. Les documents personnels (notes, PDF, "
        "code) ont aussi une recherche sémantique.",
        (
            "ai-je un fichier nommé rapport",
            "où est le PDF de la facture",
            "cherche dans mes notes la gestion du buffer audio",
        ),
    ),
    Capability(
        "browser",
        "Fichiers et navigateur",
        "Navigateur",
        "Ouvrir un site, remplir un formulaire, se connecter — toujours dans Google Chrome, jamais Firefox.",
        "Playwright pilote les pages complexes. Les liens, OAuth et recherches "
        "web passent par la politique Chrome du projet.",
        ("ouvre GitHub dans Chrome", "connecte-toi sur ce site"),
    ),
    # Création — highlight
    Capability(
        "create",
        "Création",
        "Images, vidéos, documents",
        "Je génère une image, une courte vidéo, ou un document complet, et "
        "je cherche aussi des photos existantes.",
        "Images et vidéos s'affichent dans l'interface. La galerie plein écran "
        "se ferme sur demande. Rien n'est « fait » tant que l'outil n'a pas "
        "rendu le fichier.",
        (
            "génère une image d'un orbe néon",
            "fais une vidéo de 6 secondes",
            "montre-moi des photos de Conakry",
            "ferme la galerie",
        ),
        highlight=True,
    ),
    # Mémoire — highlight
    Capability(
        "memory",
        "Mémoire et réflexion",
        "Mémoire et second cerveau",
        "Je retiens tes préférences. Je retrouve une info passée — "
        "conversation, note, fichier, commande, contact, projet, mail.",
        "La mémoire longue se sauve sans l'annoncer. Le second cerveau "
        "relie les sources : obligatoire pour « qu'avait-on utilisé », "
        "« le mois dernier », « qui était le contact du projet X ». "
        "Pour une vraie réflexion (comparer, planifier, déboguer un concept), "
        "je délègue à un agent plus fort et je te redis le résultat.",
        (
            "retenins que je préfère Kitty",
            "qu'avait-on utilisé pour l'audio",
            "réfléchis à cette architecture",
            "simule ma décision A ou B",
        ),
        highlight=True,
    ),
    # Automatisation
    Capability(
        "agents",
        "Automatisation et veilles",
        "Agents de fond",
        "Je délègue une mission longue : analyser un dépôt, installer un "
        "logiciel, surveiller un prix, attendre la fin d'un build, te prévenir "
        "quand tu arrives quelque part.",
        "Le sous-agent Antigravity (`agy`) a accès à mes outils. Je confirme "
        "la mise en file et je continue de parler. L'annonce arrive toute "
        "seule à la fin. Je ne compose jamais une URL GitHub de tête.",
        (
            "analyse ce dépôt et préviens-moi",
            "clone X dans Téléchargements",
            "préviens-moi si le prix baisse",
            "rappelle-moi quand j'arrive à la maison",
        ),
    ),
    Capability(
        "routines",
        "Automatisation et veilles",
        "Routines et mode proactif",
        "Un mot lance un enchaînement que tu as défini (« mode travail », "
        "« je pars »). Le mode proactif peut aussi prendre les devants.",
        "Le contenu des routines t'appartient : il change avec ton fichier. "
        "Je ne refais pas les étapes une par une si le nom existe.",
        ("mode travail", "mode nuit", "je pars", "sois proactif"),
    ),
    Capability(
        "focus",
        "Automatisation et veilles",
        "Bouclier anti-distraction",
        "Je bloque sites et réseaux le temps que tu te concentres.",
        "Garde dédiée : tu l'armes, tu la coupes. Elle ne se confond pas avec une fermeture d'application.",
        ("active le bouclier", "bloque les réseaux", "je me concentre"),
    ),
    Capability(
        "undo",
        "Automatisation et veilles",
        "Annuler la dernière action",
        "Je reviens en arrière sur une modification réversible : fichier, volume, luminosité.",
        "Une pile d'annulation locale. « Liste » montre l'historique sans rien changer.",
        ("annule", "annule ça", "qu'est-ce que je peux annuler"),
    ),
    # Dev
    Capability(
        "dev",
        "Développement et système",
        "Code, Git, Docker, réparation",
        "Commit propre, audit, conteneurs, services systemd, paquets Arch, "
        "debug de l'erreur à l'écran, et je peux me réparer moi-même.",
        "DevSecOps bloque un commit qui contient une clé. L'auto-debug lit "
        "la trace réelle (Python, Rust, C, Node, Go, shell) et propose un "
        "correctif. « Répare-toi » confie la dernière erreur à un agent de "
        "code en fond. Les paquets : uniquement pacman et yay, jamais apt.",
        (
            "fais un commit propre",
            "quels conteneurs tournent",
            "c'est quoi ce bug dans le terminal",
            "répare-toi",
            "y a-t-il des mises à jour",
        ),
    ),
    Capability(
        "shell",
        "Développement et système",
        "Commande shell",
        "J'exécute une commande locale certaine, puis je vérifie l'effet réel.",
        "Voie des opérations courtes et sûres sur cette machine (Arch, "
        "Hyprland). Si un nom de dépôt, d'URL ou de paquet est incertain, "
        "je délègue d'abord à l'agent au lieu d'inventer. sudo et le "
        "destructif passent par la carte de confirmation.",
        ("lance btop", "installe nmap", "montre l'espace disque"),
    ),
    # Spécialisé
    Capability(
        "tiktok",
        "Spécialisé",
        "Coach et suivi TikTok",
        "Le suivi de tes vidéos, et un coach qui diagnostique pourquoi ça "
        "plafonne, d'après les chiffres et la vidéo elle-même.",
        "Le tracker lit le compte. Le coach juge selon le genre (sketches IA "
        "compris), propose un verdict publie / corrige, le son, le commentaire "
        "à épingler. Une analyse vidéo part en fond : je t'annonce le résultat.",
        ("combien de vues a fait ma dernière vidéo", "diagnostique ce post", "laquelle je poste"),
    ),
    Capability(
        "prayer",
        "Spécialisé",
        "Heures de prière",
        "Prochaine prière, horaires du jour, activer ou couper le rappel vocal.",
        "Calcul astronomique selon ta position et la convention choisie (UOIF "
        "par défaut). Le rappel, s'il est actif, parle tout seul à l'heure.",
        ("prochaine prière", "horaires de prière aujourd'hui", "coupe le rappel de Fajr"),
    ),
    Capability(
        "sparring",
        "Spécialisé",
        "Entraînement oral",
        "Session d'entraînement : entretien, oral, contradiction. Je tiens le rôle et je te fais un retour.",
        "Persona dédiée, durée limitée, débrief à la fin. Ce n'est pas une "
        "conversation normale : le cadre est celui de l'exercice.",
        ("entraîne-moi à un entretien", "fais-moi un oral"),
    ),
    Capability(
        "travel_games",
        "Spécialisé",
        "Vols et jeux",
        "Chercher un vol (origines, dates), et mettre à jour un jeu installé.",
        "Le comparateur de vols sauve le résultat si tu le demandes. Le "
        "metteur à jour de jeu identifie le titre et lance la source adaptée.",
        ("trouve un vol Conakry–Paris en octobre", "mets à jour ce jeu"),
    ),
    Capability(
        "plugins",
        "Spécialisé",
        "Plugins et extensions",
        "Lister, activer, désactiver les plugins locaux, et voir les "
        "extensions autonomes proposées quand une demande répétée n'a pas "
        "d'outil.",
        "Les plugins vivent dans le dossier du projet. Une lacune réelle "
        "est journalisée ; une panne ou un refus de sécurité ne l'est pas.",
        ("liste les plugins", "désactive ce plugin"),
    ),
)


HIGHLIGHTS: tuple[Capability, ...] = tuple(c for c in CAPABILITIES if c.highlight)


# ── Intention vocale ─────────────────────────────────────────────────────────


def _fold(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in lowered if not unicodedata.combining(c))


_SKILLS_RE = re.compile(
    r"\b("
    r"competenc|fonctionnalit|ce que tu (?:sais|peux) faire|"
    r"qu(?:'|e )?est[- ]ce que tu (?:sais|peux|fais)|"
    r"que (?:peux|sais)[- ]?tu faire|"
    r"tes (?:outils|capacites|atouts)|"
    r"tu (?:sais|peux) faire quoi|quoi tu (?:sais|peux) faire|"
    r"a quoi tu sers|c'?est quoi tes|"
    r"guide d[' ]utilisation|comment (?:je te parle|t'utiliser|on t'utilise)|"
    r"tes meilleurs? (?:trucs|fonctions)"
    r")",
)
_WRITE_RE = re.compile(
    r"\b("
    r"note[- ]?(?:les|tout|ca|le)|noter (?:tout|ca|les|le guide)|"
    r"ecri(?:s|t|re)(?:[- ]le|[- ]les|[- ]tout|[- ]un fichier|[- ]le guide)?|"
    r"redige(?:r)? (?:le |un )?(?:guide|fichier)|"
    r"mets? a jour|actualis(?:e|er)|rafraich(?:is|ir)|"
    r"dans un fichier|fichier markdown|fichier md|en markdown"
    r")",
)
_OPEN_RE = re.compile(
    r"\b(?:ouvre|ouvrir|montre|affiche|lire|lis|vois)\b.{0,28}"
    r"\b(?:guide|fichier(?: markdown| md)?|markdown)\b",
)
_ACCEPT_RE = re.compile(
    r"^(oui|ouais|ok|okay|okey|d[' ]accord|dac|vas[- ]y|go|fait[- ]le|"
    r"fais[- ]le|s[' ]il te plait|volontiers|nickel|parfait|allez|"
    r"pourquoi pas|carrément|carrement|yes)\b",
)
_REFUSE_RE = re.compile(
    r"^(non|nan|pas maintenant|pas la peine|laisse|c'?est bon|"
    r"ca va|plus tard|oublie|nope|no)\b",
)


def parse_intent(query: str) -> str:
    """brief | write | open | accept | refuse."""
    folded = _fold(query).strip()
    if not folded:
        return "brief"
    short = folded[:80]
    if _REFUSE_RE.search(short) and not _SKILLS_RE.search(folded):
        return "refuse"
    if _OPEN_RE.search(folded) and not _SKILLS_RE.search(folded):
        return "open"
    if _WRITE_RE.search(folded):
        return "write"
    if _ACCEPT_RE.search(short) and not _SKILLS_RE.search(folded):
        return "accept"
    return "brief"


def resolve_action(action: str, query: str, state: str) -> str:
    """Décide brief / write / open / status / refuse.

    Une question « que sais-tu faire » reste un brief, même si le modèle a
    trop vite choisi write : on pose d'abord la question sur le fichier.
    L'écriture ne part que d'un consentement (oui, note-les, mets à jour…).
    """
    explicit = str(action or "").strip().casefold()
    intent = parse_intent(query)
    if explicit == "status":
        return "status"
    if intent == "refuse":
        return "refuse"
    if intent == "accept":
        return "open" if state == "current" else "write"
    if intent in {"write", "open"}:
        return intent
    if is_skills_question(query) and intent == "brief":
        return "brief"
    if explicit in {"write", "open", "refuse"}:
        return explicit
    return "brief"


def is_skills_question(query: str) -> bool:
    return bool(_SKILLS_RE.search(_fold(query)))


# ── Empreinte et état du fichier ─────────────────────────────────────────────


def catalog_fingerprint(caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    payload = [f"v{CATALOG_VERSION}"]
    for cap in caps:
        payload.append("|".join((cap.id, cap.title, cap.does, cap.how, " / ".join(cap.say))))
    digest = hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def read_stored_fingerprint(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except OSError:
        return None
    match = MARKER_RE.search(head)
    return match.group(1) if match else None


def guide_state(path: Path | None = None, caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    """missing | current | stale."""
    target = path or guide_path()
    if not target.is_file():
        return "missing"
    try:
        size = target.stat().st_size
    except OSError:
        return "missing"
    if size < 200:
        return "stale"
    stored = read_stored_fingerprint(target)
    if stored is None:
        return "stale"
    return "current" if stored == catalog_fingerprint(caps) else "stale"


# ── Rendu ────────────────────────────────────────────────────────────────────


def _today_fr() -> str:
    months = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    now = datetime.now()
    return f"{now.day} {months[now.month - 1]} {now.year}"


def render_markdown(caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    fingerprint = catalog_fingerprint(caps)
    highlights = tuple(c for c in caps if c.highlight)
    lines = [
        f"<!-- anogpt-competences fingerprint={fingerprint} catalog={CATALOG_VERSION} -->",
        "",
        "# Guide des compétences d'ANO-GPT",
        "",
        "Fichier tenu à jour par ANO-GPT. Il n'est **pas recréé** à chaque "
        "question : seulement s'il manque, ou si une capacité a changé.",
        f"Dernière mise à jour du contenu : {_today_fr()}.",
        "",
        "À l'oral, demande **« quelles sont tes compétences »** pour le résumé "
        "des meilleures fonctions. Demande **« ouvre le guide »** pour revoir "
        "ce fichier, ou **« mets à jour le guide »** s'il manque quelque chose.",
        "",
        "---",
        "",
        "## Comment me parler",
        "",
        "- Dis **« ANO »** pour me réveiller. Ensuite tu peux enchaîner sans le répéter.",
        "- Parle naturellement. Pas besoin de formules magiques ni de noms d'outils.",
        "- Dis **« stop »**, **« attends »** ou **« ANO stop »** pour me couper.",
        "- Pour une action sensible, une carte [Confirmer] / [Annuler] s'affiche : clique, ou dis oui / non.",
        "- Arch Linux uniquement : si tu dictes `apt`, je l'adapte en `pacman` ou `yay`.",
        "- Le navigateur, c'est toujours **Google Chrome**.",
        "",
        "## Ce que je fais le mieux",
        "",
    ]
    for cap in highlights:
        lines.append(f"- **{cap.title}.** {cap.does}")
    lines += ["", "## Toutes les capacités", ""]

    current_cat = ""
    for cap in caps:
        if cap.category != current_cat:
            current_cat = cap.category
            lines += [f"### {current_cat}", ""]
        examples = " », « ".join(cap.say)
        lines += [
            f"#### {cap.title}",
            "",
            f"**Ce que ça fait.** {cap.does}",
            "",
            f"**Comment ça marche.** {cap.how}",
            "",
            f"**Tu peux dire.** « {examples} ».",
            "",
        ]
    lines += [
        "---",
        "",
        "*Si ce fichier et ce qu'ANO-GPT fait vraiment divergent, dis simplement « mets à jour le guide ».*",
        "",
    ]
    return "\n".join(lines)


def _spoken_highlights(caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    if not any(cap.highlight for cap in caps):
        return "Je peux t'aider sur beaucoup de choses."
    return (
        "Voici ce que je fais le mieux. Je pilote ton ordinateur : applications, "
        "fenêtres, souris et clavier. Je vois ton écran et je peux te montrer "
        "où cliquer. Je gère la musique, YouTube, les appels et les SMS. Je te "
        "guide sur la carte, je me souviens de ce qui compte, et je peux créer "
        "des images, des vidéos ou des documents."
    )


def _offer(state: str, path: Path) -> str:
    if state == "current":
        return (
            f"Le guide complet est déjà prêt dans {path}. Je ne vais pas le recréer. Tu veux que je l'ouvre ?"
        )
    if state == "stale":
        return (
            "J'ai déjà un guide, mais il n'est plus à jour. Tu veux que je le "
            "rafraîchisse — ce que ça fait, comment ça marche, comment m'en parler ?"
        )
    return (
        "Le détail de tout le reste tient mieux à l'écrit. Tu veux que je note "
        "tout dans un fichier markdown, avec ce que ça fait, comment ça marche "
        "et comment m'en parler ?"
    )


def brief_speech(state: str, path: Path, caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    return f"{_spoken_highlights(caps)} {_offer(state, path)}"


# ── Écriture et ouverture ────────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _backup_stale(path: Path) -> None:
    if not path.is_file():
        return
    bak = path.with_name(path.stem + ".bak.md")
    with suppress(OSError):
        shutil.copy2(path, bak)


def write_guide(path: Path | None = None, caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    """Écrit seulement si besoin. Retourne created | updated | current."""
    target = path or guide_path()
    state = guide_state(target, caps)
    if state == "current":
        return "current"
    body = render_markdown(caps)
    if state == "stale":
        _backup_stale(target)
        _atomic_write(target, body)
        return "updated"
    _atomic_write(target, body)
    return "created"


def open_guide(path: Path) -> bool:
    if not path.is_file():
        return False
    studio = kit.which("markdown-studio")
    if studio:
        return kit.spawn([studio, str(path)]) is not None
    opener = kit.which("xdg-open")
    if opener:
        return kit.spawn([opener, str(path)]) is not None
    return kit.spawn(["xdg-open", str(path)]) is not None


def _show_card(player: Any, kind: str, title: str, body: str) -> None:
    show = getattr(player, "show_card", None) if player is not None else None
    if callable(show):
        with suppress(Exception):
            show(kind, title, body)


def _highlights_card_body(caps: tuple[Capability, ...] = CAPABILITIES) -> str:
    lines = [f"• {cap.title} — {cap.does}" for cap in caps if cap.highlight]
    return "\n".join(lines)


def _want_open(open_after: Any, default: bool) -> bool:
    if open_after is None or open_after == "":
        return default
    if isinstance(open_after, bool):
        return open_after
    return str(open_after).strip().casefold() in {"1", "true", "oui", "yes", "on"}


# ── Point d'entrée ───────────────────────────────────────────────────────────


@kit.action("capability_guide")
def capability_guide(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    query = str(params.get("query") or params.get("text") or "").strip()
    path = guide_path()
    state = guide_state(path)
    action = resolve_action(str(params.get("action") or ""), query, state)

    if action == "status":
        return f"Guide : {state}. Fichier : {path}. Empreinte catalogue : {catalog_fingerprint()}."

    if action == "refuse":
        return "D'accord, on reste sur l'essentiel. Redemande quand tu veux le guide."

    if action == "brief":
        speech = brief_speech(state, path)
        _show_card(player, "info", "Ce que je fais le mieux", _highlights_card_body())
        return speech

    if action == "open":
        if state == "missing":
            return "Je n'ai pas encore de guide. Tu veux que je note tout dans un fichier markdown ?"
        opened = open_guide(path)
        if state == "stale":
            suffix = (
                " Il n'est plus tout à fait à jour : dis « mets à jour le guide » "
                "si tu veux que je le rafraîchisse."
            )
        else:
            suffix = ""
        if opened:
            return f"J'ouvre {path}.{suffix}"
        return f"Le guide est dans {path}.{suffix}"

    # write
    outcome = write_guide(path)
    opened = False
    should_open = _want_open(params.get("open_after"), default=outcome != "current")
    if should_open:
        opened = open_guide(path)

    if outcome == "current":
        speech = f"Le guide est déjà à jour, je ne l'ai pas recréé. Il est dans {path}."
        if opened:
            speech += " Je te l'ouvre."
        else:
            speech += " Tu veux que je l'ouvre ?"
    elif outcome == "updated":
        speech = f"Guide rafraîchi. La version à jour est dans {path}."
        if opened:
            speech += " Je te l'ouvre."
    else:
        speech = (
            f"C'est noté. Tout le détail — ce que ça fait, comment ça marche, "
            f"comment m'en parler — est dans {path}."
        )
        if opened:
            speech += " Je te l'ouvre."

    if is_skills_question(query):
        speech = f"{_spoken_highlights()} {speech}"

    _show_card(player, "result", "Guide des compétences", speech)
    return speech
