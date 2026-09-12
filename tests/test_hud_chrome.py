"""Le châssis HUD partagé : la forme, la peinture et les panneaux qui l'utilisent.

Ces tests protègent le langage visuel commun. Ils ne jugent pas le goût, mais
les propriétés qui font qu'un panneau reste lisible : le biseau existe, le fond
est réellement peint, les fiches se calent sur leur texte, et les animations
s'arrêtent quand il n'y a plus rien à animer.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui import (
    C,
    Hud,
    HudButton,
    MusicPlayerPanel,
    RichCardWidget,
    RightCardStack,
    _Marquee,
    _Spectrum,
    qcol,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _render(widget, width=340, height=200) -> QImage:
    """Peint le widget sur un fond transparent et rend l'image obtenue."""
    widget.resize(width, height)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    widget.render(painter)
    painter.end()
    return image


# ── la forme ────────────────────────────────────────────────────────────────

def test_le_biseau_coupe_deux_angles_et_pas_quatre():
    """Quatre angles coupés donnent un octogone décoratif ; deux font une
    diagonale, et c'est elle qui fait lire une pièce d'équipement."""
    rect = QRectF(0, 0, 100, 60)
    path = Hud.bevel(rect, 10.0)

    assert path.elementCount() >= 6
    # L'angle haut-gauche et l'angle bas-droite sont creusés.
    assert not path.contains(QPointF(2, 2))
    assert not path.contains(QPointF(98, 58))
    # Les deux autres restent pleins.
    assert path.contains(QPointF(97, 3))
    assert path.contains(QPointF(3, 57))


def test_le_biseau_ne_devore_jamais_un_petit_panneau():
    """Sur un widget étroit, une coupe fixe de 11 px avalerait la moitié."""
    path = Hud.bevel(QRectF(0, 0, 18, 12), 11.0)
    assert path.contains(QPointF(9, 6)), "le centre a disparu sous la coupe"


def test_la_periode_de_balayage_boucle_avec_la_grille():
    """Sinon la grille saute d'un coup à chaque bouclage du balayage."""
    assert Hud.SCAN_PERIOD % 32 == 0


# ── la peinture ─────────────────────────────────────────────────────────────

