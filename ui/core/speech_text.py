from __future__ import annotations

import re

_SENTENCE_END_RE = re.compile(r"([.!?…]+|[。！？]+)[\"'”»)]*\s+")


def _advance_current_sentence(current: str, fragment: str) -> str:
    """Return only the sentence currently being spoken.

    Realtime transcription arrives as fragments. This keeps a partial sentence
    while it grows, but once punctuation closes it and new text begins, the old
    sentence is dropped from the HUD bubble.
    """
    fragment = (fragment or "").strip()
    current = (current or "").strip()
    if not fragment:
        return current
    if not current:
        return fragment
    if current.rstrip().endswith((".", "!", "?", "…", "。", "！", "？")):
        return fragment

    combined = f"{current} {fragment}".strip()
    matches = list(_SENTENCE_END_RE.finditer(combined))
    if matches and matches[-1].end() < len(combined):
        return combined[matches[-1].end():].strip()
    return combined
