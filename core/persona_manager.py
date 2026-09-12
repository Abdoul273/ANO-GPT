"""core/persona_manager.py — Gestionnaire de modes métiers et personas dynamiques pour ANO-GPT.

Fournit :
1. Chargement dynamique des personas depuis les configurations YAML (config/personas/).
2. Détection en temps réel des commandes vocales de commutation ("Jarvis, passe en mode DevOps", "Passe en mode Majordome", etc.).
3. Re-génération à chaud des instructions système Gemini Live sans couper le WebSocket.
4. Ajustement simultané des accents de couleur de l'orbe UI (Cyan = Jarvis, Vert = DevOps, Rouge = Sentinel, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)

# Répertoire par défaut des personas
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "personas"


def _normalize_text(text: str) -> str:
    """Normalise un texte (minuscules, sans accents, sans ponctuation superflue) pour le matching."""
    if not text:
        return ""
    text = text.lower().strip()
    # Supprime les accents
    nfkd = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remplace la ponctuation par des espaces
    text = re.sub(r"[^\w\s]", " ", text)
    # Compresse les espaces multiples
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Persona:
    """Représentation structurée d'un mode métier ou persona."""
    id: str
    name: str
    display_name: str
    description: str
    accent_color: str
    orb_palette: Dict[str, Any] = field(default_factory=dict)
    user_address: str = ""
    voice_name: Optional[str] = None
    prosody_preset: str = "neutre"
    temperature: float = 0.2
    block_interruptions: bool = False
    voice_triggers: List[str] = field(default_factory=list)
    system_prompt: str = ""
    acknowledgments: List[str] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def get_acknowledgment(self) -> str:
        """Retourne une confirmation orale type dans le style du persona."""
        if self.acknowledgments:
            return self.acknowledgments[0]
        # Formules par défaut selon l'ID
        if self.id == "ironman_jarvis":
            return "À vos ordres, Monsieur. Mode majordome rétabli."
        elif self.id == "senior_devops":
            return "Mode DevOps engagé. Système et terminal sous contrôle."
        elif self.id == "cyber_sentinel":
            return "Cyber Sentinel armé. Surveillance active du périmètre."
        elif self.id == "zen_focus":
            return "Mode concentration actif."
        return f"Mode {self.display_name} activé."

    def format_prompt_block(self) -> str:
        """Génère le bloc textuel d'instructions pour ce persona."""
        lines = [
            f"[MODE MÉTIER ACTIF — {self.display_name.upper()}]",
            f"DESCRIPTION: {self.description}",
        ]
        if self.user_address:
            lines.append(f"ADRESSE UTILISATEUR: Tu t'adresses TOUJOURS à l'utilisateur en disant '{self.user_address}'.")
        else:
            lines.append("ADRESSE UTILISATEUR: Directe, sobre, sans formule protocolaire.")
        
        lines.append(f"POSTURE & RÈGLES SPÉCIFIQUES:\n{self.system_prompt.strip()}")
        return "\n".join(lines)


@dataclass
class PersonaSwitchResult:
    """Résultat d'une tentative de basculement de persona."""
    success: bool
    persona: Persona
    previous_persona: Optional[Persona] = None
    directive: str = ""
    accent_color: str = ""
    message: str = ""
    acknowledgment: str = ""
    hot_reloaded: bool = False
    error: Optional[str] = None


