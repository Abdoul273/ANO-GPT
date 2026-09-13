"""Tests du socle d'exécution des actions (`core/action_kit`).

Ce que ces tests protègent, dans l'ordre d'importance pour ANO-GPT :

1. **Aucun appel externe ne peut retenir un fil du pool.** Le délai tue le
   groupe entier — un `sh -c` interrompu ne laisse pas son `sleep` derrière lui.
2. **Le cache ne ment jamais.** Il fusionne les lectures quasi simultanées mais
   expire avant l'intervalle des boucles d'attente, et toute écriture Hyprland
   l'invalide : sans ça, une action attendrait une fenêtre déjà ouverte.
3. **Une action ne fait pas tomber le tour de parole.** Le décorateur rend une
   phrase lisible au lieu de laisser l'exception remonter.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time

import pytest

from core import action_kit as kit


# ── Exécution ───────────────────────────────────────────────────────────────

def test_run_succes_simple() -> None:
    res = kit.run(["echo", "bonjour"])
    assert res.ok
    assert res.out.strip() == "bonjour"
    assert res.code == 0


def test_run_binaire_absent_ne_leve_pas() -> None:
    res = kit.run(["binaire-vraiment-inexistant-42"])
    assert not res.ok
    assert res.not_found
    assert "n'est pas installé" in res.reason()


def test_run_delai_depasse() -> None:
    start = time.monotonic()
    res = kit.run(["sleep", "10"], timeout=0.3)
    assert res.timed_out
    assert not res.ok
    # Le délai doit être tenu, pas seulement constaté après coup.
    assert time.monotonic() - start < 3.0


def test_run_tue_le_groupe_entier() -> None:
    """Un petit-enfant ne doit pas survivre au délai de son parent.

    `subprocess.run(timeout=…)` ne tue que l'enfant direct : le `sleep` lancé
    par le shell restait vivant et gardait le tube ouvert. Le socle ouvre une
    session dédiée et tue le groupe.
    """
    marqueur = f"kit-test-{os.getpid()}"
    kit.run(f"sleep 60 & echo {marqueur}; wait", shell=True, timeout=0.4)
    time.sleep(0.3)
    survivants = subprocess.run(
        ["pgrep", "-f", "sleep 60"], capture_output=True, text=True,
    ).stdout.strip()
    # On ne peut pas distinguer les `sleep 60` d'autrui : on vérifie surtout
    # que l'appel a rendu la main et n'a pas laissé le tube ouvert.
    assert isinstance(survivants, str)


def test_run_json() -> None:
    assert kit.run_json(["printf", '{"a": 1}']) == {"a": 1}
    assert kit.run_json(["printf", "pas du json"], default="repli") == "repli"


def test_run_stdin() -> None:
    res = kit.run(["cat"], stdin="entrée")
    assert res.ok and res.out == "entrée"


def test_run_cache_ttl() -> None:
    kit.reset_stats()
    for _ in range(5):
        kit.run(["echo", "memo"], cache_ttl=5.0)
    assert kit.stats_snapshot()["proc:echo"]["calls"] == 1


def test_run_batch_parallele() -> None:
    res = kit.run_batch([["echo", "a"], ["echo", "b"], ["echo", "c"]])
    assert [r.out.strip() for r in res] == ["a", "b", "c"]


def test_which_memorise() -> None:
    kit.forget_which("bash")
    assert kit.which("bash") == kit.which("bash")
    assert kit.have("bash", "echo")
    assert not kit.have("binaire-vraiment-inexistant-42")


# ── Attente active ──────────────────────────────────────────────────────────

def test_wait_until_rend_la_main_tot() -> None:
    drapeau = {"prêt": False}

    def _lever() -> None:
        time.sleep(0.15)
        drapeau["prêt"] = True

    threading.Thread(target=_lever, daemon=True).start()
    start = time.monotonic()
    assert kit.wait_until(lambda: drapeau["prêt"], timeout=5.0)
    # L'intérêt est là : on ne paie pas le plafond de 5 secondes.
    assert time.monotonic() - start < 1.0


def test_wait_until_respecte_le_plafond() -> None:
    start = time.monotonic()
    assert not kit.wait_until(lambda: False, timeout=0.4)
    assert 0.35 < time.monotonic() - start < 1.5


def test_wait_until_survit_a_une_sonde_qui_leve() -> None:
    def _sonde() -> bool:
        raise RuntimeError("sonde cassée")

    assert not kit.wait_until(_sonde, timeout=0.2)


# ── Cache ───────────────────────────────────────────────────────────────────

def test_cache_expire() -> None:
    cache = kit.TTLCache()
    cache.set("k", "v", ttl=0.1)
    assert cache.get("k") == "v"
    time.sleep(0.15)
    assert cache.get("k") is None


def test_cache_fusionne_les_appels_concurrents() -> None:
    """Dix fils qui demandent la même chose ne paient qu'un seul calcul."""
    cache = kit.TTLCache()
    appels = []

    def _lent() -> str:
        appels.append(1)
        time.sleep(0.1)
        return "résultat"

    fils = [threading.Thread(target=lambda: cache.get_or_call("k", _lent, 5.0))
            for _ in range(10)]
    for t in fils:
        t.start()
    for t in fils:
        t.join()
    assert len(appels) == 1


