"""Il n'existe qu'UNE carte dans ANO-GPT.

« Montre ma position » chargeait autrefois un iframe OpenStreetMap dans le
conteneur plein cadre, pendant qu'une recherche de lieux ouvrait la carte
cyberpunk dans un petit panneau flottant. Même donnée, deux styles, deux
tailles. Ces tests empêchent la seconde carte de revenir.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
UI = "\n".join(
    (UI_ROOT / relative).read_text(encoding="utf-8")
    for relative in (
        "window/media_host.py",
        "window/positions.py",
        "media/map_views.py",
    )
)


def _method(name: str) -> str:
    """Corps d'une méthode du package UI, jusqu'à la suivante."""
    start = UI.index(f"    def {name}(")
    following = UI.find("\n    def ", start + 1)
    return UI[start:following if following != -1 else len(UI)]


def test_aucun_iframe_openstreetmap_ne_subsiste():
    """C'était la « carte Google Maps » que voyait l'utilisateur."""
    assert "openstreetmap.org/export/embed" not in UI


def test_la_position_seule_passe_par_la_carte_cyberpunk():
    body = _method("_on_show_map")
    assert "_render_map" in body
    assert "load(QUrl" not in body, "un chargement d'URL externe est revenu"


def test_les_lieux_passent_par_la_meme_carte():
    body = _method("_on_show_places")
    assert "_render_map" in body


def test_le_rendu_unique_appelle_le_moteur_cyberpunk():
    body = _method("_render_map")
    assert "from core.map_render import render_map" in body
    assert "setHtml" in body


def test_sans_webengine_la_carte_souvre_dans_le_navigateur():
    body = _method("_render_map")
    assert "openstreetmap.org" in body
    # L'ouverture passe par la politique navigateur du projet (Chrome), pas
    # par QDesktopServices : c'est elle qui sait quel navigateur utiliser.
    assert "open_chrome(" in body


def test_la_carte_occupe_tout_le_cadre():
    """« Je veux que la carte soit grande » : elle prend le conteneur entier."""
    body = _method("_render_map")
    assert "self.centralWidget().rect()" in body
    assert "_map_cont.show()" in body
    assert "raise_()" in body


def test_lorbe_reste_synchronise_quand_la_carte_couvre_lecran():
    """Sans cela l'orbe reste en grand derrière la carte et la traverse."""
    assert "_sync_fullscreen_orb" in _method("_render_map")


def test_le_petit_panneau_nest_plus_quun_filet_de_securite():
    """Il ne doit servir que si PyQt6-WebEngine manque."""
    body = _method("_on_show_nearby_map")
    assert "_on_show_places" in body
    index_guard = body.index("self._map_view is not None")
    index_panel = body.index("_nearby_map_panel")
    assert index_guard < index_panel, (
        "la grande carte doit être essayée avant le panneau de repli"
    )


def test_le_panneau_de_repli_utilise_aussi_le_moteur_unique():
    """Même dégradé, il ne doit pas ressusciter un second style de carte."""
    body = _method("load_places")
    assert "from core.map_render import render_map" in body


def test_le_cadrage_des_lieux_suit_le_plus_eloigne():
    """Un rayon fixe laisse soit des lieux hors écran, soit du vide autour."""
    body = _method("_on_show_places")
    assert "dist_km" in body and "max(" in body


# ── le chemin « ma position » ───────────────────────────────────────────────

MAIN = (Path(__file__).resolve().parent.parent / "core" / "tool_dispatcher.py").read_text(encoding="utf-8")


def test_ma_position_demande_un_releve_gps_frais():
    """Afficher une position périmée revient à mentir sur l'endroit où l'on est."""
    start = MAIN.index('elif name == "show_map"')
    block = MAIN[start:start + 1200]
    assert "request_fresh_location" in block


def test_les_lieux_autour_de_moi_refusent_une_position_fixe():
    start = MAIN.index('elif name == "find_nearby"')
    block = MAIN[start:start + 2200]
    assert "fresh_location" in block
    assert "_require_precise_gps" in block
    assert "pas une position IP ou une ancienne position" in block


def test_une_position_inconnue_est_avouee_et_non_inventee():
    """Centrer sur Conakry par défaut ferait passer une supposition pour un fait."""
    start = MAIN.index('elif name == "show_map"')
    block = MAIN[start:start + 2000]
    assert "Conakry" in block, "le garde-fou contre la position inventée a disparu"


# ── Deux rendus, une seule carte : globe (aperçu) et Leaflet (guidage) ──────

def test_le_rendu_par_defaut_reste_la_carte_de_rues():
    """« Montre ma position » doit rester sur la carte, pas sur le globe."""
    body = _method("_render_map")
    assert "from core.map_render import render_globe" in body
    assert 'mode: str = "leaflet"' in body


def test_un_bouton_bascule_entre_le_globe_et_la_carte():
    """Les deux vues existent, la position doit s'afficher sur les deux."""
    body = _method("_toggle_map_view")
    assert "_map_last_args" in body
    assert '"globe" if' in body and '"leaflet"' in body
    assert "_render_map" in body


def test_le_guidage_recharge_en_leaflet_si_le_globe_est_affiche():
    """Un globe ne sait pas guider rue par rue : il faut les tuiles Leaflet."""
    body = _method("_on_start_navigation")
    assert 'mode="leaflet"' in body
    assert "loadFinished" in body, (
        "le JS de démarrage doit attendre que la page Leaflet soit chargée"
    )


def test_la_navigation_deja_en_leaflet_nattend_pas_un_rechargement():
    """Rien ne doit ralentir un guidage déjà en cours sur la bonne carte."""
    body = _method("_on_start_navigation")
    assert '!= "leaflet"' in body


# ── Choisir la vue par la voix : « en carte » / « en globe » ────────────────

DISPATCHER = (Path(__file__).resolve().parent.parent / "core" / "tool_dispatcher.py").read_text(encoding="utf-8")


def test_loutil_show_map_expose_un_parametre_vue():
    start = DISPATCHER.index('"name": "show_map"')
    block = DISPATCHER[start:start + 3200]
    assert '"view"' in block
    assert "'carte'" in block and "'globe'" in block


def test_le_handler_traduit_globe_ou_carte_en_mode_rendu():
    start = DISPATCHER.index('elif name == "show_map":')
    block = DISPATCHER[start:start + 2200]
    assert '"globe" if "globe" in _view_arg' in block
    assert "view=view" in block


def test_on_show_map_garde_la_vue_deja_affichee_par_defaut():
    """Sans demande explicite, on ne doit pas retomber sur Leaflet si le
    globe était déjà affiché — sinon la bascule manuelle serait inutile."""
    body = _method("_on_show_map")
    assert "getattr(self, \"_map_mode\", None)" in body
    assert 'mode=mode' in body
