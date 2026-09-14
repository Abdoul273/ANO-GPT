"""Rendu HTML de la grande carte : pins, échappement, cadrage."""

import json
import re

from core.map_render import render_globe, render_map

CONAKRY = (9.6412, -13.5784)


def _place(**overrides):
    base = {
        "name": "Pharmacie Camayenne",
        "lat": 9.545,
        "lon": -13.678,
        "dist_km": 12.4,
        "address": "Corniche Nord",
        "category": "Pharmacie",
        "opening_hours": "08:00–22:00",
        "phone": "+224 000",
        "rating": 4.3,
        "reviews": 128,
        "directions_url": "https://maps.example/dir",
    }
    base.update(overrides)
    return base


def test_a_single_point_renders_without_pins():
    html = render_map("Kaloum", CONAKRY, places=[], radius_km=3)

    assert "leaflet" in html
    assert "var places = [];" in html.replace("\n", " ") or "[]" in html
    assert "9.6412" in html


def test_places_become_numbered_pins():
    html = render_map(
        "pharmacie", CONAKRY,
        places=[_place(), _place(name="Pharmacie du Port", lat=9.55, lon=-13.68)],
    )

    payload = json.loads(re.search(r"var places = (\[.*?\]);", html, re.S).group(1))
    assert [item["n"] for item in payload] == [1, 2]
    assert payload[0]["name"] == "Pharmacie Camayenne"
    assert payload[0]["rating"] == 4.3
    assert payload[0]["url"] == "https://maps.example/dir"


def test_apostrophes_and_accents_survive():
    """Un nom comme « L'Escale » cassait le script de la page."""
    html = render_map("resto", CONAKRY, places=[_place(name="L'Escale — Café & Thé")])

    payload = json.loads(re.search(r"var places = (\[.*?\]);", html, re.S).group(1))
    assert payload[0]["name"] == "L'Escale — Café & Thé"


def test_quotes_in_the_centre_label_are_escaped():
    html = render_map("x", CONAKRY, center_label="L'endroit d'Ano")

    # L'étiquette est insérée dans une chaîne JavaScript à apostrophes simples.
    assert "L\\'endroit d\\'Ano" in html


def test_multiple_results_trigger_automatic_framing():
    html = render_map("x", CONAKRY, places=[_place(), _place(lat=9.7, lon=-13.4)])
    # Sans cadrage, un résultat éloigné sort de l'écran et passe pour absent.
    assert "fitBounds" in html


def test_the_basemap_needs_no_api_key_and_is_darkened_locally():
    html = render_map("x", CONAKRY)
    assert "tile.openstreetmap.org" in html
    assert "cartocdn.com" not in html
    assert "invert(1)" in html, "les tuiles claires brûlent les yeux sur le HUD noir"
    assert "OpenStreetMap contributors" in html


def test_radius_drives_the_zoom():
    close = render_map("x", CONAKRY, radius_km=0.4)
    far = render_map("x", CONAKRY, radius_km=40)

    close_zoom = int(re.search(r"setView\(center, (\d+)\)", close).group(1))
    far_zoom = int(re.search(r"setView\(center, (\d+)\)", far).group(1))
    assert close_zoom > far_zoom


def test_a_place_without_details_still_renders():
    html = render_map("x", CONAKRY, places=[{"name": "Minimal", "lat": 9.6, "lon": -13.5}])
    payload = json.loads(re.search(r"var places = (\[.*?\]);", html, re.S).group(1))
    assert payload[0]["name"] == "Minimal"
    assert payload[0]["address"] == ""


# ── fiches flottantes cyberpunk ─────────────────────────────────────────────

def test_each_place_gets_its_own_floating_card():
    """« Chaque point affiche une petite div avec ses infos. »"""
    html = render_map("pharmacie", CONAKRY, places=[_place(), _place(name="Deux")])

    # Le noeud complet : pastille, trait de rattachement et fiche.
    assert "buildNode" in html
    for piece in ("'card'", "'pin'", "'link v'", "'link h'"):
        assert piece in html, f"{piece} absent de la construction du noeud"


def test_the_cards_are_built_by_the_dom_not_by_string_concatenation():
    """Un nom de commerce vient d'une API : il ne doit pas écrire dans la page."""
    html = render_map("x", CONAKRY, places=[_place(name="<img onerror=alert(1)>")])

    # Le nom voyage en JSON puis passe par textContent : aucune balise ne doit
    # apparaître dans le corps de la page.
    assert "<img onerror" not in html
    assert "textContent" in html


def test_the_cards_float_and_enter_in_sequence():
    html = render_map("x", CONAKRY, places=[_place(), _place(name="B")])

    assert "@keyframes hover-y" in html, "les fiches doivent flotter"
    assert "@keyframes bootin" in html, "elles doivent apparaître en séquence"
    # Décalages irréguliers : sinon toutes les fiches montent en cadence.
    assert "'--fd'" in html and "'--d'" in html


def _keyframe_blocks(html: str) -> dict[str, str]:
    """Extrait chaque bloc @keyframes, accolades imbriquées comprises."""
    blocks = {}
    for match in re.finditer(r"@keyframes ([\w-]+)\s*\{", html):
        depth, index = 1, match.end()
        while depth and index < len(html):
            depth += {"{": 1, "}": -1}.get(html[index], 0)
            index += 1
        blocks[match.group(1)] = html[match.end():index - 1]
    return blocks


