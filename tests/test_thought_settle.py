"""La bulle « Synthèse des résultats… » ne doit pas survivre à un outil muet."""

import time

from core.thought_streamer import ThoughtStreamer


def test_la_bulle_s_efface_quand_le_modele_reste_muet_apres_l_outil():
    events = []
    streamer = ThoughtStreamer(ui_callback=lambda text, active: events.append((text, active)))
    streamer.SETTLE_SECONDS = 0.1
    streamer.feed_tool_start("close_app")
    streamer.feed_tool_end("close_app")
    assert events[-1] == ("Synthèse des résultats...", True)
    time.sleep(0.4)
    assert events[-1] == ("", False)
    assert not streamer.is_thinking


def test_un_nouvel_outil_annule_le_garde_fou():
    streamer = ThoughtStreamer(ui_callback=lambda *_: None)
    streamer.SETTLE_SECONDS = 0.1
    streamer.feed_tool_start("a")
    streamer.feed_tool_end("a")
    streamer.feed_tool_start("b")
    time.sleep(0.3)
    assert streamer.is_thinking
    streamer.on_turn_complete()
