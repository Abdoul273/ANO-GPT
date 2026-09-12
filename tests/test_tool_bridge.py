"""Le pont entre ANO-GPT et les agents externes.

Ce qui est vérifié ici n'est pas le confort mais la fidélité : un résultat qui
tient sur plusieurs lignes, un nom de commerce avec apostrophe, et une panne
d'outil doivent traverser le socket sans se déformer ni se taire.
"""

import asyncio
import json

import pytest

from core.tool_bridge import (
    AppUnavailable,
    ToolError,
    decode_reply,
    dispatch,
    encode_request,
)


# ── le protocole ────────────────────────────────────────────────────────────

def test_une_requete_tient_sur_une_seule_ligne():
    """Le socket lit une ligne : un argument multiligne ne doit pas la couper."""
    line = encode_request("show_card", {"body": "ligne 1\nligne 2\nligne 3"})
    assert "\n" not in line
    assert line.startswith("tool ")


def test_un_resultat_multiligne_survit_a_laller_retour():
    reply = dispatch({"x": lambda a: "un\ndeux\ntrois"}, json.dumps({"name": "x"}))
    assert "\n" not in reply, "la réponse doit tenir sur une ligne réseau"
    assert decode_reply("OK " + reply) == "un\ndeux\ntrois"


def test_les_accents_et_apostrophes_traversent_intacts():
    """« L'Escale — Café & Thé » a déjà cassé une carte dans ce projet."""
    nom = "L'Escale — Café & Thé"
    reply = dispatch({"x": lambda a: a["q"]},
                     json.dumps({"name": "x", "args": {"q": nom}}))
    assert decode_reply("OK " + reply) == nom


def test_un_outil_qui_echoue_explique_pourquoi():
    def _boom(args):
        raise RuntimeError("caméra absente")

    reply = dispatch({"boom": _boom}, json.dumps({"name": "boom"}))
    with pytest.raises(ToolError) as excinfo:
        decode_reply("OK " + reply)
    assert "caméra absente" in str(excinfo.value)


def test_un_outil_inconnu_liste_ceux_qui_existent():
    """L'agent doit pouvoir se corriger seul, sans relire la documentation."""
    reply = dispatch({"camera": lambda a: "", "email": lambda a: ""},
                     json.dumps({"name": "telepathie"}))
    with pytest.raises(ToolError) as excinfo:
        decode_reply("OK " + reply)
    message = str(excinfo.value)
    assert "camera" in message and "email" in message


def test_une_requete_illisible_ne_fait_pas_tomber_lapplication():
    reply = dispatch({}, "{ceci n'est pas du json")
    with pytest.raises(ToolError):
        decode_reply("OK " + reply)


def test_des_arguments_mal_formes_sont_refuses_proprement():
    reply = dispatch({"x": lambda a: "ok"},
                     json.dumps({"name": "x", "args": "pas un objet"}))
    with pytest.raises(ToolError):
        decode_reply("OK " + reply)


def test_un_outil_sans_retour_ne_produit_pas_le_mot_none():
    reply = dispatch({"x": lambda a: None}, json.dumps({"name": "x"}))
    assert decode_reply("OK " + reply) == ""


def test_une_erreur_du_socket_est_distinguee_dune_erreur_doutil():
    """L'agent réagit différemment : relancer l'app, ou corriger son appel."""
    with pytest.raises(ToolError):
        decode_reply("ERR quelque chose a échoué")
    with pytest.raises(AppUnavailable):
        decode_reply("")


def test_une_reponse_ancienne_non_json_reste_lisible():
    """Le socket sert aussi `toggle`, `status`… qui répondent en texte brut."""
    assert decode_reply("OK pong") == "pong"


# ── le tour complet, sur un vrai socket ─────────────────────────────────────

def test_un_agent_atteint_reellement_lapplication(tmp_path, monkeypatch):
    """Bout en bout : ControlServer réel, socket réel, client réel."""
    import core.ipc as ipc

    socket_file = tmp_path / "anogpt-test.sock"
    monkeypatch.setattr(ipc, "socket_path", lambda: socket_file)

    from core.tool_bridge import app_running, call_app

    recorded = {}

    def _find_nearby(args):
        recorded.update(args)
        return "1. Pharmacie Camayenne\n   1,2 km · ouverte"

    tools = {"find_nearby": _find_nearby}

    async def _scenario():
        server = ipc.ControlServer()

        async def _tool(raw):
            return await asyncio.to_thread(dispatch, tools, raw)

        server.register("tool", _tool)
        server.register("ping", lambda _: "pong")
        await server.start()
        try:
            assert await asyncio.to_thread(app_running) is True
            result = await asyncio.to_thread(
                call_app, "find_nearby", {"query": "pharmacie", "radius_km": 5}
            )
            return result
        finally:
            await server.close()

    result = asyncio.run(_scenario())
    assert "Pharmacie Camayenne" in result
    assert result.count("\n") == 1, "le saut de ligne doit être préservé"
    assert recorded["query"] == "pharmacie"


def test_application_eteinte_est_signalee_sans_ambiguite(tmp_path, monkeypatch):
    """Un agent doit savoir relancer l'app, pas croire à un bug de son appel."""
    import core.ipc as ipc

    monkeypatch.setattr(ipc, "socket_path", lambda: tmp_path / "absent.sock")
    from core.tool_bridge import app_running, call_app

    assert app_running() is False
    with pytest.raises(AppUnavailable):
        call_app("camera", {"action": "open"})


# ── la boucle agent ↔ assistant ─────────────────────────────────────────────

def test_le_retour_vers_lassistant_est_coupe_dans_une_session_fille(monkeypatch):
    """`agy` a ANO-GPT en MCP. Si l'agent lancé *par* ANO-GPT renvoyait la
    question à l'assistant vocal, celui-ci pourrait redemander une réflexion :
    la boucle ne s'arrêterait jamais."""
    import importlib

    from core.agent_brain import LOOP_GUARD_ENV

    monkeypatch.setenv(LOOP_GUARD_ENV, "1")
    import anogpt_mcp

    importlib.reload(anogpt_mcp)
    try:
        answer = anogpt_mcp.ask_assistant("rappelle-moi la question")
        assert "boucle" in answer
        # Et le refus doit proposer une issue, pas seulement dire non.
        assert "speak" in answer
    finally:
        monkeypatch.delenv(LOOP_GUARD_ENV, raising=False)
        importlib.reload(anogpt_mcp)
