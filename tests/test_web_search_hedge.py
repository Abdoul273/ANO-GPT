"""La recherche couverte rend le meilleur résultat disponible sans attendre
l'échec complet du moteur principal."""
import time

from actions import web_search as ws


def _sleep_then(value, delay):
    def _fn():
        time.sleep(delay)
        return value
    return _fn


def test_primary_wins_when_fast():
    assert ws._hedged(lambda: "gemini", _sleep_then("ddg", 0.2), timeout=2, hedge_after=0.3) == "gemini"


def test_primary_failure_triggers_immediate_fallback():
    def _fail():
        raise RuntimeError("quota")
    started = time.monotonic()
    assert ws._hedged(_fail, _sleep_then("ddg", 0.05), timeout=2, hedge_after=0.5) == "ddg"
    assert time.monotonic() - started < 0.5


def test_slow_primary_is_covered_by_fallback():
    started = time.monotonic()
    result = ws._hedged(_sleep_then("late", 5.0), _sleep_then("ddg", 0.05), timeout=2.0, hedge_after=0.2)
    assert result == "ddg"
    assert time.monotonic() - started < 2.0


def test_primary_arriving_during_grace_is_preferred():
    result = ws._hedged(_sleep_then("gemini", 0.5), _sleep_then("ddg", 0.05), timeout=3, hedge_after=0.2)
    assert result == "gemini"
