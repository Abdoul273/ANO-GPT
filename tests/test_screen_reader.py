"""OCR local : préparation des captures sombres et des petites fenêtres."""
from __future__ import annotations

import io

from PIL import Image

from core.screen_reader import OCR_MIN_SIDE, prepare_for_ocr, wants_image


def test_wants_image_detects_visual_questions():
    assert wants_image("Décris ce graphique") is True
    assert wants_image("lis l'erreur du terminal") is False


def test_prepare_for_ocr_inverts_dark_terminal():
    dark = Image.new("L", (120, 80), 20)
    prepared = prepare_for_ocr(dark)
    probe = prepared.resize((16, 16))
    pixels = list(probe.get_flattened_data()) if hasattr(probe, "get_flattened_data") else list(probe.getdata())
    mean = sum(pixels) / max(1, len(pixels))
    assert mean > 150


def test_prepare_for_ocr_upscales_tiny_screenshots():
    tiny = Image.new("RGB", (200, 80), (240, 240, 240))
    prepared = prepare_for_ocr(tiny)
    assert max(prepared.size) >= OCR_MIN_SIDE
