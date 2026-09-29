"""Détecte « c'est fait » dit sans qu'aucun outil n'ait été appelé.

Le modèle vocal confirme parfois une action qu'il n'a jamais exécutée (« la
fenêtre est sur le bureau quatre » — et rien n'a bougé). Le prompt le lui
interdit, mais une règle de prompt ne se vérifie pas : ce module, lui, se
teste. Il ne décide de rien — il dit seulement si un tour ressemble à une
fausse confirmation, pour que la session force l'appel réel.
"""
from __future__ import annotations

import re
import unicodedata

# Verbes d'ordre : la phrase de l'utilisateur demande une action sur la machine.
_ORDER = re.compile(
    r"\b(ouvre|ouvrir|lance|lancer|ferme|fermer|quitte|deplace|deplacer|envoie|envoyer|"
    r"bouge|mets|met|mettre|passe|passer|va|active|activer|desactive|desactiver|coupe|couper|"
    r"allume|eteins|augmente|baisse|monte|diminue|change|changer|connecte|deconnecte|"
    r"redemarre|verrouille|capture|prends|joue|pause|arrete|supprime|efface|installe|"
    r"minimise|maximise|agrandis|reduis|bascule|range|organise)\b"
)
_QUESTION = re.compile(r"^(est[- ]ce|est ce|tu peux me dire|dis[- ]moi|qu'?est|quel|quelle|combien|pourquoi|comment)\b")

# Le modèle affirme que c'est accompli.
_CLAIM = re.compile(
    r"(c'est (fait|bon|ok|plie|regle|ouvert|ferme|lance|parti)|"
    r"\best (deja )?(ouverte?|fermee?|lancee?|sur le bureau|sur l'espace|deplacee?|active[e]?|desactive[e]?)|"
    r"\bsont (ouvertes?|fermees?)|"
    r"\bj'ai (bien )?(ouvert|ferme|lance|deplace|envoye|mis|active|desactive|coupe|augmente|baisse|change|"
    r"connecte|range|installe|supprime)|"
    r"\bvoila,? (c'est|j'ai|la|le)|\bmission accomplie|\bc'est chose faite|\bdeja fait)"
)


def _fold(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in lowered if not unicodedata.combining(c)).replace("’", "'")


def is_action_request(user_text: str) -> bool:
    folded = _fold(user_text).strip()
    return bool(folded) and not _QUESTION.search(folded) and bool(_ORDER.search(folded))


def claims_completion(model_text: str) -> bool:
    return bool(_CLAIM.search(_fold(model_text)))


def is_unbacked_claim(user_text: str, model_text: str, tool_calls: int) -> bool:
    """Vrai si une action était demandée, aucun outil appelé, et « c'est fait » dit."""
    return tool_calls == 0 and is_action_request(user_text) and claims_completion(model_text)


CORRECTION = (
    "[SYSTÈME — correction] Tu viens d'annoncer « {claim} » mais tu n'as appelé AUCUN outil : "
    "rien n'a été exécuté. Appelle maintenant l'outil qui réalise « {request} », "
    "puis annonce le résultat réel. Si aucun outil ne convient, dis franchement que tu n'as "
    "pas pu le faire. Ne prétends rien.]"
)