# Personas de secours embarqués au cas où les fichiers YAML seraient indisponibles
_BUILTIN_FALLBACK_PERSONAS: Dict[str, Dict[str, Any]] = {
    "ironman_jarvis": {
        "id": "ironman_jarvis",
        "name": "ironman_jarvis",
        "display_name": "Majordome",
        "description": "Majordome britannique ultra-poli, flegmatique, concis, appelle l'utilisateur 'Monsieur'.",
        "accent_color": "#00d4ff",
        "orb_palette": {
            "core": "#78e1ff", "halo": "#00aaff", "wire": "#00beff", "hot": "#e1faff",
            "pulse_speed": 0.85, "spin": 1.0,
        },
        "user_address": "Monsieur",
        "voice_name": "Charon",
        "prosody_preset": "flegmatique",
        "temperature": 0.25,
        "block_interruptions": false if False else False,
        "voice_triggers": ["majordome", "mode majordome", "passe en mode majordome", "jarvis", "mode jarvis", "ironman", "butler"],
        "system_prompt": (
            "Tu es ANO-GPT, le majordome britannique personnel et l'intelligence artificielle de Monsieur.\n"
            "Adresse-toi toujours à l'utilisateur en disant 'Monsieur'. Sois flegmatique, extrêmement poli, concis et efficace."
        ),
        "acknowledgments": ["À vos ordres, Monsieur. Mode majordome rétabli."],
    },
    "senior_devops": {
        "id": "senior_devops",
        "name": "senior_devops",
        "display_name": "Senior DevOps / SRE",
        "description": "Expert Rust/Linux pointilleux, réponses techniques denses, commandes directes sans bavardage.",
        "accent_color": "#00ff88",
        "orb_palette": {
            "core": "#78ffcb", "halo": "#00e696", "wire": "#00f5af", "hot": "#e6fff5",
            "pulse_speed": 1.6, "spin": 1.8,
        },
        "user_address": "Anonymous",
        "voice_name": "Fenrir",
        "prosody_preset": "technique",
        "temperature": 0.10,
        "block_interruptions": False,
        "voice_triggers": ["devops", "mode devops", "passe en mode devops", "senior devops", "rust", "expert rust", "sysadmin", "sre"],
        "system_prompt": (
            "Tu es Senior DevOps / SRE et Architecte Systèmes Linux & Rust.\n"
            "Réponses denses, techniques et directes sans aucun bavardage superflu. Syntaxe exacte et code prêt à l'emploi."
        ),
        "acknowledgments": ["Mode DevOps engagé. Prêt pour le déploiement."],
    },
    "cyber_sentinel": {
        "id": "cyber_sentinel",
        "name": "cyber_sentinel",
        "display_name": "Cyber Sentinel",
        "description": "Orienté sécurité offensive/défensive, analyse continue des ports et des logs.",
        "accent_color": "#ff3355",
        "orb_palette": {
            "core": "#ff3c5a", "halo": "#dc1432", "wire": "#ff5a6e", "hot": "#ffe6eb",
            "pulse_speed": 2.2, "spin": 2.0,
        },
        "user_address": "Opérateur",
        "voice_name": "Aoede",
        "prosody_preset": "tactique",
        "temperature": 0.10,
        "block_interruptions": False,
        "voice_triggers": ["sentinel", "sentinelle", "mode sentinelle", "cyber sentinel", "sécurité", "mode sécurité", "secops", "soc"],
        "system_prompt": (
            "Tu es Cyber Sentinel, analyste SOC et officier de cyberdéfense tactique.\n"
            "Surveille activement les ports, les processus et les logs. Niveaux de menace stricts : [INFO], [ATTENTION], [CRITIQUE]."
        ),
        "acknowledgments": ["Cyber Sentinel armé. Surveillance active du périmètre."],
    },
    "zen_focus": {
        "id": "zen_focus",
        "name": "zen_focus",
        "display_name": "Zen Focus",
        "description": "Minimaliste, chuchote, bloque toutes les interruptions inutiles.",
        "accent_color": "#8f5cff",
        "orb_palette": {
            "core": "#cd96ff", "halo": "#963cff", "wire": "#af69ff", "hot": "#f5ebff",
            "pulse_speed": 0.6, "spin": 0.5,
        },
        "user_address": "",
        "voice_name": "Puck",
        "prosody_preset": "chuchote",
        "temperature": 0.05,
        "block_interruptions": True,
        "voice_triggers": ["zen", "mode zen", "passe en mode zen", "focus", "mode focus", "concentration", "calme", "silence", "chuchote"],
        "system_prompt": (
            "Tu es Zen Focus. Minimalisme absolu, chuchote, débit lent et apaisant. Bloque toute interruption inutile.\n"
            "Réponses de 1 à 5 mots maximum."
        ),
        "acknowledgments": ["Mode concentration actif."],
    },
}