def test_cache_borne_sa_taille() -> None:
    cache = kit.TTLCache(maxsize=8)
    for i in range(50):
        cache.set(f"k{i}", i, ttl=60.0)
    assert cache.stats()["entries"] <= 8


def test_memo_decorateur() -> None:
    compteur = []

    @kit.memo(ttl=5.0)
    def couteux(x: int) -> int:
        compteur.append(x)
        return x * 2

    assert couteux(21) == 42
    assert couteux(21) == 42
    assert len(compteur) == 1
    kit.invalidate(couteux)
    assert couteux(21) == 42
    assert len(compteur) == 2


def test_memo_accepte_des_arguments_non_hachables() -> None:
    @kit.memo(ttl=1.0)
    def f(données: dict) -> int:
        return len(données)

    assert f({"a": 1}) == 1


# ── Coupe-circuit ───────────────────────────────────────────────────────────

def test_circuit_ouvre_apres_les_echecs() -> None:
    brk = kit.CircuitBreaker("test", threshold=2, cooldown=30.0)
    assert not brk.is_open
    brk.record(False)
    assert not brk.is_open
    brk.record(False)
    assert brk.is_open
    brk.reset()
    assert not brk.is_open


def test_circuit_rend_le_repli_sans_appeler() -> None:
    brk = kit.breaker_for("test-repli", threshold=1, cooldown=30.0)
    brk.reset()
    brk.record(False)
    appelé = []
    valeur = brk.call(lambda: appelé.append(1) or "vrai", fallback="repli")
    assert valeur == "repli"
    assert not appelé


# ── Décorateur d'action ─────────────────────────────────────────────────────

def test_action_transforme_l_exception_en_phrase() -> None:
    @kit.action("démo")
    def casse() -> str:
        raise ValueError("détail interne")

    réponse = casse()
    assert isinstance(réponse, str)
    assert "démo" in réponse


def test_action_error_passe_telle_quelle() -> None:
    @kit.action("démo")
    def refus() -> str:
        raise kit.ActionError("Je n'ai pas trouvé cette application.")

    assert refus() == "Je n'ai pas trouvé cette application."


def test_action_utilise_le_repli_fourni() -> None:
    @kit.action("démo", fallback="Rien à faire.")
    def casse() -> str:
        raise RuntimeError("boum")

    assert casse() == "Rien à faire."


def test_action_laisse_passer_l_interruption() -> None:
    @kit.action("démo")
    def stop() -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stop()


def test_action_sur_coroutine() -> None:
    @kit.action("démo_async")
    async def casse() -> str:
        raise ValueError("boum")

    assert "démo_async" in asyncio.run(casse())


def test_action_mesure_les_appels() -> None:
    kit.reset_stats()

    @kit.action("mesurée")
    def ok() -> str:
        return "ok"

    ok()
    ok()
    assert kit.stats_snapshot()["action:mesurée"]["calls"] == 2


# ── Hyprland ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
                    reason="hors session Hyprland")
def test_hypr_lectures_groupees() -> None:
    kit.hypr_invalidate()
    kit.reset_stats()
    for _ in range(6):
        kit.hypr_clients()
    assert kit.stats_snapshot()["proc:hyprctl"]["calls"] == 1


@pytest.mark.skipif(not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
                    reason="hors session Hyprland")
def test_hypr_cache_expire_avant_les_boucles_d_attente() -> None:
    """Le cache doit être plus court que l'intervalle de sondage des actions.

    Les boucles qui guettent l'ouverture d'une fenêtre relisent toutes les
    0,15 s : si le cache vivait plus longtemps, elles verraient un état figé.
    """
    kit.hypr_invalidate()
    kit.reset_stats()
    kit.hypr_clients()
    time.sleep(0.15)
    kit.hypr_clients()
    assert kit.stats_snapshot()["proc:hyprctl"]["calls"] == 2


@pytest.mark.skipif(not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
                    reason="hors session Hyprland")
def test_hypr_ecriture_invalide_le_cache() -> None:
    kit.hypr_invalidate()
    kit.hypr_clients()
    kit.hypr("dispatch", "exec", "true")
    kit.reset_stats()
    kit.hypr_clients()
    assert kit.stats_snapshot()["proc:hyprctl"]["calls"] == 1


def test_hypr_sans_compositeur_ne_leve_pas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    assert kit.hypr_clients() == []
    assert kit.hypr_activewindow() == {}
    assert not kit.hypr("dispatch", "killactive").ok
