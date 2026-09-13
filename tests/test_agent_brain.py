"""Déléguer la réflexion à l'agent en ligne de commande.

Ces tests ne lancent jamais le vrai agent : ils vérifient ce qui casse en
silence — le cadrage de la question pour une réponse *dite*, la coupure de la
boucle de retour, et le fait qu'un dépassement de temps produise une phrase
plutôt qu'un blanc.
"""

import asyncio
import subprocess

import pytest

from core import agent_brain


class _Completed:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


@pytest.fixture
def agent(monkeypatch):
    """Un agent factice : enregistre la commande au lieu de l'exécuter."""
    calls = {}

    def _run(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs.get("env", {})
        calls["cwd"] = kwargs.get("cwd")
        calls["timeout"] = kwargs.get("timeout")
        calls["preexec"] = kwargs.get("preexec_fn")
        return _Completed(stdout="Réponse de l'agent.")

    monkeypatch.setattr(agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(agent_brain.kit, "run", _run)
    return calls


# ── le cadrage de la question ───────────────────────────────────────────────

def test_la_reponse_est_demandee_pour_etre_dite_pas_lue(agent):
    """Sans consigne, l'agent rend des listes à puces et des blocs de code —
    illisibles à voix haute."""
    agent_brain.think("Pourquoi le ciel est bleu ?")

    prompt = agent["command"][agent["command"].index("-p") + 1]
    assert "voix haute" in prompt
    assert "listes à puces" in prompt
    assert "blocs de code" in prompt
    assert "Pourquoi le ciel est bleu ?" in prompt


def test_le_contexte_connu_est_transmis(agent):
    """Sans lui, l'agent redemande ce que l'utilisateur vient de dire."""
    agent_brain.think("Et ensuite ?", context="On parlait de Hyprland.")
    prompt = agent["command"][agent["command"].index("-p") + 1]
    assert "On parlait de Hyprland." in prompt


def test_une_question_vide_ne_lance_pas_lagent(monkeypatch):
    monkeypatch.setattr(agent_brain, "agent_binary", lambda: "/faux/agy")

    def _explode(*a, **k):
        raise AssertionError("l'agent n'aurait pas dû être lancé")

    monkeypatch.setattr(agent_brain.kit, "run", _explode)
    assert "vide" in agent_brain.think("   ")


# ── la boucle de retour ─────────────────────────────────────────────────────

def test_le_garde_fou_anti_boucle_est_transmis_au_fils(agent):
    """`agy` a ANO-GPT en MCP : sans ce drapeau, l'agent peut renvoyer la
    question à l'assistant vocal, qui la renverra à l'agent, sans fin."""
    agent_brain.think("Une question")
    assert agent["env"].get(agent_brain.LOOP_GUARD_ENV) == "1"


def test_lagent_ne_tourne_pas_dans_le_depot(agent):
    """Lancé dans le projet, l'agent l'indexe et la réponse arrive bien plus
    tard — ce qui se paie directement en silence à l'oreille."""
    agent_brain.think("Une question")
    assert agent["cwd"] == str(agent_brain.Path.home())


# ── les pannes ──────────────────────────────────────────────────────────────

def test_un_depassement_de_temps_donne_une_phrase_pas_un_blanc(monkeypatch):
    monkeypatch.setattr(agent_brain, "agent_binary", lambda: "/faux/agy")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="agy", timeout=90)

    monkeypatch.setattr(agent_brain.subprocess, "run", _timeout)
    answer = agent_brain.think("Question lente", timeout=90)
    assert "90 secondes" in answer
    assert "réponds toi-même" in answer


def test_la_marge_de_temps_depasse_la_borne_interne_de_lagent(agent):
    """Tué avant sa propre limite, l'agent ne peut pas expliquer son abandon."""
    agent_brain.think("Une question", timeout=60)
    assert agent["timeout"] > 60


def test_une_erreur_de_lagent_est_resumee_a_une_ligne(monkeypatch):
    """Une trace entière lue à voix haute est un supplice."""
    monkeypatch.setattr(agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(
        agent_brain.subprocess, "run",
        lambda *a, **k: _Completed(stderr="échec net\ndétail 1\ndétail 2\ndétail 3"),
    )
    answer = agent_brain.think("Question")
    assert "échec net" in answer
    assert "détail 2" not in answer


def test_labsence_dagent_est_signalee_et_non_masquee(monkeypatch):
    monkeypatch.setattr(agent_brain, "agent_binary", lambda: None)
    with pytest.raises(agent_brain.AgentUnavailable):
        agent_brain.think("Question")
    assert agent_brain.available() is False


def test_la_configuration_peut_imposer_modele_et_effort(agent, monkeypatch):
    """Choisir un modèle rapide pour le courant est ce qui rend la voix
    utilisable sans épuiser le quota de l'abonnement."""
    monkeypatch.setattr(agent_brain, "_config",
                        lambda: {"agent_model": "gemini-3.7-flash-low",
                                 "agent_effort": "low"})
    agent_brain.think("Une question")
    command = agent["command"]
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gemini-3.7-flash-low"
    assert command[command.index("--effort") + 1] == "low"


# ── le chemin vocal ─────────────────────────────────────────────────────────

def test_deep_think_passe_par_les_cles_avant_lagent_local(monkeypatch):
    """Une clé externe configurée doit devenir le cerveau demandé."""
    import main
    from core import llm_client

    calls = []
    monkeypatch.setattr(
        llm_client, "think_deep",
        lambda question, context: calls.append(("api", question, context)) or "API",
    )
    monkeypatch.setattr(
        agent_brain, "available",
        lambda: calls.append(("agent",)) or True,
    )

    assert main.JarvisLive._compute_deep_research("Question", "Contexte") == "API"
    assert calls == [("api", "Question", "Contexte")]


def test_recherche_profonde_est_detachee_du_tool_call():
    """Le tool call rend la main sans attendre le calcul lourd."""
    import main

    async def scenario():
        jarvis = main.JarvisLive.__new__(main.JarvisLive)
        jarvis._deep_research_tasks = set()
        release = asyncio.Event()

        async def slow_delivery(_question, _context=""):
            await release.wait()

        jarvis._deliver_deep_research = slow_delivery

        assert jarvis._start_deep_research("question") is True
        assert jarvis._start_deep_research("doublon") is False
        assert len(jarvis._deep_research_tasks) == 1
        release.set()
        await asyncio.gather(*jarvis._deep_research_tasks)
        await asyncio.sleep(0)
        assert not jarvis._deep_research_tasks

    asyncio.run(scenario())


def test_resultat_profond_est_livre_dans_un_nouveau_tour(monkeypatch):
    import main

    class UI:
        def __init__(self):
            self.cards = []
            self.logs = []

        def show_card(self, kind, title, body):
            self.cards.append((kind, title, body))

        def dismiss_cards(self, *_args):
            return None

        def write_log(self, message):
            self.logs.append(message)

    async def scenario():
        jarvis = main.JarvisLive.__new__(main.JarvisLive)
        jarvis.ui = UI()
        jarvis.session = object()
        delivered = []
        monkeypatch.setattr(
            jarvis, "_compute_deep_research",
            lambda question, context="": "résultat solide",
        )

        async def submit(text):
            delivered.append(text)
            return True

        jarvis._submit_text_turn = submit
        await jarvis._deliver_deep_research("question initiale")

        assert any(card[0] == "result" for card in jarvis.ui.cards)
        assert len(delivered) == 1
        assert "résultat solide" in delivered[0]
        assert "sans appeler aucun outil" in delivered[0]

    asyncio.run(scenario())


def test_lagent_tourne_en_priorite_basse(agent):
    """Deux cœurs seulement : lancé à égalité, un binaire de 200 Mo hache la
    voix, et l'assistant prend sa propre parole hachée pour une interruption."""
    agent_brain.think("Une question")
    assert agent.get("preexec") is agent_brain._lower_priority
