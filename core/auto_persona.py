"""Routage prudent des demandes vers un persona pédagogique spécialisé."""
from __future__ import annotations

import re


def detect_contextual_persona(text: str) -> str | None:
    """Identifie seulement des intentions assez explicites pour changer de posture."""
    folded = " ".join(str(text or "").casefold().split())
    if not folded:
        return None
    if re.search(
        r"\b(?:apprend(?:re|s)?|cours|leçon|lecon|pratiqu(?:e|er)|prononciation|"
        r"vocabulaire|grammaire|pronunciation)\b.{0,48}\b(?:anglais|english)\b|"
        r"\b(?:anglais|english)\b.{0,48}\b(?:apprend(?:re|s)?|cours|leçon|lecon|"
        r"pratiqu(?:e|er)|prononciation|pronunciation|vocabulaire|grammaire)\b",
        folded,
    ):
        return "english_learning_coach"
    if re.search(
        r"\b(?:prépar(?:e|er|ons)|aide.{0,20}(?:prépar|prepar)|simulation|simuler|"
        r"entraînement|entrainement)\b.{0,56}\b(?:entretien|interview)\b|"
        r"\b(?:entretien|interview)\b.{0,40}\b(?:embauche|emploi|job|recrutement|"
        r"candidature|prépar|prepar)\b",
        folded,
    ):
        return "job_interview_coach"
    return None
