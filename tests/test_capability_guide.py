"""Guide des compétences : résumé parlé, un seul Markdown, jamais recréé à tort."""

from __future__ import annotations

from pathlib import Path

from actions import capability_guide as cg
from core.tool_dispatcher import TOOL_DECLARATIONS
from core import tool_packs as tp


def _path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "Guide des compétences.md"
    monkeypatch.setenv("ANOGPT_COMPETENCES_PATH", str(target))
    monkeypatch.setattr(cg.kit, "spawn", lambda *a, **k: 1)
    monkeypatch.setattr(cg.kit, "which", lambda name: "/usr/bin/" + name)
    return target


def test_les_phrases_de_competences_donnent_un_brief():
    for phrase in (
        "quelles sont tes compétences",
        "qu'est-ce que tu peux faire",
        "que peux-tu faire",
        "c'est quoi tes fonctionnalités",
        "à quoi tu sers",
        "comment je te parle",
        "montre-moi tes compétences",
    ):
        assert cg.parse_intent(phrase) == "brief", phrase
        assert cg.is_skills_question(phrase), phrase
        assert cg.resolve_action("write", phrase, "missing") == "brief", phrase


def test_le_consentement_ecrit_ou_ouvre_selon_l_etat():
    assert cg.parse_intent("oui") == "accept"
    assert cg.parse_intent("vas-y") == "accept"
    assert cg.parse_intent("note-les dans un fichier") == "write"
    assert cg.parse_intent("mets à jour le guide") == "write"
    assert cg.parse_intent("ouvre le guide") == "open"
    assert cg.parse_intent("montre-moi le fichier") == "open"
    assert cg.parse_intent("non") == "refuse"
    assert cg.resolve_action("brief", "oui", "missing") == "write"
    assert cg.resolve_action("brief", "oui", "current") == "open"
    assert cg.resolve_action("", "note tout dans un markdown", "current") == "write"


def test_une_question_plus_le_fichier_ecrit_sans_redemander():
    phrase = "quelles sont tes compétences et note-les dans un fichier"
    assert cg.parse_intent(phrase) == "write"
    assert cg.resolve_action("brief", phrase, "missing") == "write"


def test_brief_sans_fichier_demande_de_noter(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    spoken = cg.capability_guide({"action": "brief", "query": "qu'est-ce que tu peux faire"})
    assert "Je pilote ton ordinateur" in spoken
    assert "fichier markdown" in spoken
    assert "Je ne vais pas le recréer" not in spoken


def test_brief_fichier_a_jour_propose_d_ouvrir(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    cg.write_guide(path)
    spoken = cg.capability_guide({"action": "brief", "query": "tes compétences"})
    assert "déjà prêt" in spoken
    assert "Je ne vais pas le recréer" in spoken
    assert "l'ouvre" in spoken


def test_brief_fichier_perime_propose_de_rafraichir(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    path.write_text("# vieux guide\n\ncontenu obsolète sans empreinte.\n", encoding="utf-8")
    spoken = cg.capability_guide({"action": "brief", "query": "tes compétences"})
    assert "plus à jour" in spoken


def test_write_cree_puis_ne_reecrit_pas(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(cg.kit, "spawn", lambda cmd, **k: opened.append(cmd) or 1)

    first = cg.capability_guide({"action": "write", "query": "note-les"})
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    assert f"fingerprint={cg.catalog_fingerprint()}" in body
    assert "Comment me parler" in body
    assert "Ce que ça fait" in body
    assert "Comment ça marche" in body
    assert "Tu peux dire" in body
    for cap in cg.CAPABILITIES:
        assert cap.title in body, cap.id
    assert "C'est noté" in first
    assert opened  # ouvert après création

    opened.clear()
    mtime = path.stat().st_mtime_ns
    second = cg.capability_guide({"action": "write", "query": "note-les", "open_after": False})
    assert "déjà à jour" in second
    assert "pas recréé" in second
    assert path.read_text(encoding="utf-8") == body
    assert path.stat().st_mtime_ns == mtime
    assert not opened


def test_write_perime_sauve_une_copie_et_rafraichit(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    path.write_text("# ancien\n\nnotes personnelles à ne pas jeter silencieusement.\n", encoding="utf-8")
    spoken = cg.capability_guide({"action": "write", "query": "mets à jour le guide"})
    assert "rafraîchi" in spoken
    assert f"fingerprint={cg.catalog_fingerprint()}" in path.read_text(encoding="utf-8")
    bak = path.with_name(path.stem + ".bak.md")
    assert bak.is_file()
    assert "notes personnelles" in bak.read_text(encoding="utf-8")


def test_oui_apres_brief_ecrit_le_fichier(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    cg.capability_guide({"action": "brief", "query": "que peux-tu faire"})
    spoken = cg.capability_guide({"action": "brief", "query": "oui"})
    assert "C'est noté" in spoken
    assert cg.guide_path().is_file()


def test_oui_quand_le_guide_existe_l_ouvre(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    cg.write_guide(path)
    opened: list = []
    monkeypatch.setattr(cg.kit, "spawn", lambda cmd, **k: opened.append(cmd) or 1)
    spoken = cg.capability_guide({"action": "brief", "query": "oui"})
    assert "ouvre" in spoken.lower()
    assert opened
    assert opened[0][-1] == str(path)


def test_open_sans_fichier_redemande(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    spoken = cg.capability_guide({"action": "open", "query": "ouvre le guide"})
    assert "pas encore" in spoken


def test_refus_ne_touche_pas_au_fichier(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    spoken = cg.capability_guide({"action": "brief", "query": "non merci"})
    assert "essentiel" in spoken
    assert not path.exists()


def test_la_carte_hud_s_affiche_au_brief(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    cards: list[tuple] = []

    class Ui:
        def show_card(self, kind, title, body):
            cards.append((kind, title, body))

    cg.capability_guide({"query": "tes compétences"}, player=Ui())
    assert cards and cards[0][1] == "Ce que je fais le mieux"
    assert "Piloter le PC" in cards[0][2]


def test_empreinte_change_si_le_catalogue_change():
    original = cg.catalog_fingerprint()
    tweaked = cg.Capability(
        "x",
        "Test",
        "X",
        "fait",
        "marche",
        ("dis ça",),
        highlight=False,
    )
    other = cg.catalog_fingerprint(cg.CAPABILITIES + (tweaked,))
    assert original != other
    assert len(original) == 16


def test_ids_uniques_et_au_moins_six_meilleures():
    ids = [cap.id for cap in cg.CAPABILITIES]
    assert len(ids) == len(set(ids))
    assert len(cg.HIGHLIGHTS) >= 6
    assert {cap.id for cap in cg.HIGHLIGHTS} <= set(ids)


def test_outil_declare_dans_le_noyau_et_le_prompt():
    names = {item["name"] for item in TOOL_DECLARATIONS}
    assert "capability_guide" in names
    assert "capability_guide" in tp.CORE
    noyau = {d["name"] for d in tp.select_declarations(TOOL_DECLARATIONS, frozenset())}
    assert "capability_guide" in noyau
    prompt = Path(__file__).resolve().parents[1] / "core" / "prompt.txt"
    text = prompt.read_text(encoding="utf-8")
    assert "capability_guide —" in text
    cold = tp.filter_prompt(text, frozenset())
    assert "capability_guide —" in cold
    assert tp.resolve("quelles sont tes compétences") == frozenset()