def test_le_chassis_peint_un_fond_opaque(qapp):
    image = QImage(120, 80, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    Hud.chassis(painter, QRectF(4, 4, 112, 72), accent=qcol(C.PRI), scan=10.0)
    painter.end()

    centre = QColor(image.pixelColor(60, 40))
    assert centre.alpha() > 200, "le corps du panneau doit être opaque"
    # L'angle coupé reste vide : c'est la signature de la forme.
    assert QColor(image.pixelColor(1, 1)).alpha() == 0


def test_le_chassis_rend_le_chemin_pour_decouper_le_contenu(qapp):
    image = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    path = Hud.chassis(painter, QRectF(2, 2, 56, 36))
    painter.end()
    assert path.contains(QPointF(30, 20))


# ── le bouton ───────────────────────────────────────────────────────────────

def test_le_bouton_ne_laisse_pas_qt_peindre_son_fond(qapp):
    """Sans feuille de style transparente, Qt repeint un rectangle par-dessus
    la peinture et le biseau disparaît."""
    button = HudButton("Analyser")
    assert "transparent" in button.styleSheet()

    image = _render(button, 90, 26)
    assert QColor(image.pixelColor(1, 1)).alpha() == 0, "angle coupé recouvert"
    assert QColor(image.pixelColor(45, 13)).alpha() > 0


def test_la_lueur_du_bouton_est_fondue_et_non_commutee(qapp):
    """Un bouton qui s'allume d'un coup paraît cassé."""
    button = HudButton("Ignorer")
    button._glow = 0.0
    button._ease()
    assert button._glow == pytest.approx(0.0), "au repos, aucune lueur"

    button._glow = 1.0
    button._ease()          # souris absente : la lueur doit redescendre
    assert 0.0 < button._glow < 1.0, "la lueur a été coupée d'un coup"


def test_le_bouton_de_fermeture_ne_vire_au_rouge_quau_survol(qapp):
    """Un rouge permanent crie l'alarme sur une simple croix de fermeture."""
    button = HudButton(icon="x", accent=C.TEXT_DIM, hover_accent=C.RED, size=11)
    assert button._accent == qcol(C.TEXT_DIM)
    assert button._hover_accent == qcol(C.RED)
    # Deux images pré-rendues : pas de rendu SVG à chaque image d'animation.
    assert button._pix is not None and button._pix_hover is not None


def test_un_bouton_principal_reste_lisible_sans_survol(qapp):
    button = HudButton("Analyser", primary=True)
    image = _render(button, 90, 26)
    assert QColor(image.pixelColor(45, 13)).alpha() > 200


# ── les cartes d'information ────────────────────────────────────────────────

def test_une_carte_courte_noccupe_pas_la_hauteur_dune_longue(qapp):
    """La vue s'étirait jusqu'à sa hauteur maximale : deux lignes prenaient
    autant de place qu'un rapport entier et la pile perdait son sens."""
    courte = RichCardWidget("info", "Alerte", "Charge CPU à 91 %.")
    longue = RichCardWidget("result", "Rapport", "\n\n".join(["Paragraphe."] * 12))
    try:
        assert courte.body_view.height() < longue.body_view.height()
        assert longue.body_view.height() <= RichCardWidget._BODY_MAX_H
    finally:
        courte.deleteLater()
        longue.deleteLater()


def test_une_carte_trop_longue_reste_defilable(qapp):
    card = RichCardWidget("result", "Rapport", "\n\n".join(["Paragraphe."] * 30))
    try:
        assert (card.body_view.verticalScrollBarPolicy()
                == Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    finally:
        card.deleteLater()


def test_une_carte_arrive_en_glissant_puis_se_pose(qapp):
    card = RichCardWidget("message", "Alice", "Bonjour")
    try:
        assert card._slide > 0, "la carte doit entrer depuis la droite"
        for _ in range(40):
            card._tick_fx()
        assert card._slide == 0.0, "le glissement doit s'arrêter net"
    finally:
        card.deleteLater()


def test_chaque_type_de_carte_porte_son_etiquette(qapp):
    for kind in ("message", "result", "task", "info", "error"):
        assert RichCardWidget.CARD_TAGS.get(kind), f"étiquette absente : {kind}"


def test_la_fin_dune_action_ferme_uniquement_ses_cartes_temporaires(qapp):
    stack = RightCardStack()
    task = stack.add_card("task", "Recherche web", "Recherche en cours…")
    result = stack.add_card("result", "Résultats", "Trois sources trouvées.")
    try:
        assert stack.dismiss_cards("task") == 1
        assert task._closing is True
        assert result._closing is False
        assert task._anim.endValue() == 0.0
    finally:
        task.deleteLater()
        result.deleteLater()
        stack.deleteLater()


def test_la_sortie_dune_carte_est_un_glissement_cyberpunk(qapp):
    card = RichCardWidget("task", "Analyse", "Analyse en cours…")
    try:
        card._slide = 0.0
        card.close_card()
        card._tick_fx()
        assert card._slide > 0.0, "la carte doit être aspirée vers la droite"
        assert card._fx.interval() == 24, "la dissolution doit rester fluide"
        assert card._anim.duration() == 280
    finally:
        card.deleteLater()


# ── le lecteur musique ──────────────────────────────────────────────────────

def test_le_lecteur_reflete_letat_dans_toutes_ses_pieces(qapp):
    panel = MusicPlayerPanel()
    try:
        panel.update_status({"state": "playing", "title": "Midnight Protocol",
                             "artist": "Neon District", "pos": 60.0,
                             "duration": 240.0})
        assert panel.cover_lbl._playing is True
        assert panel.spectrum._target == 1.0
        assert panel.slider._live is True
        assert panel.btn_play._icon_name == "pause"
        assert panel.slider.value() == 250
        assert panel.time_lbl.text() == "01:00"
        assert panel.dur_lbl.text() == "04:00"

        panel.update_status({"state": "paused", "title": "Midnight Protocol",
                             "artist": "Neon District", "pos": 60.0,
                             "duration": 240.0})
        assert panel.cover_lbl._playing is False
        assert panel.spectrum._target == 0.0
        assert panel.slider._live is False
        assert panel.btn_play._icon_name == "play"
    finally:
        panel.deleteLater()


def test_le_titre_complet_est_conserve_pour_le_defilement(qapp):
    """Le tronquer cacherait justement ce que l'utilisateur cherche à lire."""
    panel = MusicPlayerPanel()
    long_title = "Un titre beaucoup trop long pour tenir dans le panneau étroit"
    try:
        panel.update_status({"state": "playing", "title": long_title,
                             "artist": "X", "pos": 0.0, "duration": 100.0})
        assert panel.title_lbl.text() == long_title
    finally:
        panel.deleteLater()


def test_le_titre_ne_defile_que_sil_deborde(qapp):
    """Un texte court qui glisse pour rien est une distraction permanente."""
    court = _Marquee()
    court.resize(300, 16)
    court.setText("Court")
    assert not court._timer.isActive()

    long = _Marquee()
    long.resize(60, 16)
    long.setText("Un titre bien plus large que soixante pixels")
    assert long._timer.isActive()


def test_le_spectre_cesse_de_se_redessiner_a_larret(qapp):
    """Décor animé en permanence = CPU brûlé pendant que rien ne joue."""
    spectrum = _Spectrum()
    spectrum.set_playing(False)
    spectrum._level = 0.0
    image = _render(spectrum, 200, 26)
    assert QColor(image.pixelColor(100, 20)).alpha() == 0


def test_un_spectre_cache_nanime_rien(qapp):
    """Le lecteur refermé continuait d'animer son décor.

    Sur cette machine à deux cœurs, Qt et la boucle audio se partagent le GIL :
    chaque image peinte pour rien est prise sur la voix.
    """
    spectrum = _Spectrum()
    spectrum.set_playing(True)
    for _ in range(60):
        spectrum._tick()          # jamais affiché
    assert spectrum._level == 0.0, "un widget caché ne doit pas s'animer"


def test_un_spectre_visible_monte_bien_en_regime(qapp):
    spectrum = _Spectrum()
    spectrum.show()
    spectrum.set_playing(True)
    for _ in range(60):
        spectrum._tick()
    assert spectrum._level > 0.5
    spectrum.hide()


def test_le_spectre_est_documente_comme_decoratif():
    """Il ne doit jamais passer pour une analyse audio réelle."""
    assert "Décor, pas mesure" in _Spectrum.__doc__


# ── le piège du fond opaque ─────────────────────────────────────────────────

def test_les_widgets_peints_ne_perforent_pas_le_panneau(qapp):
    """La feuille de style globale donne un fond à tout QWidget.

    Sans règle transparente explicite, chaque widget peint à la main remplit
    d'abord un rectangle `#00060a` opaque — un trou net dans le châssis dégradé
    du panneau qui le porte. Le défaut est invisible tant qu'on teste les
    widgets isolément : il n'apparaît qu'avec le QSS de l'application.
    """
    from ui import MetricBar, _CoverArt, _StatusPill, get_global_style

    previous = qapp.styleSheet()
    qapp.setStyleSheet(get_global_style())
    try:
        for widget, size, point in (
            (_Spectrum(), (200, 26), (100, 20)),
            (MetricBar("CPU"), (200, 30), (150, 25)),
            (_CoverArt(), (54, 54), (1, 1)),
        ):
            image = _render(widget, *size)
            alpha = QColor(image.pixelColor(*point)).alpha()
            assert alpha == 0, (
                f"{type(widget).__name__} peint un fond opaque sous le QSS global"
            )
        # La pastille d'état, elle, peint volontairement sa propre balise.
        pill = _StatusPill()
        pill.set_state("LISTENING")
        image = _render(pill, 200, 32)
        assert QColor(image.pixelColor(1, 1)).alpha() == 0, "angle coupé rempli"
    finally:
        qapp.setStyleSheet(previous)


# ── le budget de peinture ───────────────────────────────────────────────────

def test_le_chassis_est_mis_en_cache_et_non_repeint_a_chaque_image(qapp):
    """Le décor immobile coûtait 12 ms par image sur le lecteur musique.

    Qt et la boucle audio se partagent le GIL sur cette machine à deux cœurs :
    ces millisecondes étaient prises sur la voix, l'assistant s'entendait
    lui-même dans le micro et s'interrompait. Seuls le balayage et les équerres
    bougent réellement ; le reste doit venir d'un cache.
    """
    import time

    from PyQt6.QtCore import QRectF

    from ui import C, Hud, qcol

    Hud._LAYERS.clear()
    image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)

    def _one_frame() -> float:
        start = time.perf_counter()
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        Hud.chassis(painter, QRectF(2, 2, 316, 196), accent=qcol(C.PRI),
                    scan=12.0, pulse=0.4)
        painter.end()
        return time.perf_counter() - start

    _one_frame()                      # la première construit le cache
    assert len(Hud._LAYERS) == 1

    warm = min(_one_frame() for _ in range(20))
    assert warm < 0.0025, (
        f"une image de châssis coûte {warm * 1000:.2f} ms : trop pour une "
        "machine qui décode de l'audio en même temps"
    )


def test_le_cache_de_chassis_reste_borne(qapp):
    """Un panneau étiré à la souris crée une entrée par taille."""
    from PyQt6.QtCore import QRectF

    from ui import C, Hud, qcol

    Hud._LAYERS.clear()
    image = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
    for width in range(120, 120 + Hud._LAYER_LIMIT + 10):
        painter = QPainter(image)
        Hud.chassis(painter, QRectF(2, 2, width, 120), accent=qcol(C.PRI))
        painter.end()
    assert len(Hud._LAYERS) <= Hud._LAYER_LIMIT


def test_les_panneaux_caches_narretent_pas_de_couter(qapp):
    """Le lecteur musique refermé continuait d'animer son décor."""
    import inspect

    from ui import ClipboardPanel, FloatingPanel, RichCardWidget

    for widget in (FloatingPanel, RichCardWidget, ClipboardPanel):
        source = inspect.getsource(widget._tick_fx)
        assert "isVisible" in source, (
            f"{widget.__name__} anime son décor même caché"
        )