class PersonaManager:
    """Gestionnaire central des modes métiers et personas d'ANO-GPT."""

    def __init__(self, config_dir: Optional[Union[Path, str]] = None) -> None:
        self.config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
        self.personas: Dict[str, Persona] = {}
        self._current_persona: Optional[Persona] = None
        self._listeners: List[Callable[[Optional[Persona], Persona], None]] = []
        self._history: List[Dict[str, Any]] = []

        # Charge les personas
        self.reload()

    @property
    def current_persona(self) -> Persona:
        """Retourne le persona actif (par défaut ironman_jarvis)."""
        if self._current_persona is None:
            self._current_persona = (
                self.personas.get("ironman_jarvis")
                or next(iter(self.personas.values()), None)
            )
        return self._current_persona

    def reload(self) -> None:
        """Recharge tous les fichiers YAML de configuration."""
        self.personas.clear()

        # 1. Chargement des fichiers YAML si le dossier existe
        if self.config_dir.is_dir():
            for yaml_file in sorted(self.config_dir.glob("*.yaml")) + sorted(self.config_dir.glob("*.yml")):
                try:
                    with open(yaml_file, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if isinstance(data, dict) and "id" in data:
                        persona = self._create_persona_from_dict(data)
                        self.personas[persona.id] = persona
                except Exception as exc:
                    logger.warning(f"[PersonaManager] Erreur chargement {yaml_file}: {exc}")

        # 2. Complète avec les fallbacks si manquant
        for pid, fallback_data in _BUILTIN_FALLBACK_PERSONAS.items():
            if pid not in self.personas:
                self.personas[pid] = self._create_persona_from_dict(fallback_data)

        # Rétablit le persona actif si possible
        if self._current_persona and self._current_persona.id in self.personas:
            self._current_persona = self.personas[self._current_persona.id]
        else:
            self._current_persona = self.personas.get("ironman_jarvis") or next(iter(self.personas.values()))

    def _create_persona_from_dict(self, data: Dict[str, Any]) -> Persona:
        """Instancie un objet Persona depuis un dictionnaire."""
        pid = str(data.get("id") or data.get("name") or "default").strip().lower()
        return Persona(
            id=pid,
            name=str(data.get("name") or pid).strip(),
            display_name=str(data.get("display_name") or pid.title()).strip(),
            description=str(data.get("description") or "").strip(),
            accent_color=str(data.get("accent_color") or "#00d4ff").strip(),
            orb_palette=dict(data.get("orb_palette") or {}),
            user_address=str(data.get("user_address") or "").strip(),
            voice_name=data.get("voice_name"),
            prosody_preset=str(data.get("prosody_preset") or "neutre").strip(),
            temperature=float(data.get("temperature", 0.2)),
            block_interruptions=bool(data.get("block_interruptions", False)),
            voice_triggers=[str(t).strip().lower() for t in (data.get("voice_triggers") or [])],
            system_prompt=str(data.get("system_prompt") or "").strip(),
            acknowledgments=[str(a).strip() for a in (data.get("acknowledgments") or [])],
            raw_data=data,
        )

    def list_personas(self) -> List[Persona]:
        """Retourne la liste ordonnée de tous les personas disponibles."""
        return list(self.personas.values())

    def get_persona(self, name_or_id: str) -> Optional[Persona]:
        """Recherche un persona par son ID, nom ou nom affiché."""
        if not name_or_id:
            return None
        target = _normalize_text(name_or_id)
        # 1. Correspondance directe ID
        if name_or_id.lower() in self.personas:
            return self.personas[name_or_id.lower()]
        # 2. Recherche par normalisation
        for p in self.personas.values():
            if _normalize_text(p.id) == target or _normalize_text(p.name) == target or _normalize_text(p.display_name) == target:
                return p
        # 3. Recherche dans les déclencheurs directs
        for p in self.personas.values():
            for trig in p.voice_triggers:
                if _normalize_text(trig) == target:
                    return p
        return None

    def detect_trigger(self, utterance: str) -> Optional[Persona]:
        """Détecte si une phrase orale ou textuelle contient une commande de bascule de persona.
        
        Exemples reconnus :
        - "Jarvis, passe en mode DevOps"
        - "Passe en mode Majordome"
        - "Active le mode sentinelle"
        - "Mets-toi en mode zen"
        - "Mode sécurité"
        - "Change de persona pour senior devops"
        """
        if not utterance:
            return None

        norm_text = _normalize_text(utterance)
        if not norm_text:
            return None

        # 1. Expressions régulières courantes de commande vocale
        # Ex: "(jarvis)? (peux-tu)? (passe|bascule|active|mets|mets-toi) (en|le)? (mode|persona)? <cible>"
        action_patterns = [
            r"(?:jarvis|ano-?gpt|hey jarvis)?\s*(?:peux\s+tu|veuillez)?\s*(?:passe|passer|bascule|basculer|active|activer|mets|mettre|mettez|mets\s+toi|mettez\s+vous|enclenche|enclencher|engage|engager|reviens|remets)\s+(?:en\s+|le\s+|sur\s+le\s+|dans\s+le\s+|au\s+)?(?:mode\s+|persona\s+|profil\s+)?(?P<target>.+)",
            r"^(?:mode|persona|profil)\s+(?P<target>.+)",
        ]

        extracted_targets: List[str] = []
        for pat in action_patterns:
            m = re.search(pat, norm_text)
            if m:
                extracted = m.group("target").strip()
                if extracted:
                    extracted_targets.append(extracted)

        # Si un motif a extrait une cible, vérifie si elle correspond à un persona
        for target in extracted_targets:
            for p in self.personas.values():
                # Vérifie ID ou nom
                if target in (_normalize_text(p.id), _normalize_text(p.name), _normalize_text(p.display_name)):
                    return p
                # Vérifie chacun des triggers du persona
                for trig in p.voice_triggers:
                    norm_trig = _normalize_text(trig)
                    if norm_trig == target or norm_trig.endswith(target) or target.endswith(norm_trig):
                        return p

        # 2. Vérification si l'énoncé entier est un déclencheur direct (ex: "mode devops", "majordome", "senior devops")
        for p in self.personas.values():
            for trig in p.voice_triggers:
                norm_trig = _normalize_text(trig)
                if norm_text == norm_trig or norm_text == f"mode {norm_trig}":
                    return p

        # 3. Vérification des déclencheurs multi-mots explicites (ex: "mode devops", "mode majordome", "expert rust", "cyber sentinel")
        # Les mots uniques ne déclenchent pas seuls au milieu d'une phrase pour éviter les faux positifs.
        candidate_matches: List[tuple[int, Persona]] = []
        for p in self.personas.values():
            for trig in p.voice_triggers:
                norm_trig = _normalize_text(trig)
                if not norm_trig or " " not in norm_trig:
                    continue
                # Vérifie présence avec délimiteurs de mots
                pattern = rf"(?:\b|^){re.escape(norm_trig)}(?:\b|$)"
                if re.search(pattern, norm_text):
                    candidate_matches.append((len(norm_trig), p))

        if candidate_matches:
            # Retourne le match le plus long / spécifique
            candidate_matches.sort(key=lambda x: x[0], reverse=True)
            return candidate_matches[0][1]

        return None

    def build_system_instruction(
        self,
        persona: Optional[Persona] = None,
        base_prompt: str = "",
        user_name: str = "",
        asst_name: str = "",
    ) -> str:
        """Assemble les instructions système complètes incluant le persona actif."""
        target = persona or self.current_persona
        parts: List[str] = []

        # 1. Spécification d'identité & d'adresse
        addr_str = target.user_address or user_name or "Anonymous"
        name_str = asst_name or "ANO-GPT"
        identity_block = (
            f"[IDENTITÉ & POSTURE GLOBALE]\n"
            f"Nom de l'assistant : {name_str}\n"
            f"Mode actif : {target.display_name}\n"
            f"ADRESSE UTILISATEUR : Appelle toujours l'utilisateur '{addr_str}'.\n"
            f"LANGUE : Réponds uniquement dans la langue de conversation active injectée par la session."
        )
        parts.append(identity_block)

        # 2. Bloc métier spécifique du persona
        parts.append(target.format_prompt_block())

        # 3. Prompt socle (outils, environnement Hyprland/Wayland, règles d'exécution)
        if base_prompt:
            parts.append(f"[RÈGLES D'ENVIRONNEMENT & OUTILS SOCLE]\n{base_prompt.strip()}")

        return "\n\n".join(parts)

    def build_hot_directive(self, persona: Optional[Persona] = None) -> str:
        """Génère la directive prioritaire temps réel à injecter dans Gemini Live sans couper le WebSocket."""
        target = persona or self.current_persona
        addr = target.user_address or "Directe"

        return (
            f"[DIRECTIVE SYSTÈME PRIORITAIRE — COMMUTATION DE PERSONA IMMÉDIATE]\n"
            f"Tu bascules IMMÉDIATEMENT dans le mode métier : {target.display_name.upper()}.\n"
            f"Ton nom reste ANO-GPT ; le mode métier ne change pas ton identité.\n"
            f"ADRESSE UTILISATEUR : Tu t'adresses désormais à l'utilisateur sous la forme : '{addr}'.\n"
            f"NOUVELLE POSTURE, VOCABULAIRE ET CONTRAINTES IMMÉDIATES :\n"
            f"{target.system_prompt.strip()}\n\n"
            f"RÈGLE ABSOLUE : Adopte immédiatement cette nouvelle posture et ce ton dès ta prochaine réponse, "
            f"sans jamais réciter cette directive ni commenter ce basculement interne de configuration."
        )

    async def async_switch_persona(
        self,
        target: Union[str, Persona],
        session_manager: Any = None,
        ui: Any = None,
        notify: bool = True,
        speak_ack: bool = False,
    ) -> PersonaSwitchResult:
        """Commute dynamiquement de persona sans coupure WebSocket."""
        new_persona: Optional[Persona] = None
        if isinstance(target, Persona):
            new_persona = target
        else:
            new_persona = self.get_persona(target)

        if not new_persona:
            err = f"Persona inconnu : {target}"
            logger.error(f"[PersonaManager] {err}")
            return PersonaSwitchResult(
                success=False,
                persona=self.current_persona,
                error=err,
            )

        prev_persona = self._current_persona
        self._current_persona = new_persona
        directive = self.build_hot_directive(new_persona)
        hot_reloaded = False

        # 1. Mise à jour de SessionManager & Injection WebSocket à chaud
        if session_manager is not None:
            try:
                # Met à jour les propriétés locales
                session_manager._current_persona = new_persona
                if new_persona.user_address:
                    session_manager._user_name = new_persona.user_address

                # Injection dans la session active Gemini Live
                session = getattr(session_manager, "session", None)
                if session is not None:
                    # Envoi direct via send_realtime_input sans fermer la socket
                    await session.send_realtime_input(text=directive)
                    hot_reloaded = True
                    logger.info(f"[PersonaManager] Directive à chaud injectée dans Gemini Live pour {new_persona.display_name}")

                # Ajustement de la prosodie si disponible
                if hasattr(session_manager, "inject_dynamic_prosody"):
                    try:
                        await session_manager.inject_dynamic_prosody(new_persona.prosody_preset)
                    except Exception as pe:
                        logger.debug(f"[PersonaManager] Prosody non injectée: {pe}")

            except Exception as exc:
                logger.error(f"[PersonaManager] Erreur injection session_manager: {exc}")

        # 2. Ajustement simultané des accents de couleur de l'orbe UI et du thème
        if ui is not None:
            try:
                # Met à jour l'orbe et la fenêtre
                if hasattr(ui, "set_accent_color"):
                    ui.set_accent_color(new_persona.accent_color, new_persona.orb_palette)
                elif hasattr(ui, "_win") and hasattr(ui._win, "_accent_sig"):
                    ui._win._accent_sig.emit(new_persona.accent_color, new_persona.orb_palette)

                if hasattr(ui, "write_log"):
                    ui.write_log(
                        f"SYS : Mode métier activé — [{new_persona.display_name}] "
                        f"(Accent: {new_persona.accent_color})"
                    )
            except Exception as exc:
                logger.warning(f"[PersonaManager] Erreur mise à jour UI: {exc}")

        # 3. Notification des observateurs
        for listener in list(self._listeners):
            try:
                listener(prev_persona, new_persona)
            except Exception as exc:
                logger.warning(f"[PersonaManager] Erreur listener: {exc}")

        # 4. Historique
        self._history.append({
            "timestamp": time.time(),
            "from": prev_persona.id if prev_persona else None,
            "to": new_persona.id,
            "hot_reloaded": hot_reloaded,
        })

        ack = new_persona.get_acknowledgment()
        msg = f"Mode {new_persona.display_name} opérationnel."

        # Prononciation optionnelle de l'accusé de réception
        if speak_ack and session_manager and hasattr(session_manager, "speak"):
            try:
                session_manager.speak(ack)
            except Exception:
                pass

        return PersonaSwitchResult(
            success=True,
            persona=new_persona,
            previous_persona=prev_persona,
            directive=directive,
            accent_color=new_persona.accent_color,
            message=msg,
            acknowledgment=ack,
            hot_reloaded=hot_reloaded,
        )

    def switch_persona(
        self,
        target: Union[str, Persona],
        session_manager: Any = None,
        ui: Any = None,
        notify: bool = True,
        speak_ack: bool = False,
    ) -> PersonaSwitchResult:
        """Version synchrone de commutation pour compatibilité avec appels non-async."""
        try:
            loop = asyncio.get_running_loop()
            # Si on est déjà dans une boucle d'événements, on schedule la coroutine
            task = loop.create_task(
                self.async_switch_persona(
                    target, session_manager=session_manager, ui=ui, notify=notify, speak_ack=speak_ack
                )
            )
            # Met à jour le persona courant immédiatement pour cohérence synchrone
            new_p = target if isinstance(target, Persona) else self.get_persona(target)
            if new_p:
                prev = self._current_persona
                self._current_persona = new_p
                # Mise à jour UI synchrone rapide
                if ui and hasattr(ui, "set_accent_color"):
                    try:
                        ui.set_accent_color(new_p.accent_color, new_p.orb_palette)
                    except Exception:
                        pass
                return PersonaSwitchResult(
                    success=True,
                    persona=new_p,
                    previous_persona=prev,
                    directive=self.build_hot_directive(new_p),
                    accent_color=new_p.accent_color,
                    message=f"Mode {new_p.display_name} activé.",
                    acknowledgment=new_p.get_acknowledgment(),
                    hot_reloaded=False,
                )
        except RuntimeError:
            # Pas de boucle courante : exécution via asyncio.run
            return asyncio.run(
                self.async_switch_persona(
                    target, session_manager=session_manager, ui=ui, notify=notify, speak_ack=speak_ack
                )
            )

        new_p = target if isinstance(target, Persona) else self.get_persona(target)
        if not new_p:
            return PersonaSwitchResult(success=False, persona=self.current_persona, error=f"Inconnu: {target}")
        return PersonaSwitchResult(
            success=True, persona=new_p, previous_persona=self._current_persona,
            directive=self.build_hot_directive(new_p), accent_color=new_p.accent_color,
        )

    def add_listener(self, callback: Callable[[Optional[Persona], Persona], None]) -> None:
        """Enregistre un écouteur appelé à chaque commutation."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Optional[Persona], Persona], None]) -> None:
        """Supprime un écouteur enregistré."""
        if callback in self._listeners:
            self._listeners.remove(callback)


# Instance globale singleton
_PERSONA_MANAGER: Optional[PersonaManager] = None


def get_persona_manager() -> PersonaManager:
    """Accès au singleton PersonaManager."""
    global _PERSONA_MANAGER
    if _PERSONA_MANAGER is None:
        _PERSONA_MANAGER = PersonaManager()
    return _PERSONA_MANAGER


def set_persona_manager(manager: PersonaManager) -> None:
    """Définit le singleton PersonaManager (utile pour les tests)."""
    global _PERSONA_MANAGER
    _PERSONA_MANAGER = manager
