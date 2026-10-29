"""L'orbe (donc la bouche du portrait) reçoit le son à l'instant où il est audible."""
from pathlib import Path

ENGINE = (Path(__file__).resolve().parent.parent / "core" / "audio_engine.py").read_text(encoding="utf-8")


def _output_loop_block() -> str:
    start = ENGINE.index("visual_delay = max(0.0")
    return ENGINE[start:start + 1600]


def test_le_niveau_et_le_spectre_sont_retardes_de_la_latence_de_sortie():
    block = _output_loop_block()
    assert '"_audio_output_latency"' in block
    assert "loop.call_later(\n                            visual_delay, self._show_output_level" in block
    assert "visual_delay, self._show_output_spectrum" in block


def test_aucun_envoi_visuel_immediat_dans_la_boucle_de_sortie():
    block = _output_loop_block()
    assert "self.ui.set_volume(" not in block
    assert "self.ui.feed_audio_spectrum(" not in block