def test_the_animations_only_move_transforms():
    """Animer une taille force un recalcul de mise en page à chaque image."""
    html = render_map("x", CONAKRY, places=[_place()])

    blocks = _keyframe_blocks(html)
    assert "hover-y" in blocks and "ping" in blocks
    # `sweepdown` et `cardscan` déplacent `top` sur un élément décoratif isolé,
    # sans influence sur la mise en page du reste : ils sont exclus.
    for name, block in blocks.items():
        if name in ("sweepdown", "cardscan"):
            continue
        for costly in ("width:", "height:", "margin:", "padding:"):
            assert costly not in block, f"@keyframes {name} anime {costly}"


def test_a_crowded_search_folds_the_cards_away():
    """Vingt fiches ouvertes couvriraient la carte entière."""
    html = render_map("x", CONAKRY, places=[_place(name=f"P{i}") for i in range(20)])

    assert "body.dense" in html
    assert "applyDensity" in html
    assert "zoomend" in html, "dézoomer rapproche les points : il faut replier"


def test_reduced_motion_switches_the_animations_off():
    html = render_map("x", CONAKRY, places=[_place()])
    assert "prefers-reduced-motion" in html


def test_the_framing_leaves_room_above_the_pins_for_the_cards():
    """La fiche monte au-dessus de sa pastille : sans marge haute, elle sort."""
    html = render_map("x", CONAKRY, places=[_place(), _place(lat=9.7, lon=-13.4)])

    top = re.search(r"paddingTopLeft: \[(\d+), (\d+)\]", html)
    bottom = re.search(r"paddingBottomRight: \[(\d+), (\d+)\]", html)
    assert top and bottom, "fitBounds sans marge asymétrique"
    assert int(top.group(2)) > int(bottom.group(2)), (
        "la marge du haut doit dépasser celle du bas : les fiches montent"
    )
    assert int(top.group(2)) >= 180


def test_clicking_a_pin_does_not_reach_the_map_handler():
    """La carte referme les fiches au clic : sans arrêt, elle annulait le clic
    sur la pastille dans la même impulsion."""
    html = render_map("x", CONAKRY, places=[_place()])
    assert "L.DomEvent.stopPropagation" in html


def test_cards_avoid_covering_other_pins():
    """Une pastille masquée disparaît de la carte tout en restant listée."""
    html = render_map("x", CONAKRY, places=[_place(), _place(name="B")])
    assert "declutter" in html
    assert "querySelector('.pin')" in html


def test_neighbouring_cards_are_staggered():
    """Alterner gauche/droite ne suffit pas sur une grappe de points serrés."""
    html = render_map("x", CONAKRY, places=[_place(name=f"P{i}") for i in range(6)])
    assert "'--dy'" in html
    assert "var(--dy" in html


def test_the_hud_announces_what_was_found():
    html = render_map("pharmacie", CONAKRY, places=[_place(), _place(name="B")])
    assert "2 NOEUDS" in html
    assert "pharmacie" in html


def test_a_query_with_html_cannot_break_out_of_the_hud():
    html = render_map("<script>alert(1)</script>", CONAKRY, places=[_place()])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_map_has_no_looping_animation_left_running():
    """Carte ouverte, aucune animation en boucle : le CPU appartient au micro."""
    from core.map_render import render_map

    html = render_map("Test", (9.5, -13.7), places=[], radius_km=2.0,
                      center_label="Ici", mark_center=True)
    css_tail = html[html.rfind("Machine à deux cœurs"):]
    assert "animation:none !important" in css_tail
    assert "zoomAnimation: false" in html and "updateWhenIdle: true" in html


# ── Vue globe (« world monitor ») ───────────────────────────────────────────



def test_globe_seul_point_affiche_le_libelle_central():
    html = render_globe("Ma position", CONAKRY, places=[], center_label="Ici")
    assert "Globe()" in html
    assert "Ici" in html


def test_globe_places_deviennent_des_points_numerotes():
    html = render_globe(
        "pharmacie", CONAKRY,
        places=[_place(), _place(name="Pharmacie du Port", lat=9.55, lon=-13.68)],
    )
    assert '"n": 1' in html.replace("'", '"')
    assert "Pharmacie du Port" in html


def test_globe_echappe_les_apostrophes_du_libelle():
    html = render_globe("Test", CONAKRY, places=[], center_label="Chez l'ami")
    assert "Chez l\\'ami" in html


def test_le_panneau_pays_affiche_les_champs_enrichis():
    card = {
        "flag": "🇯🇵", "name": "Japon", "capital": "Tokyo",
        "population": "123 366 734", "currencies": "Yen (¥)",
        "languages": "japonais", "timezone": "Asia/Tokyo",
        "area": "377 930 km²", "neighbors": "", "calling_code": "+81",
        "weather": "☀️ ciel dégagé, 20°C",
    }
    html = render_globe("Japon", CONAKRY, places=[], country=card)
    assert "japonais" in html
    assert "377 930 km²" in html
    assert "+81" in html
