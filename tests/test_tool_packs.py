"""Outils par contexte : le noyau suffit à froid, les paquets s'ouvrent seuls."""

import json
from pathlib import Path

from core import tool_packs as tp
from core.tool_dispatcher import TOOL_DECLARATIONS


ROOT = Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")


def _weight(declarations) -> int:
    return len(json.dumps(list(declarations), ensure_ascii=False)) // 4


def test_le_decoupage_couvre_exactement_les_outils_declares():
    declared = {tool["name"] for tool in TOOL_DECLARATIONS}
    covered = tp.CORE | tp.all_pack_tools()
    assert tp.CORE & tp.all_pack_tools() == set(), "un outil ne peut pas être dans deux endroits"
    assert covered - declared == set(), "paquet qui référence un outil inexistant"
    assert declared - covered == set(), "outil déclaré que personne ne revendique"


def test_le_noyau_divise_le_preambule_par_deux():
    tout = _weight(TOOL_DECLARATIONS) + len(PROMPT) // 4
    noyau = (_weight(tp.select_declarations(TOOL_DECLARATIONS, frozenset()))
             + len(tp.filter_prompt(PROMPT, frozenset())) // 4)
    assert noyau < tout * 0.55, f"{noyau} jetons contre {tout} : gain insuffisant"


def test_a_froid_seul_le_carnet_du_telephone_est_visible():
    """La confusion d'origine : contacts_control pris pour le carnet Android.

    À froid, « ai-je un contact nommé X ? » n'ouvre aucun paquet, donc le
    carnet local du PC n'est même pas proposé au modèle.
    """
    assert tp.resolve("Est-ce que j'ai un contact nommé Frère Aladji ?") == frozenset()
    noyau = {d["name"] for d in tp.select_declarations(TOOL_DECLARATIONS, frozenset())}
    assert "phone_contacts" in noyau
    assert "contacts_control" not in noyau


def test_le_telephone_repond_toujours_sans_reconnexion():
    for phrase in ("appelle maman", "raccroche", "envoie un SMS à maman",
                   "quelle heure est-il", "montre-moi la carte", "où suis-je"):
        assert tp.resolve(phrase) == frozenset(), phrase


def test_chaque_domaine_ouvre_son_paquet():
    attendus = {
        "mets un peu de musique": "musique",
        "lis mes mails": "bureautique",
        "ouvre la caméra frontale": "camera",
        "où est la pharmacie la plus proche": "navigation",
        "fais un commit et pousse sur GitHub": "dev",
        "cherche le fichier rapport.pdf": "fichiers",
        "génère une image de chat": "images",
        "préviens-moi dès que j'arrive à la maison": "assistanat",
    }
    for phrase, pack in attendus.items():
        assert tp.resolve(phrase) == {pack}, f"{phrase} → {tp.resolve(phrase)}"


def test_les_accents_et_la_casse_ne_changent_rien():
    assert tp.resolve("OÙ EST LA PHARMACIE LA PLUS PROCHE") == {"navigation"}
    assert tp.resolve("ou est la pharmacie la plus proche") == {"navigation"}


def test_un_outil_inconnu_du_decoupage_reste_toujours_visible():
    """Un plugin ou un outil tout juste ajouté ne doit jamais disparaître."""
    extra = [{"name": "plugin_maison", "description": "test"}]
    noms = {d["name"] for d in tp.select_declarations(
        list(TOOL_DECLARATIONS) + extra, frozenset())}
    assert "plugin_maison" in noms


def test_un_paquet_ouvert_ramene_ses_outils_et_son_guide():
    noms = {d["name"] for d in tp.select_declarations(TOOL_DECLARATIONS, {"musique"})}
    assert {"music_control", "youtube_video", "download_music"} <= noms
    assert "email_control" not in noms
    guide = tp.filter_prompt(PROMPT, {"musique"})
    assert "music_control —" in guide
    assert "email_control —" not in guide


def test_le_guide_garde_les_consignes_du_noyau_et_les_regles_generales():
    guide = tp.filter_prompt(PROMPT, frozenset())
    for outil in ("phone_call", "phone_sms", "show_map", "reminder", "save_memory"):
        if f"{outil} —" in PROMPT:
            assert f"{outil} —" in guide, outil
    # Les règles qui tiennent la conduite du modèle ne dépendent d'aucun outil.
    for regle in ("FONDAMENTAL", "CONFIRMATION HUMAINE REQUISE", "EN CAS DE DOUTE"):
        assert regle in guide, regle


def test_tous_les_paquets_ouverts_rendent_le_prompt_intact():
    assert tp.filter_prompt(PROMPT, set(tp.PACKS)) == PROMPT
    complet = {d["name"] for d in tp.select_declarations(TOOL_DECLARATIONS, set(tp.PACKS))}
    assert complet == {t["name"] for t in TOOL_DECLARATIONS}


def test_le_moteur_de_session_est_reellement_cable():
    """Le câblage compte autant que le découpage : sans lui, rien ne filtre."""
    session = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")
    assert "tool_packs.select_declarations(" in session, "Live reçoit encore tous les outils"
    assert "tool_packs.filter_prompt(" in session, "le guide du prompt n'est pas découpé"
    assert "def _extend_toolkit(" in session

    principal = (ROOT / "main.py").read_text(encoding="utf-8")
    # Les méthodes de SessionManager sont liées une par une : une méthode
    # oubliée ici n'existe tout simplement pas sur l'assistant.
    assert "_extend_toolkit = SessionManager._extend_toolkit" in principal
    assert "self._active_tool_packs" in principal
    assert "self._toolkit_reconnect_requested" in principal


def test_le_cerveau_externe_garde_tous_les_outils():
    """Le relais MCP exécute lui-même : le brider le rendrait inutile."""
    session = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")
    relay = session.split("def _relay_declarations")[1].split("def ")[0]
    assert "tool_packs" not in relay
